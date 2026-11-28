"""Clean-room Google AI Plan inference provider (Interactions API personal quota).

Speaks ``POST https://generativelanguage.googleapis.com/v1beta/interactions:createPerUserQuota``
with:
- Stateless ``store: false`` requests (never ``v1alpha``, never ``:generateContent``).
- Plain ``Authorization: Bearer <access_token>`` and explicit ``User-Agent: awdk/...``
  (explicitly forbidding ``DPoP``, ``x-goog-api-key``, ``x-goog-user-project``, ``?key=``).
- Fixed 4-model catalog (never calling ``GET /models``).
- Model-validated ``thinking_level`` clamping and ``skip_thought_signature_validator``
  replay injection before unsigned tool calls.
- Unified 3-format error classification (flat JSON object, ``google.rpc.Status`` array,
  SSE error frame) driving zero-stall failover to the local ``:8287`` bridge.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import logging
import os
import re
import sys
from typing import Any, AsyncIterator

import httpx

from .base import (
    LLMProvider,
    LLMResponse,
    Message,
    ProviderCapabilities,
    StreamChunk,
    ToolCall,
    _timer,
)
from .google_ai_plan_auth import (
    GoogleAIPlanAuthError,
    GoogleAIPlanCredentialStore,
    get_valid_access_token,
)

logger = logging.getLogger("adk.llm.google_ai_plan")

# ── Wire Constants & Fixed Model Catalog ─────────────────────────────────────

INTERACTIONS_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/interactions:createPerUserQuota"
)
DEFAULT_MODEL = "gemini-4-hydrogen"
SINGLE_REQUEST_OUTPUT_CAP = 65536
SKIP_THOUGHT_SIGNATURE_SENTINEL = "skip_thought_signature_validator"

MODEL_THINKING_LEVELS: dict[str, tuple[str, ...]] = {
    "gemini-4-hydrogen": ("low", "medium", "high", "xhigh"),
    "gemini-4-neon": ("low", "medium", "high", "xhigh"),
    "gemini-4-carbon": ("low", "medium", "high"),
    "gemini-3.5-flash-lite": ("minimal", "low", "medium", "high"),
}

SUPPORTED_MODELS: tuple[str, ...] = tuple(MODEL_THINKING_LEVELS.keys())

_PROHIBITED_HEADERS = frozenset({"dpop", "x-goog-api-key", "x-goog-user-project"})

_DAILY_LIMIT_TEXT_RE = re.compile(r"reached today's .*? limit", re.IGNORECASE)
_PER_DAY_RATE_RE = re.compile(
    r"rate limit exceeded(?: for model \S+)? \(limit:\s*(\d+)\s+(?:tokens|requests) per day\)",
    re.IGNORECASE,
)
_NOT_SUPPORTED_RE = re.compile(r"not supported for per-user quota", re.IGNORECASE)
_INSUFFICIENT_SCOPE_RE = re.compile(r"insufficient authentication scopes", re.IGNORECASE)
_CLIENT_NOT_AUTH_RE = re.compile(r"oauth client is not authorized", re.IGNORECASE)
_DATA_URI_RE = re.compile(r"^data:([^;]+);base64,(.+)$", re.DOTALL)


def _build_user_agent() -> str:
    try:
        ver = importlib.metadata.version("awdk")
    except Exception:
        ver = "0.0.0-dev"
    pyver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return f"awdk/{ver} (AitherOS; python/{pyver})"


# ── Typed Exception Hierarchy ────────────────────────────────────────────────


class GoogleAIPlanError(RuntimeError):
    """Base error for Google AI Plan inference failures."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        error_code: str = "",
        retry_after_s: float | None = None,
        failover_cooldown_s: float = 60.0,
        circuit_scope: str = "*",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.retry_after_s = retry_after_s
        self.failover_cooldown_s = failover_cooldown_s
        self.circuit_scope = circuit_scope


class GoogleAIPlanQuotaError(GoogleAIPlanError):
    """Non-retryable daily quota exhaustion or model not included on plan."""

    def __init__(
        self,
        message: str,
        *,
        scope: str = "daily",
        model: str = "*",
        status_code: int = 429,
        error_code: str = "",
        retry_after_s: float | None = None,
    ) -> None:
        if scope == "not_on_plan":
            cooldown = 3600.0
        else:
            cooldown = min(float(retry_after_s) if retry_after_s else 900.0, 900.0)
        super().__init__(
            message,
            status_code=status_code,
            error_code=error_code,
            retry_after_s=retry_after_s,
            failover_cooldown_s=cooldown,
            circuit_scope=model or "*",
        )
        self.scope = scope


class GoogleAIPlanRateLimitError(GoogleAIPlanError):
    """Per-minute / burst 429 rate limit."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 429,
        error_code: str = "",
        retry_after_s: float | None = None,
    ) -> None:
        base = float(retry_after_s) if retry_after_s is not None else 30.0
        cooldown = min(max(base, 15.0), 120.0)
        super().__init__(
            message,
            status_code=status_code,
            error_code=error_code,
            retry_after_s=retry_after_s,
            failover_cooldown_s=cooldown,
            circuit_scope="*",
        )


class GoogleAIPlanClientBlockedError(GoogleAIPlanError):
    """Google server-side kill switch (SERVICE_DISABLED / oauth client not authorized)."""

    def __init__(self, message: str, *, status_code: int = 403, error_code: str = "") -> None:
        super().__init__(
            message,
            status_code=status_code,
            error_code=error_code,
            failover_cooldown_s=900.0,
            circuit_scope="*",
        )


class GoogleAIPlanEligibilityError(GoogleAIPlanError):
    """Account lacks eligible Google AI Plan (HTTP 402 or non-scope/non-kill-switch 403)."""

    def __init__(self, message: str, *, status_code: int = 403, error_code: str = "") -> None:
        super().__init__(
            message,
            status_code=status_code,
            error_code=error_code,
            failover_cooldown_s=600.0,
            circuit_scope="*",
        )


class GoogleAIPlanTransientError(GoogleAIPlanError):
    """5xx or transport failure eligible for 1 short retry before failover."""

    def __init__(self, message: str, *, status_code: int = 503, error_code: str = "") -> None:
        super().__init__(
            message,
            status_code=status_code,
            error_code=error_code,
            failover_cooldown_s=60.0,
            circuit_scope="*",
        )


class GoogleAIPlanStreamError(GoogleAIPlanError):
    """Stream closed prematurely before ``interaction.completed``."""

    def __init__(self, message: str = "Google stream ended before interaction.completed") -> None:
        super().__init__(
            message,
            status_code=0,
            error_code="premature_eof",
            failover_cooldown_s=60.0,
            circuit_scope="*",
        )


# ── Unified 3-Format Error Parser ────────────────────────────────────────────


def _parse_retry_after_seconds(headers: Any) -> float | None:
    if not headers:
        return None
    # Check retry-after-ms first (higher precision)
    ms_val = headers.get("retry-after-ms") if hasattr(headers, "get") else None
    if ms_val is not None:
        try:
            return max(0.0, float(ms_val) / 1000.0)
        except (TypeError, ValueError):
            pass
    ra_val = headers.get("retry-after") if hasattr(headers, "get") else None
    if ra_val is not None:
        try:
            return max(0.0, float(ra_val))
        except (TypeError, ValueError):
            pass
    return None


def _extract_error_obj(raw_body: str | bytes | dict | list) -> dict[str, Any]:
    """Normalize flat JSON object, google.rpc.Status array, or SSE error frame."""
    if isinstance(raw_body, bytes):
        raw_body = raw_body.decode("utf-8", errors="replace")

    parsed: Any = raw_body
    if isinstance(raw_body, str):
        text = raw_body.strip()
        # Handle SSE `event: error\ndata: {...}` frame
        if "data:" in text and not text.startswith(("{", "[")):
            data_lines = [
                line[len("data:"):].strip()
                for line in text.splitlines()
                if line.strip().startswith("data:")
            ]
            if data_lines:
                text = "\n".join(data_lines)
        try:
            parsed = json.loads(text)
        except Exception:
            return {"message": raw_body[:500], "code": "", "details": []}

    if isinstance(parsed, list) and parsed:
        first = parsed[0]
        if isinstance(first, dict) and isinstance(first.get("error"), dict):
            return first["error"]
        if isinstance(first, dict):
            return first

    if isinstance(parsed, dict):
        if isinstance(parsed.get("error"), dict):
            return parsed["error"]
        return parsed

    return {"message": str(raw_body)[:500], "code": "", "details": []}


def parse_google_interactions_error(
    status_code: int,
    raw_body: str | bytes | dict | list,
    *,
    headers: Any = None,
    model: str = DEFAULT_MODEL,
) -> Exception:
    """Classify a Google Interactions API error into a typed failover exception."""
    err = _extract_error_obj(raw_body)
    message = str(err.get("message", "") or "").strip() or f"Google AI Plan error (HTTP {status_code})"
    code_str = str(err.get("code", "") or "")
    details = err.get("details") if isinstance(err.get("details"), list) else []
    retry_after_s = _parse_retry_after_seconds(headers)

    error_reasons: list[str] = []
    quota_violations: list[dict[str, Any]] = []
    for item in details:
        if not isinstance(item, dict):
            continue
        t = str(item.get("@type", "") or "")
        if "ErrorInfo" in t and item.get("reason"):
            error_reasons.append(str(item["reason"]))
        if "QuotaFailure" in t and isinstance(item.get("violations"), list):
            for v in item["violations"]:
                if isinstance(v, dict):
                    quota_violations.append(v)

    if status_code == 401:
        return GoogleAIPlanAuthError(
            f"Google AI Plan authentication failed (401): {message}. "
            "Run `adk auth google-ai-plan` to re-authenticate."
        )

    if status_code == 403:
        if "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in error_reasons or _INSUFFICIENT_SCOPE_RE.search(message):
            return GoogleAIPlanAuthError(
                f"Google AI Plan token missing required scope (403): {message}. "
                "Run `adk auth google-ai-plan` and grant the Gemini per-user quota scope.",
                scope_missing=True,
            )
        if "SERVICE_DISABLED" in error_reasons or _CLIENT_NOT_AUTH_RE.search(message):
            return GoogleAIPlanClientBlockedError(
                f"Google AI Plan OAuth client is currently disabled or unauthorized (403): {message}",
                status_code=403,
                error_code="SERVICE_DISABLED" if "SERVICE_DISABLED" in error_reasons else code_str,
            )
        return GoogleAIPlanEligibilityError(
            f"Google AI Plan account ineligible or permission denied (403): {message}. "
            "Check plan eligibility at https://one.google.com/ai",
            status_code=403,
            error_code=code_str,
        )

    if status_code == 402:
        return GoogleAIPlanEligibilityError(
            f"Google AI Plan subscription required or limit reached (402): {message}. "
            "Check your plan at https://one.google.com/ai",
            status_code=402,
            error_code=code_str,
        )

    if status_code == 400 and _NOT_SUPPORTED_RE.search(message):
        return GoogleAIPlanQuotaError(
            f"Model {model!r} is not supported for per-user quota (400): {message}",
            scope="not_on_plan",
            model=model,
            status_code=400,
            error_code=code_str,
        )

    if status_code == 429:
        # 1. Check structured QuotaFailure PerDay violations
        for v in quota_violations:
            qid = str(v.get("quotaId", "") or "")
            if "PerDay" in qid:
                raw_qval = v.get("quotaValue")
                try:
                    qval = int(raw_qval) if raw_qval is not None else 1
                except (TypeError, ValueError):
                    qval = 1
                if qval == 0:
                    return GoogleAIPlanQuotaError(
                        f"Model {model!r} is not included on your Google AI Plan (limit: 0): {message}",
                        scope="not_on_plan",
                        model=model,
                        status_code=429,
                        error_code=code_str,
                        retry_after_s=retry_after_s,
                    )
                return GoogleAIPlanQuotaError(
                    f"Google AI Plan daily quota exhausted for {model!r}: {message}",
                    scope="daily",
                    model=model,
                    status_code=429,
                    error_code=code_str,
                    retry_after_s=retry_after_s,
                )

        # 2. Check `(limit: N tokens|requests per day)` message pattern
        m_day = _PER_DAY_RATE_RE.search(message)
        if m_day:
            limit_val = int(m_day.group(1))
            if limit_val == 0:
                return GoogleAIPlanQuotaError(
                    f"Model {model!r} is not included on your Google AI Plan (limit: 0): {message}",
                    scope="not_on_plan",
                    model=model,
                    status_code=429,
                    error_code=code_str,
                    retry_after_s=retry_after_s,
                )
            return GoogleAIPlanQuotaError(
                f"Google AI Plan daily quota exhausted for {model!r}: {message}",
                scope="daily",
                model=model,
                status_code=429,
                error_code=code_str,
                retry_after_s=retry_after_s,
            )

        # 3. Check human-readable daily limit message
        if _DAILY_LIMIT_TEXT_RE.search(message):
            return GoogleAIPlanQuotaError(
                f"Google AI Plan daily quota exhausted for {model!r}: {message}",
                scope="daily",
                model=model,
                status_code=429,
                error_code=code_str,
                retry_after_s=retry_after_s,
            )

        # 4. Otherwise burst / per-minute 429
        return GoogleAIPlanRateLimitError(
            f"Google AI Plan rate limit (429): {message}",
            status_code=429,
            error_code=code_str,
            retry_after_s=retry_after_s,
        )

    if status_code >= 500 or code_str in ("service_unavailable", "internal_error"):
        return GoogleAIPlanTransientError(
            f"Google AI Plan server error ({status_code}): {message}",
            status_code=status_code or 503,
            error_code=code_str,
        )

    return GoogleAIPlanError(
        message,
        status_code=status_code,
        error_code=code_str,
        retry_after_s=retry_after_s,
        failover_cooldown_s=60.0,
    )


# ── Thinking Level Clamping & Request Builder ────────────────────────────────


def clamp_thinking_level(
    model: str,
    *,
    thinking_level: str | None = None,
    effort: int | None = None,
) -> str:
    """Resolve and clamp ``thinking_level`` to the model's supported set."""
    allowed = MODEL_THINKING_LEVELS.get(model, ("low", "medium", "high"))
    desired = (thinking_level or "").strip().lower()

    if not desired and effort is not None:
        eff = max(1, min(10, int(effort)))
        if eff <= 2:
            desired = "minimal"
        elif eff <= 4:
            desired = "low"
        elif eff <= 6:
            desired = "medium"
        elif eff <= 8:
            desired = "high"
        else:
            desired = "xhigh"

    if not desired:
        return "medium" if "medium" in allowed else allowed[0]

    if desired in allowed:
        return desired

    # Clamp against ordered ladder
    ladder = ("minimal", "low", "medium", "high", "xhigh")
    if desired not in ladder:
        return "medium" if "medium" in allowed else allowed[0]
    idx = ladder.index(desired)
    allowed_indices = [ladder.index(lvl) for lvl in allowed if lvl in ladder]
    if idx < min(allowed_indices):
        return ladder[min(allowed_indices)]
    if idx > max(allowed_indices):
        return ladder[max(allowed_indices)]
    # Fallback to nearest allowed
    nearest = min(allowed_indices, key=lambda i: abs(i - idx))
    return ladder[nearest]


def _convert_user_content(content: str | list[dict]) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    parts: list[dict[str, Any]] = []
    for item in content or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "text" and isinstance(item.get("text"), str):
            parts.append({"type": "text", "text": item["text"]})
        elif itype == "image_url":
            url_obj = item.get("image_url")
            url = url_obj.get("url", "") if isinstance(url_obj, dict) else str(url_obj or "")
            m = _DATA_URI_RE.match(url.strip())
            if m:
                parts.append({"type": "image", "mime_type": m.group(1), "data": m.group(2)})
            elif url:
                parts.append({"type": "text", "text": f"[image: {url}]"})
        elif itype == "image" and item.get("data"):
            parts.append({
                "type": "image",
                "mime_type": str(item.get("mime_type", "image/png")),
                "data": str(item["data"]),
            })
    return parts or [{"type": "text", "text": ""}]


def _convert_tools(tools: list[dict] | None) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            fn = t["function"]
            converted.append({
                "type": "function",
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        elif t.get("name"):
            converted.append({
                "type": "function",
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("parameters", {"type": "object", "properties": {}}),
            })
    return converted


def _normalize_tool_call(tc: Any, idx: int) -> tuple[str, str, dict[str, Any]]:
    if isinstance(tc, ToolCall):
        return tc.id or f"call_{idx}", tc.name, tc.arguments if isinstance(tc.arguments, dict) else {}
    if isinstance(tc, dict):
        tc_id = str(tc.get("id") or f"call_{idx}")
        if isinstance(tc.get("function"), dict):
            fn = tc["function"]
            name = str(fn.get("name", ""))
            raw_args = fn.get("arguments", {})
        else:
            name = str(tc.get("name", ""))
            raw_args = tc.get("arguments", {})
        if isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args) if raw_args.strip() else {}
            except Exception:
                parsed = {}
            args = parsed if isinstance(parsed, dict) else {"value": parsed}
        elif isinstance(raw_args, dict):
            args = raw_args
        else:
            args = {}
        return tc_id, name, args
    return f"call_{idx}", "", {}


def build_interaction_request(
    messages: list[Message],
    *,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 4096,
    tools: list[dict] | None = None,
    tool_choice: str | dict | None = None,
    stream: bool = True,
    effort: int | None = None,
    thinking_level: str | None = None,
) -> dict[str, Any]:
    """Build a stateless Google Interactions API request body.

    Guarantees:
    - ``store`` is always ``False`` (never ``previous_interaction_id`` / ``continuation_token`` / ``background``).
    - ``temperature`` and ``top_p`` are NEVER emitted.
    - ``max_output_tokens`` is clamped to ``[1, 65536]``.
    - Any assistant turn with ``tool_calls`` and no signed ``thought`` step gets
      ``{"type": "thought", "signature": "skip_thought_signature_validator"}`` prepended.
    """
    system_parts: list[str] = []
    steps: list[dict[str, Any]] = []

    for msg in messages:
        if msg.role == "system":
            if isinstance(msg.content, str) and msg.content.strip():
                system_parts.append(msg.content.strip())
            elif isinstance(msg.content, list):
                text = "".join(
                    p.get("text", "") for p in msg.content if isinstance(p, dict) and p.get("type") == "text"
                ).strip()
                if text:
                    system_parts.append(text)
            continue

        if msg.role == "user":
            steps.append({
                "type": "user_input",
                "content": _convert_user_content(msg.content),
            })
            continue

        if msg.role == "assistant":
            has_signed_thought = False
            if msg.content_blocks:
                for blk in msg.content_blocks:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("type") in ("google_thought", "thought"):
                        sig = str(blk.get("signature", "") or "")
                        thought_step: dict[str, Any] = {"type": "thought"}
                        if sig:
                            thought_step["signature"] = sig
                            has_signed_thought = True
                        summary = blk.get("summary")
                        if summary:
                            thought_step["summary"] = summary
                        steps.append(thought_step)

            text_content = msg.content if isinstance(msg.content, str) else ""
            if text_content:
                steps.append({
                    "type": "model_output",
                    "content": [{"type": "text", "text": text_content}],
                })

            if msg.tool_calls:
                if not has_signed_thought:
                    steps.append({
                        "type": "thought",
                        "signature": SKIP_THOUGHT_SIGNATURE_SENTINEL,
                    })
                for idx, tc in enumerate(msg.tool_calls):
                    tc_id, tc_name, tc_args = _normalize_tool_call(tc, idx)
                    steps.append({
                        "type": "function_call",
                        "id": tc_id,
                        "name": tc_name,
                        "arguments": tc_args,
                    })
            continue

        if msg.role == "tool":
            result_val: Any = msg.content
            if isinstance(msg.content, list):
                result_val = _convert_user_content(msg.content)
            is_err = False
            if isinstance(result_val, str) and result_val.startswith("Error:"):
                is_err = True
            steps.append({
                "type": "function_result",
                "call_id": msg.tool_call_id or "",
                "name": msg.name or "",
                "result": result_val,
                "is_error": is_err,
            })

    gen_cfg: dict[str, Any] = {
        "thinking_level": clamp_thinking_level(model, thinking_level=thinking_level, effort=effort),
        "thinking_summaries": "auto",
        "max_output_tokens": min(max(1, int(max_tokens or 4096)), SINGLE_REQUEST_OUTPUT_CAP),
    }

    if tool_choice is not None:
        if isinstance(tool_choice, str):
            tc_low = tool_choice.strip().lower()
            if tc_low in ("required", "any"):
                gen_cfg["tool_choice"] = "any"
            elif tc_low in ("auto", "none"):
                gen_cfg["tool_choice"] = tc_low
        elif isinstance(tool_choice, dict):
            gen_cfg["tool_choice"] = "any"

    body: dict[str, Any] = {
        "model": model,
        "store": False,
        "stream": bool(stream),
        "generation_config": gen_cfg,
        "input": steps,
    }
    if system_parts:
        body["system_instruction"] = "\n\n".join(system_parts)

    converted_tools = _convert_tools(tools)
    if converted_tools:
        body["tools"] = converted_tools

    return body


# ── Response / Usage Parsing ─────────────────────────────────────────────────


def _map_status(status: str, has_tool_calls: bool) -> str:
    s = (status or "").strip().lower()
    if s == "requires_action" or has_tool_calls:
        return "tool_calls"
    if s == "completed":
        return "stop"
    if s in ("incomplete", "budget_exceeded", "requires_continuation"):
        return "length"
    if s == "failed":
        return "failed"
    return "stop"


def _extract_usage(usage: dict[str, Any] | None) -> tuple[int, int, int, int]:
    """Return ``(tokens_used, prompt_tokens, completion_tokens, cache_read_tokens)``."""
    u = usage if isinstance(usage, dict) else {}
    total_input = int(u.get("total_input_tokens") or 0)
    total_cached = int(u.get("total_cached_tokens") or 0)
    total_output = int(u.get("total_output_tokens") or 0)
    total_thought = int(u.get("total_thought_tokens") or 0)
    completion = total_output + total_thought
    total = int(u.get("total_tokens") or 0) or (total_input + completion)
    return total, total_input, completion, total_cached


def _parse_unary_response(data: dict[str, Any], model: str, latency_ms: float) -> LLMResponse:
    status = str(data.get("status", "completed") or "completed")
    if status.lower() == "failed":
        errors = data.get("errors") or []
        msg = json.dumps(errors) if errors else "Google interaction failed"
        raise GoogleAIPlanError(msg, status_code=200, error_code="interaction_failed")

    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    raw_blocks: list[dict[str, Any]] = []

    for idx, step in enumerate(data.get("steps") or []):
        if not isinstance(step, dict):
            continue
        stype = step.get("type")
        if stype == "thought":
            sig = str(step.get("signature", "") or "")
            summary = step.get("summary") or []
            for item in summary:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    reasoning_parts.append(item["text"])
            if sig or summary:
                raw_blocks.append({
                    "type": "google_thought",
                    "signature": sig,
                    "summary": summary,
                })
        elif stype == "model_output":
            for item in step.get("content") or []:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    text_parts.append(item["text"])
        elif stype == "function_call":
            args = step.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except Exception:
                    args = {}
            tool_calls.append(
                ToolCall(
                    id=str(step.get("id") or f"call_{idx}"),
                    name=str(step.get("name") or ""),
                    arguments=args if isinstance(args, dict) else {},
                )
            )

    tokens_used, prompt_tokens, completion_tokens, cache_read = _extract_usage(data.get("usage"))
    finish_reason = _map_status(status, bool(tool_calls))

    return LLMResponse(
        content="".join(text_parts),
        model=str(data.get("model") or model),
        tokens_used=tokens_used,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        reasoning="".join(reasoning_parts),
        cache_read_tokens=cache_read,
        raw_content_blocks=raw_blocks,
    )


# ── Provider Implementation ──────────────────────────────────────────────────


class GoogleAIPlanProvider(LLMProvider):
    """Google AI Plan provider using ``v1beta/interactions:createPerUserQuota``."""

    accepts_effort: bool = True

    def __init__(
        self,
        *,
        default_model: str = DEFAULT_MODEL,
        store: GoogleAIPlanCredentialStore | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        config: Any = None,
        endpoint: str = INTERACTIONS_ENDPOINT,
        transport: httpx.AsyncBaseTransport | None = None,
        token_transport: httpx.AsyncBaseTransport | None = None,
        header_timeout_s: float | None = None,
        burst_max_wait_s: float | None = None,
        transient_retry_delay_s: float = 1.0,
    ) -> None:
        self.default_model = default_model or DEFAULT_MODEL
        self._store = store or GoogleAIPlanCredentialStore()
        self._client_id = client_id
        self._client_secret = client_secret
        self._config = config
        self._endpoint = endpoint
        self._transport = transport
        self._token_transport = token_transport
        self._user_agent = _build_user_agent()
        self._header_timeout_s = (
            float(header_timeout_s)
            if header_timeout_s is not None
            else float(os.environ.get("AITHER_GOOGLE_AI_PLAN_HEADER_TIMEOUT_S", "60.0"))
        )
        self._burst_max_wait_s = (
            float(burst_max_wait_s)
            if burst_max_wait_s is not None
            else float(os.environ.get("AITHER_GOOGLE_AI_PLAN_BURST_MAX_WAIT_S", "5.0"))
        )
        self._transient_retry_delay_s = float(transient_retry_delay_s)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(prompt_cache="automatic", batch=False)

    async def list_models(self) -> list[str]:
        """Return fixed per-user-quota model catalog (never calls GET /models)."""
        return list(SUPPORTED_MODELS)

    async def health_check(self) -> bool:
        try:
            cred = self._store.load(verify_binding=True)
            return cred is not None and bool(cred.refresh_token or cred.access_token)
        except Exception:
            return False

    def _build_headers(self, access_token: str, *, stream: bool) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": self._user_agent,
        }
        # Enforce clean-room prohibited header strip
        for bad in list(headers):
            if bad.lower() in _PROHIBITED_HEADERS:
                del headers[bad]
        return headers

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(connect=15.0, read=None, write=30.0, pool=30.0)

    async def _get_token(self, *, force_refresh: bool = False) -> str:
        token, _ = await get_valid_access_token(
            self._store,
            client_id=self._client_id,
            client_secret=self._client_secret,
            config=self._config,
            force_refresh=force_refresh,
            transport=self._token_transport,
        )
        return token

    async def chat(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 0.7,  # noqa: ARG002 — prohibited on Interactions API
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        top_p: float | None = None,  # noqa: ARG002 — prohibited on Interactions API
        repetition_penalty: float | None = None,  # noqa: ARG002
        cache: bool = False,  # noqa: ARG002 — automatic prompt caching
        **kwargs,
    ) -> LLMResponse:
        target_model = model or self.default_model
        body = build_interaction_request(
            messages,
            model=target_model,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            stream=False,
            effort=kwargs.get("effort"),
            thinking_level=kwargs.get("thinking_level"),
        )

        refreshed_401 = False
        burst_retried = False
        transient_retried = False

        t0 = _timer()
        while True:
            token = await self._get_token(force_refresh=refreshed_401)
            headers = self._build_headers(token, stream=False)
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout(),
                    transport=self._transport,
                ) as client:
                    resp = await asyncio.wait_for(
                        client.post(self._endpoint, json=body, headers=headers),
                        timeout=self._header_timeout_s,
                    )
            except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError) as exc:
                if not transient_retried:
                    transient_retried = True
                    if self._transient_retry_delay_s > 0:
                        await asyncio.sleep(self._transient_retry_delay_s)
                    continue
                raise GoogleAIPlanTransientError(
                    f"Google AI Plan request failed: {exc}",
                    status_code=503,
                ) from exc

            if resp.status_code == 200:
                return _parse_unary_response(resp.json(), target_model, (_timer() - t0) * 1000.0)

            exc = parse_google_interactions_error(
                resp.status_code,
                resp.text,
                headers=resp.headers,
                model=target_model,
            )

            if resp.status_code == 401 and not refreshed_401:
                refreshed_401 = True
                continue

            if isinstance(exc, GoogleAIPlanRateLimitError) and not burst_retried:
                wait_s = exc.retry_after_s
                if wait_s is not None and wait_s <= self._burst_max_wait_s:
                    burst_retried = True
                    if wait_s > 0:
                        await asyncio.sleep(wait_s)
                    continue

            if isinstance(exc, GoogleAIPlanTransientError) and not transient_retried:
                transient_retried = True
                if self._transient_retry_delay_s > 0:
                    await asyncio.sleep(self._transient_retry_delay_s)
                continue

            raise exc

    async def chat_stream(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 0.7,  # noqa: ARG002 — prohibited on Interactions API
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        top_p: float | None = None,  # noqa: ARG002 — prohibited on Interactions API
        repetition_penalty: float | None = None,  # noqa: ARG002
        cache: bool = False,  # noqa: ARG002
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        target_model = model or self.default_model
        body = build_interaction_request(
            messages,
            model=target_model,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            stream=True,
            effort=kwargs.get("effort"),
            thinking_level=kwargs.get("thinking_level"),
        )

        refreshed_401 = False
        burst_retried = False
        transient_retried = False

        while True:
            token = await self._get_token(force_refresh=refreshed_401)
            headers = self._build_headers(token, stream=True)
            emitted_any = False

            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout(),
                    transport=self._transport,
                ) as client:
                    req_cm = client.stream("POST", self._endpoint, json=body, headers=headers)
                    resp = await asyncio.wait_for(
                        req_cm.__aenter__(),
                        timeout=self._header_timeout_s,
                    )
                    try:
                        if resp.status_code != 200:
                            raw_err = await resp.aread()
                            exc = parse_google_interactions_error(
                                resp.status_code,
                                raw_err,
                                headers=resp.headers,
                                model=target_model,
                            )
                            if resp.status_code == 401 and not refreshed_401:
                                refreshed_401 = True
                                continue
                            if isinstance(exc, GoogleAIPlanRateLimitError) and not burst_retried:
                                wait_s = exc.retry_after_s
                                if wait_s is not None and wait_s <= self._burst_max_wait_s:
                                    burst_retried = True
                                    if wait_s > 0:
                                        await asyncio.sleep(wait_s)
                                    continue
                            if isinstance(exc, GoogleAIPlanTransientError) and not transient_retried:
                                transient_retried = True
                                if self._transient_retry_delay_s > 0:
                                    await asyncio.sleep(self._transient_retry_delay_s)
                                continue
                            raise exc

                        # Parse SSE stream
                        steps_state: dict[int, dict[str, Any]] = {}
                        tool_calls: list[ToolCall] = []
                        completed = False
                        finish_reason = "stop"
                        event_name = ""
                        data_lines: list[str] = []

                        async for line in resp.aiter_lines():
                            if line == "":
                                if data_lines:
                                    raw_data = "\n".join(data_lines).strip()
                                    data_lines.clear()
                                    if raw_data == "[DONE]":
                                        event_name = ""
                                        continue
                                    try:
                                        frame = json.loads(raw_data)
                                    except json.JSONDecodeError:
                                        event_name = ""
                                        continue
                                    ev_type = str(frame.get("event_type") or event_name or "")
                                    event_name = ""

                                    if ev_type == "error" or "error" in frame and ev_type != "interaction.completed":
                                        raise parse_google_interactions_error(
                                            503 if (frame.get("error") or {}).get("code") == "service_unavailable" else 429
                                            if (frame.get("error") or {}).get("code") == "too_many_requests"
                                            else 400,
                                            frame,
                                            model=target_model,
                                        )

                                    if ev_type == "step.start":
                                        idx = int(frame.get("index", 0))
                                        step = frame.get("step") or {}
                                        steps_state[idx] = {
                                            "type": step.get("type", ""),
                                            "id": step.get("id", f"call_{idx}"),
                                            "name": step.get("name", ""),
                                            "args_buf": [],
                                            "initial_args": step.get("arguments"),
                                            "sig_buf": [step["signature"]] if step.get("signature") else [],
                                        }
                                    elif ev_type == "step.delta":
                                        idx = int(frame.get("index", 0))
                                        st = steps_state.setdefault(idx, {"type": "", "args_buf": [], "sig_buf": []})
                                        delta = frame.get("delta") or {}
                                        dtype = delta.get("type", "")
                                        if dtype == "text":
                                            txt = delta.get("text", "")
                                            if txt and st.get("type") != "thought":
                                                emitted_any = True
                                                yield StreamChunk(content=txt, done=False, model=target_model)
                                        elif dtype == "thought_signature":
                                            sig = delta.get("signature", "")
                                            if sig:
                                                st.setdefault("sig_buf", []).append(sig)
                                        elif dtype == "arguments_delta":
                                            frag = delta.get("arguments", "")
                                            if isinstance(frag, str):
                                                st.setdefault("args_buf", []).append(frag)
                                    elif ev_type == "step.stop":
                                        idx = int(frame.get("index", 0))
                                        st = steps_state.get(idx)
                                        if st and st.get("type") == "function_call":
                                            raw_args = "".join(st.get("args_buf") or [])
                                            if raw_args.strip():
                                                try:
                                                    parsed_args = json.loads(raw_args)
                                                except Exception:
                                                    parsed_args = {}
                                            elif isinstance(st.get("initial_args"), dict):
                                                parsed_args = st["initial_args"]
                                            else:
                                                parsed_args = {}
                                            tool_calls.append(
                                                ToolCall(
                                                    id=str(st.get("id") or f"call_{idx}"),
                                                    name=str(st.get("name") or ""),
                                                    arguments=parsed_args if isinstance(parsed_args, dict) else {},
                                                )
                                            )
                                    elif ev_type == "interaction.completed":
                                        completed = True
                                        inter = frame.get("interaction") or {}
                                        status = str(inter.get("status") or "completed")
                                        if status.lower() == "failed":
                                            raise GoogleAIPlanError(
                                                f"Google interaction failed: {inter.get('errors') or 'unknown'}",
                                                error_code="interaction_failed",
                                            )
                                        finish_reason = _map_status(status, bool(tool_calls))
                                continue

                            if line.startswith("event:"):
                                event_name = line[len("event:"):].strip()
                            elif line.startswith("data:"):
                                data_lines.append(line[len("data:"):].strip())

                        if not completed:
                            raise GoogleAIPlanStreamError(
                                "Google stream ended before interaction.completed"
                            )

                        yield StreamChunk(
                            content="",
                            done=True,
                            model=target_model,
                            tool_calls=tool_calls,
                            finish_reason=finish_reason,
                        )
                        return
                    finally:
                        await req_cm.__aexit__(None, None, None)

            except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError) as exc:
                if not emitted_any and not transient_retried:
                    transient_retried = True
                    if self._transient_retry_delay_s > 0:
                        await asyncio.sleep(self._transient_retry_delay_s)
                    continue
                raise GoogleAIPlanTransientError(
                    f"Google AI Plan stream request failed: {exc}",
                    status_code=503,
                ) from exc
