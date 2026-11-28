"""Local multi-protocol inference bridge for Antigravity, Aither (`awdk`), and `awsh`.

Translates **Gemini `v1beta`**, **Anthropic `/v1/messages`**, and **OpenAI `/v1/chat/completions`**
onto the local AitherOS inference stack (RTX 5090 `bonsai2-27b` on `:8092`, `bonsai-4b` on `:8267`,
`AitherMicroScheduler` on `:8150`, or `awzeal` / custom OpenAI-compatible upstreams) over plain
loopback HTTP — no cloud credentials, no self-signed TLS friction, zero Gemini/Anthropic credit burn.

Why one unified local bridge:
  - ``agy`` (Antigravity CLI) supports ``modelProvider: "gemini"`` + ``GOOGLE_GEMINI_BASE_URL``
    (Gemini ``v1beta`` wire protocol).
  - ``google-antigravity`` Python SDK supports ``LocalOpenAIAgentConfig(base_url=...)``
    (OpenAI ``/v1`` wire protocol).
  - ``claude`` / ``awsh`` local profiles speak Anthropic ``/v1/messages`` (replacing the retired
    ``:8151`` AitherClaudeBridge shim with a live local translator straight to ``:8092`` / ``:8267``
    / ``:8150``).
  - ``awdk`` / ``adk serve`` / ``awharness`` speak OpenAI ``/v1/chat/completions``.

Run:
    python -m adk.harnesses.local_bridge serve --host 127.0.0.1 --port 8287
    python -m adk.harnesses.local_bridge status
"""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger("adk.harnesses.local_bridge")

DEFAULT_HOST = os.environ.get("AITHER_LOCAL_BRIDGE_HOST", "127.0.0.1").strip() or "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("AITHER_LOCAL_BRIDGE_PORT", "8287"))
HEALTH_MARKER = "aither-local-bridge"


@dataclass
class UpstreamSpec:
    """One local OpenAI-compatible inference lane."""

    name: str
    base_url: str
    default_model: str
    aliases: Tuple[str, ...] = ()
    context_window: int = 131072
    verify_tls: bool = False
    priority: int = 10


def default_upstreams() -> List[UpstreamSpec]:
    """Build the ordered list of local inference lanes from env or standard fleet ports."""
    custom = (os.environ.get("AITHER_LOCAL_BRIDGE_UPSTREAM") or "").strip()
    if custom:
        model = (os.environ.get("AITHER_LOCAL_BRIDGE_MODEL") or "bonsai2-27b").strip()
        return [
            UpstreamSpec(
                name="custom",
                base_url=custom.rstrip("/"),
                default_model=model,
                aliases=(model, "default", "local"),
                priority=1,
            )
        ]

    bonsai2_url = (os.environ.get("AITHER_BONSAI2_URL") or "https://127.0.0.1:8092").strip().rstrip("/")
    bonsai4b_url = (os.environ.get("AITHER_BONSAI4B_URL") or "http://127.0.0.1:8267").strip().rstrip("/")
    micro_url = (os.environ.get("AITHER_MICROSCSHEDULER_URL") or os.environ.get("AITHER_MICROS_URL") or "https://127.0.0.1:8150").strip().rstrip("/")
    awzeal_url = (os.environ.get("AITHER_AWZEAL_URL") or "http://127.0.0.1:8088").strip().rstrip("/")

    return [
        UpstreamSpec(
            name="bonsai2.5",
            base_url=bonsai2_url,
            default_model="bonsai2-27b",
            aliases=(
                "bonsai2.5",
                "bonsai2.5-27b",
                "bonsai-2.5-27b",
                "bonsai2.5-27b-5090",
                "ternary-bonsai-2.5-27b",
                "bonsai2",
                "bonsai2-27b",
                "bonsai2-27b-5090",
                "bonsai2-5090",
                "local-bonsai2",
                "aither-orchestrator",
                "aither-best",
                "local",
                "default",
            ),
            context_window=131072,
            verify_tls=False,
            priority=1,
        ),
        UpstreamSpec(
            name="bonsai4b",
            base_url=bonsai4b_url,
            default_model="bonsai-4b",
            aliases=(
                "bonsai-4b",
                "bonsai-4b-5090",
                "bonsai4b",
                "aither-tiny",
                "aither-fast",
                "local-fast",
            ),
            context_window=32768,
            verify_tls=False,
            priority=2,
        ),
        UpstreamSpec(
            name="awzeal",
            base_url=awzeal_url,
            default_model="awzeal-local",
            aliases=("awzeal", "awzeal-local", "zero-libc"),
            context_window=32768,
            verify_tls=False,
            priority=3,
        ),
        UpstreamSpec(
            name="microscheduler",
            base_url=micro_url,
            default_model="bonsai2-27b-5090",
            aliases=("microscheduler", "deepseek-v4-flash-pool", "gemma4-12b"),
            context_window=131072,
            verify_tls=False,
            priority=4,
        ),
    ]


def _ssl_context(url: str, verify_tls: bool) -> Optional[ssl.SSLContext]:
    if not url.lower().startswith("https://"):
        return None
    if verify_tls:
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def probe_upstream(spec: UpstreamSpec, timeout_s: float = 2.0) -> Dict[str, Any]:
    """Probe an upstream's ``/v1/models`` endpoint and return status + served model ids."""
    url = f"{spec.base_url}/v1/models"
    req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=_ssl_context(url, spec.verify_tls)) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            doc = json.loads(raw) if raw.strip() else {}
            models = [
                str(m.get("id") or m.get("name") or "")
                for m in (doc.get("data") or doc.get("models") or [])
                if isinstance(m, dict) and (m.get("id") or m.get("name"))
            ]
            return {
                "name": spec.name,
                "base_url": spec.base_url,
                "healthy": True,
                "status": resp.status,
                "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                "default_model": spec.default_model,
                "models": models,
            }
    except Exception as exc:  # noqa: BLE001
        return {
            "name": spec.name,
            "base_url": spec.base_url,
            "healthy": False,
            "status": 0,
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "default_model": spec.default_model,
            "models": [],
            "error": str(exc)[:200],
        }


# ---------------------------------------------------------------------------
# Protocol translators (pure functions — easy to unit-test)
# ---------------------------------------------------------------------------

_GEMINI_FINISH_MAP = {
    "stop": "STOP",
    "length": "MAX_TOKENS",
    "tool_calls": "STOP",
    "function_call": "STOP",
    "content_filter": "SAFETY",
}

_ANTHROPIC_STOP_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "end_turn",
}


def resolve_target(
    requested_model: str,
    upstreams: List[UpstreamSpec],
    health_cache: Optional[Dict[str, bool]] = None,
) -> Tuple[UpstreamSpec, str]:
    """Map a Gemini, Claude, or local model id onto an upstream lane and model name."""
    raw = (requested_model or "").strip()
    if raw.startswith("models/"):
        raw = raw[len("models/"):]
    low = raw.lower()

    # Fast-tier cloud names (flash-lite / haiku) prefer bonsai4b if healthy, else primary
    want_fast = any(tok in low for tok in ("lite", "haiku", "4b", "tiny", "fast"))

    # 1. Exact alias match first
    for spec in upstreams:
        if low == spec.default_model.lower() or low in {a.lower() for a in spec.aliases}:
            if health_cache is None or health_cache.get(spec.name, True):
                return spec, spec.default_model

    # 2. Fast-tier preference for small/haiku/flash-lite cloud model ids
    if want_fast:
        for spec in upstreams:
            if spec.name == "bonsai4b" and (health_cache is None or health_cache.get(spec.name, True)):
                return spec, spec.default_model

    # 3. First healthy primary upstream (e.g. bonsai2 on :8092)
    for spec in upstreams:
        if health_cache is None or health_cache.get(spec.name, True):
            return spec, spec.default_model

    # 4. Fallback to first configured upstream even if probe hasn't succeeded yet
    first = upstreams[0]
    return first, first.default_model


def _estimate_tokens(obj: Any) -> int:
    raw = json.dumps(obj, ensure_ascii=False, default=str) if not isinstance(obj, str) else obj
    return max(1, len(raw) // 4)


def _extract_gemini_parts_text(parts: List[Dict[str, Any]]) -> str:
    chunks: List[str] = []
    for p in parts or []:
        if isinstance(p, dict) and isinstance(p.get("text"), str):
            chunks.append(p["text"])
    return "".join(chunks)


def gemini_to_openai_request(
    body: Dict[str, Any],
    *,
    target_model: str,
    stream: bool = False,
) -> Dict[str, Any]:
    """Translate a Gemini ``generateContent`` JSON body to OpenAI ``/v1/chat/completions``."""
    messages: List[Dict[str, Any]] = []

    sys_inst = body.get("systemInstruction") or body.get("system_instruction")
    if isinstance(sys_inst, dict):
        sys_txt = _extract_gemini_parts_text(sys_inst.get("parts") or [])
        if sys_txt.strip():
            messages.append({"role": "system", "content": sys_txt})
    elif isinstance(sys_inst, str) and sys_inst.strip():
        messages.append({"role": "system", "content": sys_inst})

    call_seq = 0
    call_id_by_name: Dict[str, List[str]] = {}

    for turn in body.get("contents") or []:
        if not isinstance(turn, dict):
            continue
        role_raw = str(turn.get("role") or "user").lower()
        parts = turn.get("parts") or []
        if role_raw in ("model", "assistant"):
            text_chunks: List[str] = []
            tool_calls: List[Dict[str, Any]] = []
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if "text" in part and not part.get("thought"):
                    text_chunks.append(str(part.get("text") or ""))
                elif "functionCall" in part and isinstance(part["functionCall"], dict):
                    fc = part["functionCall"]
                    call_seq += 1
                    fname = str(fc.get("name") or "tool")
                    cid = str(fc.get("id") or f"call_{call_seq}")
                    call_id_by_name.setdefault(fname, []).append(cid)
                    tool_calls.append({
                        "id": cid,
                        "type": "function",
                        "function": {
                            "name": fname,
                            "arguments": json.dumps(fc.get("args") or {}, ensure_ascii=False),
                        },
                    })
            msg: Dict[str, Any] = {"role": "assistant", "content": "".join(text_chunks) or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            elif msg["content"] is None:
                msg["content"] = ""
            messages.append(msg)
        else:
            # user / function turn
            text_chunks = []
            tool_msgs: List[Dict[str, Any]] = []
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if "functionResponse" in part and isinstance(part["functionResponse"], dict):
                    fr = part["functionResponse"]
                    fname = str(fr.get("name") or "tool")
                    cid = str(fr.get("id") or "")
                    if not cid and call_id_by_name.get(fname):
                        cid = call_id_by_name[fname].pop(0)
                    if not cid:
                        call_seq += 1
                        cid = f"call_{call_seq}"
                    resp_val = fr.get("response")
                    content_str = (
                        resp_val if isinstance(resp_val, str) else json.dumps(resp_val, ensure_ascii=False)
                    )
                    tool_msgs.append({
                        "role": "tool",
                        "tool_call_id": cid,
                        "name": fname,
                        "content": content_str,
                    })
                elif "text" in part:
                    text_chunks.append(str(part.get("text") or ""))
            for tm in tool_msgs:
                messages.append(tm)
            if text_chunks or not tool_msgs:
                messages.append({"role": "user", "content": "".join(text_chunks)})

    if not messages:
        messages.append({"role": "user", "content": ""})

    oai: Dict[str, Any] = {
        "model": target_model,
        "messages": messages,
        "stream": bool(stream),
    }

    gen_cfg = body.get("generationConfig") or body.get("generation_config") or {}
    if isinstance(gen_cfg, dict):
        if "temperature" in gen_cfg and gen_cfg["temperature"] is not None:
            oai["temperature"] = float(gen_cfg["temperature"])
        if "topP" in gen_cfg and gen_cfg["topP"] is not None:
            oai["top_p"] = float(gen_cfg["topP"])
        if "maxOutputTokens" in gen_cfg and gen_cfg["maxOutputTokens"] is not None:
            oai["max_tokens"] = max(1, int(gen_cfg["maxOutputTokens"]))
        if gen_cfg.get("stopSequences"):
            oai["stop"] = list(gen_cfg["stopSequences"])

    # Convert Gemini tools -> OpenAI tools
    oai_tools: List[Dict[str, Any]] = []
    for tool_grp in body.get("tools") or []:
        if not isinstance(tool_grp, dict):
            continue
        decls = tool_grp.get("functionDeclarations") or tool_grp.get("function_declarations") or []
        for fn in decls:
            if not isinstance(fn, dict) or not fn.get("name"):
                continue
            params = fn.get("parameters") or fn.get("parametersJsonSchema") or {"type": "object", "properties": {}}
            oai_tools.append({
                "type": "function",
                "function": {
                    "name": str(fn["name"]),
                    "description": str(fn.get("description") or ""),
                    "parameters": _normalize_gemini_schema(params),
                },
            })
    if oai_tools:
        oai["tools"] = oai_tools

    return oai


def _normalize_gemini_schema(schema: Any) -> Any:
    """Lowercase Gemini uppercase JSON-schema types (OBJECT -> object, STRING -> string)."""
    if isinstance(schema, list):
        return [_normalize_gemini_schema(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out: Dict[str, Any] = {}
    for k, v in schema.items():
        if k == "type" and isinstance(v, str):
            out[k] = v.lower()
        else:
            out[k] = _normalize_gemini_schema(v)
    return out


def openai_to_gemini_response(oai_resp: Dict[str, Any], *, requested_model: str = "") -> Dict[str, Any]:
    """Translate a non-streaming OpenAI chat completion into a Gemini ``GenerateContentResponse``."""
    choices = oai_resp.get("choices") or []
    choice = choices[0] if choices else {}
    msg = choice.get("message") or {}
    text = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""
    tool_calls = msg.get("tool_calls") or []

    parts: List[Dict[str, Any]] = []
    if reasoning and text:
        parts.append({"text": str(reasoning), "thought": True})
    if text:
        parts.append({"text": str(text)})
    elif reasoning and not tool_calls:
        # Thinking model ran out of max_tokens before emitting final content; surface cleanly
        parts.append({"text": str(reasoning)})

    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments")
        if isinstance(raw_args, str):
            try:
                parsed_args = json.loads(raw_args) if raw_args.strip() else {}
            except ValueError:
                parsed_args = {"raw": raw_args}
        elif isinstance(raw_args, dict):
            parsed_args = raw_args
        else:
            parsed_args = {}
        fc_part: Dict[str, Any] = {
            "functionCall": {
                "name": str(fn.get("name") or ""),
                "args": parsed_args,
            }
        }
        if tc.get("id"):
            fc_part["functionCall"]["id"] = str(tc["id"])
        parts.append(fc_part)

    if not parts:
        parts.append({"text": ""})

    finish_raw = str(choice.get("finish_reason") or "stop")
    finish_reason = _GEMINI_FINISH_MAP.get(finish_raw, "STOP")

    usage = oai_resp.get("usage") or {}
    pt = int(usage.get("prompt_tokens") or 0)
    ct = int(usage.get("completion_tokens") or 0)
    tt = int(usage.get("total_tokens") or (pt + ct))

    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": parts},
                "finishReason": finish_reason,
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": pt,
            "candidatesTokenCount": ct,
            "totalTokenCount": tt,
        },
        "modelVersion": str(oai_resp.get("model") or requested_model or "bonsai2-27b"),
        "responseId": str(oai_resp.get("id") or f"resp_{uuid.uuid4().hex[:12]}"),
    }


def _anthropic_text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(b.get("text") or "") if isinstance(b, dict) and b.get("type") == "text"
            else (b if isinstance(b, str) else "")
            for b in content
        )
    return ""


def anthropic_to_openai_request(
    body: Dict[str, Any],
    *,
    target_model: str,
) -> Dict[str, Any]:
    """Translate an Anthropic ``/v1/messages`` JSON body into OpenAI ``/v1/chat/completions``."""
    messages: List[Dict[str, Any]] = []
    sys_raw = body.get("system")
    sys_txt = _anthropic_text_of(sys_raw)
    if sys_txt.strip():
        messages.append({"role": "system", "content": sys_txt})

    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "user")
        content = m.get("content")
        if role == "assistant" and isinstance(content, list):
            text_parts: List[str] = []
            tool_calls: List[Dict[str, Any]] = []
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                btype = blk.get("type")
                if btype == "text":
                    text_parts.append(str(blk.get("text") or ""))
                elif btype == "tool_use":
                    tool_calls.append({
                        "id": str(blk.get("id") or f"toolu_{uuid.uuid4().hex[:10]}"),
                        "type": "function",
                        "function": {
                            "name": str(blk.get("name") or ""),
                            "arguments": json.dumps(blk.get("input") or {}, ensure_ascii=False),
                        },
                    })
            msg: Dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            elif msg["content"] is None:
                msg["content"] = ""
            messages.append(msg)
        elif role == "user" and isinstance(content, list):
            text_parts = []
            tool_results: List[Dict[str, Any]] = []
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                btype = blk.get("type")
                if btype == "tool_result":
                    raw_c = blk.get("content")
                    c_str = _anthropic_text_of(raw_c) if isinstance(raw_c, list) else (
                        raw_c if isinstance(raw_c, str) else json.dumps(raw_c, ensure_ascii=False)
                    )
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": str(blk.get("tool_use_id") or ""),
                        "content": c_str,
                    })
                elif btype == "text":
                    text_parts.append(str(blk.get("text") or ""))
            for tr in tool_results:
                messages.append(tr)
            if text_parts or not tool_results:
                messages.append({"role": "user", "content": "".join(text_parts)})
        else:
            messages.append({"role": role, "content": _anthropic_text_of(content)})

    oai: Dict[str, Any] = {
        "model": target_model,
        "messages": messages or [{"role": "user", "content": ""}],
        "stream": bool(body.get("stream", False)),
    }
    if body.get("max_tokens") is not None:
        oai["max_tokens"] = max(1, int(body["max_tokens"]))
    if body.get("temperature") is not None:
        oai["temperature"] = float(body["temperature"])
    if body.get("top_p") is not None:
        oai["top_p"] = float(body["top_p"])
    if body.get("stop_sequences"):
        oai["stop"] = list(body["stop_sequences"])

    oai_tools: List[Dict[str, Any]] = []
    for t in body.get("tools") or []:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        oai_tools.append({
            "type": "function",
            "function": {
                "name": str(t["name"]),
                "description": str(t.get("description") or ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    if oai_tools:
        oai["tools"] = oai_tools
    return oai


def openai_to_anthropic_response(oai_resp: Dict[str, Any], *, requested_model: str = "") -> Dict[str, Any]:
    """Translate a non-streaming OpenAI chat completion into an Anthropic ``message`` object."""
    choices = oai_resp.get("choices") or []
    choice = choices[0] if choices else {}
    msg = choice.get("message") or {}
    text = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""
    tool_calls = msg.get("tool_calls") or []

    blocks: List[Dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": str(text)})
    elif reasoning and not tool_calls:
        blocks.append({"type": "text", "text": str(reasoning)})

    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments")
        if isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args) if raw_args.strip() else {}
            except ValueError:
                parsed = {"raw": raw_args}
        elif isinstance(raw_args, dict):
            parsed = raw_args
        else:
            parsed = {}
        blocks.append({
            "type": "tool_use",
            "id": str(tc.get("id") or f"toolu_{uuid.uuid4().hex[:10]}"),
            "name": str(fn.get("name") or ""),
            "input": parsed,
        })

    if not blocks:
        blocks.append({"type": "text", "text": ""})

    finish_raw = str(choice.get("finish_reason") or "stop")
    if tool_calls:
        stop_reason = "tool_use"
    else:
        stop_reason = _ANTHROPIC_STOP_MAP.get(finish_raw, "end_turn")

    usage = oai_resp.get("usage") or {}
    return {
        "id": str(oai_resp.get("id") or f"msg_{uuid.uuid4().hex[:14]}"),
        "type": "message",
        "role": "assistant",
        "model": str(requested_model or oai_resp.get("model") or "bonsai2-27b"),
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    }


def iter_openai_sse_to_gemini_sse(
    lines: Iterator[bytes],
    *,
    requested_model: str,
) -> Iterator[bytes]:
    """Convert upstream OpenAI SSE bytes into Gemini ``?alt=sse`` chunks."""
    pending_calls: Dict[int, Dict[str, str]] = {}
    last_usage: Dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    finish_reason = "STOP"
    emitted_any_text = False

    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except ValueError:
            continue

        if isinstance(chunk.get("usage"), dict):
            u = chunk["usage"]
            last_usage = {
                "prompt_tokens": int(u.get("prompt_tokens") or 0),
                "completion_tokens": int(u.get("completion_tokens") or 0),
                "total_tokens": int(u.get("total_tokens") or 0),
            }

        choices = chunk.get("choices") or []
        if not choices:
            continue
        c0 = choices[0]
        delta = c0.get("delta") or {}
        if c0.get("finish_reason"):
            finish_reason = _GEMINI_FINISH_MAP.get(str(c0["finish_reason"]), "STOP")

        parts: List[Dict[str, Any]] = []
        if delta.get("reasoning_content"):
            parts.append({"text": str(delta["reasoning_content"]), "thought": True})
        if delta.get("content"):
            emitted_any_text = True
            parts.append({"text": str(delta["content"])})

        for tc in delta.get("tool_calls") or []:
            idx = int(tc.get("index", 0))
            slot = pending_calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if tc.get("id"):
                slot["id"] = str(tc["id"])
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] += str(fn["name"])
            if fn.get("arguments"):
                slot["arguments"] += str(fn["arguments"])

        if parts:
            payload = {
                "candidates": [{"content": {"role": "model", "parts": parts}, "index": 0}],
                "modelVersion": str(chunk.get("model") or requested_model),
            }
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

    # Emit accumulated tool calls + final finishReason
    final_parts: List[Dict[str, Any]] = []
    for idx in sorted(pending_calls):
        slot = pending_calls[idx]
        try:
            parsed_args = json.loads(slot["arguments"]) if slot["arguments"].strip() else {}
        except ValueError:
            parsed_args = {"raw": slot["arguments"]}
        fc: Dict[str, Any] = {"name": slot["name"], "args": parsed_args}
        if slot["id"]:
            fc["id"] = slot["id"]
        final_parts.append({"functionCall": fc})
    if not final_parts and not emitted_any_text:
        final_parts.append({"text": ""})

    final_payload = {
        "candidates": [
            {
                "content": {"role": "model", "parts": final_parts},
                "finishReason": finish_reason,
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": last_usage["prompt_tokens"],
            "candidatesTokenCount": last_usage["completion_tokens"],
            "totalTokenCount": last_usage["total_tokens"] or (last_usage["prompt_tokens"] + last_usage["completion_tokens"]),
        },
        "modelVersion": requested_model,
    }
    yield f"data: {json.dumps(final_payload, ensure_ascii=False)}\n\n".encode("utf-8")


def iter_openai_sse_to_anthropic_sse(
    lines: Iterator[bytes],
    *,
    requested_model: str,
) -> Iterator[bytes]:
    """Convert upstream OpenAI SSE bytes into Anthropic ``/v1/messages`` SSE events."""
    msg_id = f"msg_{uuid.uuid4().hex[:14]}"

    def _ev(name: str, data: Dict[str, Any]) -> bytes:
        return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")

    yield _ev(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": requested_model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )

    block_idx = 0
    text_open = False
    text_block_idx = -1
    tool_blocks: Dict[int, Dict[str, Any]] = {}
    stop_reason = "end_turn"
    out_tokens = 0

    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except ValueError:
            continue
        usage = chunk.get("usage")
        if isinstance(usage, dict) and usage.get("completion_tokens"):
            out_tokens = int(usage["completion_tokens"])
        choices = chunk.get("choices") or []
        if not choices:
            continue
        c0 = choices[0]
        delta = c0.get("delta") or {}
        if c0.get("finish_reason"):
            stop_reason = _ANTHROPIC_STOP_MAP.get(str(c0["finish_reason"]), "end_turn")

        txt = delta.get("content") or ""
        if txt:
            if not text_open:
                text_block_idx = block_idx
                block_idx += 1
                text_open = True
                yield _ev(
                    "content_block_start",
                    {"type": "content_block_start", "index": text_block_idx, "content_block": {"type": "text", "text": ""}},
                )
            yield _ev(
                "content_block_delta",
                {"type": "content_block_delta", "index": text_block_idx, "delta": {"type": "text_delta", "text": str(txt)}},
            )

        for tc in delta.get("tool_calls") or []:
            t_idx = int(tc.get("index", 0))
            if t_idx not in tool_blocks:
                if text_open:
                    yield _ev("content_block_stop", {"type": "content_block_stop", "index": text_block_idx})
                    text_open = False
                b_idx = block_idx
                block_idx += 1
                fn = tc.get("function") or {}
                tid = str(tc.get("id") or f"toolu_{uuid.uuid4().hex[:10]}")
                tname = str(fn.get("name") or "")
                tool_blocks[t_idx] = {"block_index": b_idx, "id": tid, "name": tname}
                yield _ev(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": b_idx,
                        "content_block": {"type": "tool_use", "id": tid, "name": tname, "input": {}},
                    },
                )
            fn = tc.get("function") or {}
            arg_delta = fn.get("arguments") or ""
            if arg_delta:
                yield _ev(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": tool_blocks[t_idx]["block_index"],
                        "delta": {"type": "input_json_delta", "partial_json": str(arg_delta)},
                    },
                )

    if text_open:
        yield _ev("content_block_stop", {"type": "content_block_stop", "index": text_block_idx})
    for tb in tool_blocks.values():
        yield _ev("content_block_stop", {"type": "content_block_stop", "index": tb["block_index"]})
    if tool_blocks:
        stop_reason = "tool_use"

    yield _ev(
        "message_delta",
        {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": out_tokens}},
    )
    yield _ev("message_stop", {"type": "message_stop"})


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

_GEMINI_ACTION_RE = re.compile(
    r"^/v1(?:beta)?/models/([^/:]+):(generateContent|streamGenerateContent|countTokens)$"
)
_GEMINI_MODEL_GET_RE = re.compile(r"^/v1(?:beta)?/models/([^/:]+)$")


@dataclass
class BridgeRuntime:
    upstreams: List[UpstreamSpec] = field(default_factory=default_upstreams)
    upstream_timeout_s: float = 180.0
    _health_cache: Dict[str, bool] = field(default_factory=dict)
    _health_ts: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # Injectable HTTP caller for unit tests: (spec, path, payload, stream) -> (status, headers, body_or_iter)
    transport: Optional[Callable[[UpstreamSpec, str, Optional[Dict[str, Any]], bool], Tuple[int, Dict[str, str], Any]]] = None

    def refresh_health(self, *, force: bool = False) -> List[Dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            if not force and self._health_cache and (now - self._health_ts) < 10.0:
                return [
                    {"name": s.name, "base_url": s.base_url, "healthy": self._health_cache.get(s.name, False), "default_model": s.default_model}
                    for s in self.upstreams
                ]
        results = [probe_upstream(s) for s in self.upstreams]
        with self._lock:
            self._health_cache = {r["name"]: bool(r["healthy"]) for r in results}
            self._health_ts = time.monotonic()
        return results

    def call_upstream(
        self,
        spec: UpstreamSpec,
        path: str,
        payload: Optional[Dict[str, Any]],
        *,
        stream: bool = False,
    ) -> Tuple[int, Dict[str, str], Any]:
        if self.transport is not None:
            return self.transport(spec, path, payload, stream)

        parsed = urllib.parse.urlparse(spec.base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        full_path = (parsed.path.rstrip("/") + path) if parsed.path else path

        if parsed.scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                host, port, timeout=self.upstream_timeout_s, context=_ssl_context(spec.base_url, spec.verify_tls)
            )
        else:
            conn = http.client.HTTPConnection(host, port, timeout=self.upstream_timeout_s)

        headers = {"Accept": "text/event-stream" if stream else "application/json"}
        body_bytes: Optional[bytes] = None
        method = "GET" if payload is None else "POST"
        if payload is not None:
            body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body_bytes))

        conn.request(method, full_path, body=body_bytes, headers=headers)
        resp = conn.getresponse()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        if stream and resp.status == 200:
            def _line_iter() -> Iterator[bytes]:
                try:
                    while True:
                        line = resp.readline()
                        if not line:
                            break
                        yield line
                finally:
                    conn.close()

            return resp.status, resp_headers, _line_iter()
        raw = resp.read()
        conn.close()
        return resp.status, resp_headers, raw


def make_handler(runtime: BridgeRuntime) -> type[BaseHTTPRequestHandler]:
    class LocalBridgeHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.debug(fmt, *args)

        def _send_json(self, status: int, obj: Any) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                doc = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                return {}
            return doc if isinstance(doc, dict) else {}

        def _stream_bytes(self, chunks: Iterator[bytes], content_type: str = "text/event-stream; charset=utf-8") -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in chunks:
                if not chunk:
                    continue
                self.wfile.write(chunk)
                self.wfile.flush()
            self.close_connection = True

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            if path == "/__local_bridge_health":
                body = HEALTH_MARKER.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path in ("/health", "/"):
                probed = runtime.refresh_health(force=(path == "/health"))
                spec, model = resolve_target("default", runtime.upstreams, runtime._health_cache)
                self._send_json(
                    200,
                    {
                        "ok": any(p["healthy"] for p in probed) or runtime.transport is not None,
                        "service": HEALTH_MARKER,
                        "default_lane": spec.name,
                        "default_model": model,
                        "protocols": ["gemini-v1beta", "anthropic-v1", "openai-v1"],
                        "upstreams": probed,
                    },
                )
                return

            if path in ("/v1/models", "/models"):
                probed = runtime.refresh_health(force=False)
                seen: set[str] = set()
                rows: List[Dict[str, Any]] = []
                for p in probed:
                    if not p["healthy"] and runtime.transport is None:
                        continue
                    for mid in (p.get("models") or [p["default_model"]]):
                        if mid and mid not in seen:
                            seen.add(mid)
                            rows.append({"id": mid, "object": "model", "owned_by": f"aither-local:{p['name']}"})
                if not rows:
                    for s in runtime.upstreams:
                        rows.append({"id": s.default_model, "object": "model", "owned_by": f"aither-local:{s.name}"})
                self._send_json(200, {"object": "list", "data": rows})
                return

            if path in ("/v1beta/models", "/v1/models/gemini"):
                models = []
                for mname in ("gemini-2.5-pro", "gemini-2.5-flash", "gemini-4-neon", "bonsai2.5-27b", "bonsai-4b"):
                    models.append({
                        "name": f"models/{mname}",
                        "displayName": f"{mname} (Aither Local Bridge)",
                        "supportedGenerationMethods": ["generateContent", "streamGenerateContent", "countTokens"],
                        "inputTokenLimit": 131072,
                        "outputTokenLimit": 8192,
                    })
                self._send_json(200, {"models": models})
                return

            gm = _GEMINI_MODEL_GET_RE.match(path)
            if gm:
                mname = gm.group(1)
                self._send_json(
                    200,
                    {
                        "name": f"models/{mname}",
                        "displayName": f"{mname} (Aither Local Bridge)",
                        "supportedGenerationMethods": ["generateContent", "streamGenerateContent", "countTokens"],
                        "inputTokenLimit": 131072,
                        "outputTokenLimit": 8192,
                    },
                )
                return

            self._send_json(404, {"error": f"Unknown route: {path}"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)
            body = self._read_json()

            # 1. Gemini v1beta protocol (agy CLI / Gemini CLI)
            gm = _GEMINI_ACTION_RE.match(path)
            if gm:
                req_model, action = gm.group(1), gm.group(2)
                if action == "countTokens":
                    self._send_json(200, {"totalTokens": _estimate_tokens(body)})
                    return
                if not runtime._health_cache and runtime.transport is None:
                    runtime.refresh_health(force=True)
                spec, target_model = resolve_target(req_model, runtime.upstreams, runtime._health_cache)
                want_stream = action == "streamGenerateContent"
                alt_sse = (qs.get("alt") or [""])[0].lower() == "sse"
                oai_req = gemini_to_openai_request(body, target_model=target_model, stream=(want_stream and alt_sse))
                try:
                    status, _hdrs, resp_data = runtime.call_upstream(
                        spec, "/v1/chat/completions", oai_req, stream=(want_stream and alt_sse)
                    )
                except Exception as exc:  # noqa: BLE001
                    self._send_json(503, {"error": {"code": 503, "message": f"Local upstream '{spec.name}' unreachable: {exc}", "status": "UNAVAILABLE"}})
                    return

                if status != 200:
                    err_txt = resp_data.decode("utf-8", errors="replace") if isinstance(resp_data, (bytes, bytearray)) else str(resp_data)
                    self._send_json(status, {"error": {"code": status, "message": err_txt[:600], "status": "INTERNAL"}})
                    return

                if want_stream and alt_sse:
                    self._stream_bytes(iter_openai_sse_to_gemini_sse(resp_data, requested_model=req_model))
                    return

                oai_doc = json.loads(resp_data.decode("utf-8", errors="replace")) if isinstance(resp_data, (bytes, bytearray)) else resp_data
                gem_doc = openai_to_gemini_response(oai_doc, requested_model=req_model)
                self._send_json(200, [gem_doc] if want_stream else gem_doc)
                return

            # 2. Anthropic /v1/messages protocol (awsh claude harness / bonsai2 expeditions)
            if path in ("/v1/messages/count_tokens", "/anthropic/v1/messages/count_tokens"):
                self._send_json(200, {"input_tokens": _estimate_tokens(body)})
                return

            if path in ("/v1/messages", "/anthropic/v1/messages"):
                req_model = str(body.get("model") or "bonsai2-27b")
                if not runtime._health_cache and runtime.transport is None:
                    runtime.refresh_health(force=True)
                spec, target_model = resolve_target(req_model, runtime.upstreams, runtime._health_cache)
                want_stream = bool(body.get("stream", False))
                oai_req = anthropic_to_openai_request(body, target_model=target_model)
                try:
                    status, _hdrs, resp_data = runtime.call_upstream(
                        spec, "/v1/chat/completions", oai_req, stream=want_stream
                    )
                except Exception as exc:  # noqa: BLE001
                    self._send_json(503, {"type": "error", "error": {"type": "api_error", "message": f"Local upstream '{spec.name}' unreachable: {exc}"}})
                    return

                if status != 200:
                    err_txt = resp_data.decode("utf-8", errors="replace") if isinstance(resp_data, (bytes, bytearray)) else str(resp_data)
                    self._send_json(status, {"type": "error", "error": {"type": "api_error", "message": err_txt[:600]}})
                    return

                if want_stream:
                    self._stream_bytes(iter_openai_sse_to_anthropic_sse(resp_data, requested_model=req_model))
                    return

                oai_doc = json.loads(resp_data.decode("utf-8", errors="replace")) if isinstance(resp_data, (bytes, bytearray)) else resp_data
                self._send_json(200, openai_to_anthropic_response(oai_doc, requested_model=req_model))
                return

            # 3. OpenAI /v1/chat/completions passthrough (google-antigravity LocalOpenAIAgentConfig / awdk)
            if path in ("/v1/chat/completions", "/chat/completions"):
                req_model = str(body.get("model") or "bonsai2-27b")
                if not runtime._health_cache and runtime.transport is None:
                    runtime.refresh_health(force=True)
                spec, target_model = resolve_target(req_model, runtime.upstreams, runtime._health_cache)
                forwarded = dict(body)
                forwarded["model"] = target_model
                want_stream = bool(forwarded.get("stream", False))
                try:
                    status, _hdrs, resp_data = runtime.call_upstream(
                        spec, "/v1/chat/completions", forwarded, stream=want_stream
                    )
                except Exception as exc:  # noqa: BLE001
                    self._send_json(503, {"error": {"message": f"Local upstream '{spec.name}' unreachable: {exc}", "type": "service_unavailable"}})
                    return

                if status != 200:
                    err_txt = resp_data.decode("utf-8", errors="replace") if isinstance(resp_data, (bytes, bytearray)) else str(resp_data)
                    self._send_json(status, {"error": {"message": err_txt[:600], "type": "upstream_error"}})
                    return

                if want_stream:
                    self._stream_bytes(resp_data)
                    return

                raw = resp_data if isinstance(resp_data, (bytes, bytearray)) else json.dumps(resp_data).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return

            self._send_json(404, {"error": f"Unknown route: {path}"})

    return LocalBridgeHandler


def create_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    runtime: Optional[BridgeRuntime] = None,
) -> ThreadingHTTPServer:
    rt = runtime or BridgeRuntime()
    server = ThreadingHTTPServer((host, port), make_handler(rt))
    server.daemon_threads = True
    return server


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="adk-local-bridge", description="AitherOS local multi-protocol LLM bridge")
    sub = parser.add_subparsers(dest="cmd")

    sv = sub.add_parser("serve", help="Run the local Gemini/Anthropic/OpenAI bridge")
    sv.add_argument("--host", default=DEFAULT_HOST)
    sv.add_argument("--port", type=int, default=DEFAULT_PORT)
    sv.add_argument("--upstream", default="", help="Override single OpenAI-compatible upstream URL")
    sv.add_argument("--model", default="", help="Override default upstream model id")

    st = sub.add_parser("status", help="Probe the running local bridge and its upstreams")
    st.add_argument("--url", default=f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")

    args = parser.parse_args(argv)
    if args.cmd == "status":
        url = args.url.rstrip("/") + "/health"
        try:
            with urllib.request.urlopen(url, timeout=3.0) as r:
                print(r.read().decode("utf-8", errors="replace"))
            return 0
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"ok": False, "url": url, "error": str(exc)}))
            return 1

    if args.cmd == "serve" or not args.cmd:
        host = getattr(args, "host", DEFAULT_HOST)
        port = getattr(args, "port", DEFAULT_PORT)
        if getattr(args, "upstream", ""):
            os.environ["AITHER_LOCAL_BRIDGE_UPSTREAM"] = args.upstream
        if getattr(args, "model", ""):
            os.environ["AITHER_LOCAL_BRIDGE_MODEL"] = args.model
        rt = BridgeRuntime()
        srv = create_server(host, port, runtime=rt)
        threading.Thread(target=lambda: rt.refresh_health(force=True), daemon=True).start()
        print(
            f"[aither-local-bridge] listening on http://{host}:{port} "
            f"(protocols: gemini-v1beta, anthropic-v1, openai-v1)",
            flush=True,
        )
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
