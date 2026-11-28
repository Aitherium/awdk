"""Unit & HTTP round-trip tests for the local multi-protocol Antigravity/Aither bridge."""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from adk.antigravity_setup import configure_local_antigravity
from adk.harnesses.local_bridge import (
    BridgeRuntime,
    UpstreamSpec,
    anthropic_to_openai_request,
    create_server,
    gemini_to_openai_request,
    iter_openai_sse_to_anthropic_sse,
    iter_openai_sse_to_gemini_sse,
    openai_to_anthropic_response,
    openai_to_gemini_response,
    resolve_target,
)


def _sample_upstreams() -> List[UpstreamSpec]:
    return [
        UpstreamSpec(
            name="bonsai2",
            base_url="https://127.0.0.1:8092",
            default_model="bonsai2-27b",
            aliases=("bonsai2", "bonsai2-27b", "bonsai2-27b-5090"),
        ),
        UpstreamSpec(
            name="bonsai4b",
            base_url="http://127.0.0.1:8267",
            default_model="bonsai-4b",
            aliases=("bonsai-4b", "aither-tiny"),
        ),
    ]


def test_resolve_target_routes_gemini_claude_and_fast_tiers() -> None:
    ups = _sample_upstreams()
    health = {"bonsai2": True, "bonsai4b": True}

    spec, model = resolve_target("models/gemini-4-neon", ups, health)
    assert spec.name == "bonsai2" and model == "bonsai2-27b"

    spec_fast, model_fast = resolve_target("gemini-2.5-flash-lite", ups, health)
    assert spec_fast.name == "bonsai4b" and model_fast == "bonsai-4b"

    spec_haiku, model_haiku = resolve_target("claude-haiku-4-5", ups, health)
    assert spec_haiku.name == "bonsai4b" and model_haiku == "bonsai-4b"

    # Failover to bonsai4b if bonsai2 is unhealthy
    spec_fb, model_fb = resolve_target("gemini-2.5-pro", ups, {"bonsai2": False, "bonsai4b": True})
    assert spec_fb.name == "bonsai4b" and model_fb == "bonsai-4b"


def test_gemini_request_and_response_translation_with_tools() -> None:
    req = gemini_to_openai_request(
        {
            "systemInstruction": {"parts": [{"text": "You are Aither."}]},
            "contents": [
                {"role": "user", "parts": [{"text": "Check disk"}]},
                {"role": "model", "parts": [{"functionCall": {"name": "df", "args": {"path": "/"}, "id": "c1"}}]},
                {"role": "user", "parts": [{"functionResponse": {"name": "df", "response": {"free_gb": 47}}}]},
            ],
            "tools": [
                {
                    "functionDeclarations": [
                        {
                            "name": "df",
                            "description": "Disk free",
                            "parameters": {"type": "OBJECT", "properties": {"path": {"type": "STRING"}}},
                        }
                    ]
                }
            ],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 512},
        },
        target_model="bonsai2-27b",
    )
    assert req["model"] == "bonsai2-27b"
    assert req["messages"][0] == {"role": "system", "content": "You are Aither."}
    assert req["messages"][2]["tool_calls"][0]["function"]["name"] == "df"
    assert req["messages"][3]["role"] == "tool"
    assert req["messages"][3]["tool_call_id"] == "c1"
    assert req["tools"][0]["function"]["parameters"]["type"] == "object"
    assert req["tools"][0]["function"]["parameters"]["properties"]["path"]["type"] == "string"

    gem = openai_to_gemini_response(
        {
            "id": "chatcmpl-1",
            "model": "bonsai2-27b",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "checking disk",
                        "tool_calls": [
                            {"id": "c2", "type": "function", "function": {"name": "df", "arguments": '{"path":"/"}'}}
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        },
        requested_model="gemini-4-neon",
    )
    parts = gem["candidates"][0]["content"]["parts"]
    assert parts[0]["functionCall"] == {"name": "df", "args": {"path": "/"}, "id": "c2"}
    assert gem["usageMetadata"]["totalTokenCount"] == 20


def test_anthropic_request_and_response_translation() -> None:
    req = anthropic_to_openai_request(
        {
            "model": "bonsai2",
            "system": [{"type": "text", "text": "Stay concise."}],
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "Ping"}]},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "echo", "input": {"x": 1}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
            ],
            "tools": [{"name": "echo", "description": "echo", "input_schema": {"type": "object"}}],
            "max_tokens": 256,
        },
        target_model="bonsai2-27b",
    )
    assert req["model"] == "bonsai2-27b"
    assert req["messages"][0]["role"] == "system"
    assert req["messages"][3] == {"role": "tool", "tool_call_id": "t1", "content": "ok"}

    anth = openai_to_anthropic_response(
        {
            "id": "chatcmpl-2",
            "model": "bonsai2-27b",
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "LOCAL_OK"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
        requested_model="bonsai2",
    )
    assert anth["type"] == "message"
    assert anth["content"] == [{"type": "text", "text": "LOCAL_OK"}]
    assert anth["stop_reason"] == "end_turn"


def test_sse_translators_gemini_and_anthropic() -> None:
    oai_lines = [
        b'data: {"model":"bonsai2-27b","choices":[{"delta":{"content":"Hello "}}]}\n',
        b'data: {"model":"bonsai2-27b","choices":[{"delta":{"content":"local!"},"finish_reason":"stop"}]}\n',
        b"data: [DONE]\n",
    ]
    gem_chunks = b"".join(iter_openai_sse_to_gemini_sse(iter(oai_lines), requested_model="gemini-4-neon")).decode()
    assert '"Hello "' in gem_chunks
    assert '"local!"' in gem_chunks
    assert '"finishReason": "STOP"' in gem_chunks

    anth_chunks = b"".join(iter_openai_sse_to_anthropic_sse(iter(oai_lines), requested_model="bonsai2")).decode()
    assert "event: message_start" in anth_chunks
    assert '"text_delta"' in anth_chunks
    assert "event: message_stop" in anth_chunks


def test_http_server_end_to_end_all_three_protocols() -> None:
    calls: List[Tuple[str, str, Dict[str, Any], bool]] = []

    def fake_transport(
        spec: UpstreamSpec, path: str, payload: Optional[Dict[str, Any]], stream: bool
    ) -> Tuple[int, Dict[str, str], Any]:
        calls.append((spec.name, path, payload or {}, stream))
        if stream:
            def _gen() -> Iterator[bytes]:
                yield b'data: {"model":"bonsai2-27b","choices":[{"delta":{"content":"STREAM_OK"},"finish_reason":"stop"}]}\n'
                yield b"data: [DONE]\n"
            return 200, {"content-type": "text/event-stream"}, _gen()
        body = json.dumps({
            "id": "chatcmpl-test",
            "model": spec.default_model,
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "BRIDGE_OK"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }).encode("utf-8")
        return 200, {"content-type": "application/json"}, body

    rt = BridgeRuntime(upstreams=_sample_upstreams(), transport=fake_transport)
    rt._health_cache = {"bonsai2": True, "bonsai4b": True}
    srv = create_server("127.0.0.1", 0, runtime=rt)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"

        # 1. Gemini v1beta generateContent
        req = urllib.request.Request(
            f"{base}/v1beta/models/gemini-4-neon:generateContent",
            data=json.dumps({"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            gem = json.loads(r.read().decode())
        assert gem["candidates"][0]["content"]["parts"][0]["text"] == "BRIDGE_OK"

        # 2. Gemini v1beta streamGenerateContent?alt=sse
        req_s = urllib.request.Request(
            f"{base}/v1beta/models/gemini-4-neon:streamGenerateContent?alt=sse",
            data=json.dumps({"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_s, timeout=5) as r:
            sse_body = r.read().decode()
        assert "STREAM_OK" in sse_body

        # 3. Anthropic /v1/messages
        req_a = urllib.request.Request(
            f"{base}/anthropic/v1/messages",
            data=json.dumps({"model": "bonsai2", "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_a, timeout=5) as r:
            anth = json.loads(r.read().decode())
        assert anth["content"][0]["text"] == "BRIDGE_OK"

        # 4. OpenAI /v1/chat/completions
        req_o = urllib.request.Request(
            f"{base}/v1/chat/completions",
            data=json.dumps({"model": "bonsai2-27b", "messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_o, timeout=5) as r:
            oai = json.loads(r.read().decode())
        assert oai["choices"][0]["message"]["content"] == "BRIDGE_OK"
    finally:
        srv.shutdown()
        srv.server_close()


def test_configure_local_antigravity_enable_and_disable(tmp_path: Path) -> None:
    cfg = tmp_path / "settings.json"
    res = configure_local_antigravity(
        bridge_url="http://127.0.0.1:8287",
        model="bonsai2-27b",
        cli_settings_path=cfg,
        enable=True,
    )
    assert res["ok"] is True
    assert json.loads(cfg.read_text(encoding="utf-8"))["modelProvider"] == "gemini"
    assert res["env"]["GOOGLE_GEMINI_BASE_URL"] == "http://127.0.0.1:8287"
    assert res["env"]["OPENAI_BASE_URL"] == "http://127.0.0.1:8287/v1"

    res_off = configure_local_antigravity(cli_settings_path=cfg, enable=False)
    assert res_off["enabled"] is False
    assert "modelProvider" not in json.loads(cfg.read_text(encoding="utf-8"))
