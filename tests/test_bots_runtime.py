"""Tests for Knowledge RAG, OpenAPI SSRF guard, Gemini v1beta & OpenAI v1/Assistants runtime, and Playground UI."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.genai import types as gt
from openai.types.beta import Assistant, Thread
from openai.types.beta.threads import Message as BetaMessage, Run
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from adk.bots.actions import validate_action_url
from adk.bots.knowledge import KnowledgeIndex
from adk.bots.manifest import KnowledgeConfig, scaffold_bot
from adk.bots.runtime import BotRuntime, create_bot_app, mount_bot_routes


def _parse_sse_json_events(raw_text: str) -> list[dict | str]:
    events: list[dict | str] = []
    for line in raw_text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        if payload == "[DONE]":
            events.append("[DONE]")
        else:
            events.append(json.loads(payload))
    return events


def test_knowledge_retains_short_docs_and_blocks_traversal(tmp_path: Path) -> None:
    bot_dir = tmp_path / "rag-bot"
    scaffold_bot("Rag Bot", "gemini", bot_dir)
    # Short doc (< 50 chars) MUST be preserved!
    (bot_dir / "knowledge" / "policy.md").write_text(
        "Refund window is 30 days.", encoding="utf-8"
    )
    (bot_dir / "knowledge" / "pricing.json").write_text(
        json.dumps({"starter_plan": "$0/month", "pro_plan": "$29/month"}),
        encoding="utf-8",
    )

    idx = KnowledgeIndex.from_manifest(
        bot_dir, KnowledgeConfig(paths=["knowledge"], chunk_size=800, top_k=5)
    )
    hits = idx.search("What is the refund window?", top_k=3)
    assert hits
    assert "30 days" in hits[0].text
    assert hits[0].source == "knowledge/policy.md"

    file_search = idx.as_file_search_tool()
    tool_out = file_search("pro_plan pricing")
    assert tool_out["hits"]

    with pytest.raises(ValueError):
        KnowledgeIndex.from_manifest(
            bot_dir, KnowledgeConfig(paths=["../outside.txt"])
        )

    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret", encoding="utf-8")
    link = bot_dir / "knowledge" / "sym.md"
    try:
        os.symlink(outside, link)
    except OSError:
        return
    with pytest.raises(ValueError):
        KnowledgeIndex.from_manifest(bot_dir, KnowledgeConfig(paths=["knowledge"]))


@pytest.mark.parametrize(
    "blocked_url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8080/admin",
        "http://localhost:8000/secret",
        "http://10.0.0.5/internal",
        "http://100.64.0.1/cgnat",
        "http://0.0.0.0:9000/",
        "http://[::1]:8080/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://printer.local/api",
        "file:///etc/passwd",
    ],
)
def test_ssrf_blocks_private_metadata_and_local_urls(blocked_url: str) -> None:
    ok, reason = validate_action_url(blocked_url)
    assert not ok
    assert reason


def test_ssrf_blocks_dns_rebinding_and_allows_explicit_opt_in(monkeypatch) -> None:
    def fake_getaddrinfo(host, port, type=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.20.30.40", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    ok, reason = validate_action_url("https://rebind.example.com/hook")
    assert not ok
    assert "private IP" in reason

    ok_opt, _ = validate_action_url(
        "http://127.0.0.1:8099/hook", allow_local_urls=True
    )
    assert ok_opt


def test_openapi_action_execution_and_redirect_ssrf_block(tmp_path: Path) -> None:
    bot_dir = tmp_path / "action-bot"
    scaffold_bot("Action Bot", "openai", bot_dir)

    (bot_dir / "actions" / "orders.yaml").write_text(
        yaml.safe_dump(
            {
                "openapi": "3.0.3",
                "info": {"title": "Orders API", "version": "1.0.0"},
                "servers": [{"url": "https://api.example.com"}],
                "paths": {
                    "/orders/{order_id}": {
                        "get": {
                            "operationId": "get_order",
                            "summary": "Fetch an order by ID",
                            "parameters": [
                                {
                                    "name": "order_id",
                                    "in": "path",
                                    "required": True,
                                    "schema": {"type": "string"},
                                }
                            ],
                        }
                    },
                    "/redirect-evil": {
                        "get": {
                            "operationId": "redirect_evil",
                            "summary": "Triggers redirect to metadata IP",
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    data = yaml.safe_load((bot_dir / "bot.yaml").read_text(encoding="utf-8"))
    data["actions"] = [
        {"name": "orders", "openapi_spec": "actions/orders.yaml", "auth_type": "none"}
    ]
    (bot_dir / "bot.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/orders/ORD-42":
            return httpx.Response(200, json={"order_id": "ORD-42", "status": "shipped"})
        if request.url.path == "/redirect-evil":
            return httpx.Response(
                302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    app = create_bot_app(bot_dir, backend="mock", action_transport=transport)
    client = TestClient(app)

    # Normal action call executes through ToolRegistry -> MockTransport
    r1 = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": '[tool:get_order {"order_id": "ORD-42"}]'}],
        },
    )
    assert r1.status_code == 200
    body1 = r1.json()
    ChatCompletion.model_validate(body1)
    assert "shipped" in body1["choices"][0]["message"]["content"]

    # Redirect hop to 169.254.169.254 is blocked before following
    r2 = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "[tool:redirect_evil {}]"}],
        },
    )
    assert r2.status_code == 200
    assert "SSRF blocked redirect" in r2.json()["choices"][0]["message"]["content"]


def test_gemini_v1beta_generate_and_stream_and_ui(tmp_path: Path) -> None:
    bot_dir = tmp_path / "gem-bot"
    scaffold_bot("Support Gem", "gemini", bot_dir)
    (bot_dir / "knowledge" / "faq.md").write_text(
        "Warranty covers hardware defects for 24 months.", encoding="utf-8"
    )

    app = create_bot_app(bot_dir, backend="mock")
    client = TestClient(app)

    # Models list
    m_resp = client.get("/v1beta/models")
    assert m_resp.status_code == 200
    models = m_resp.json()["models"]
    assert any(m["name"] == "models/gemini-2.0-flash" for m in models)

    # Non-streaming generateContent validated with google.genai.types
    g_resp = client.post(
        "/v1beta/models/gemini-2.0-flash:generateContent",
        json={
            "contents": [
                {"role": "user", "parts": [{"text": "How long is the warranty?"}]}
            ]
        },
    )
    assert g_resp.status_code == 200
    g_obj = gt.GenerateContentResponse.model_validate(g_resp.json())
    assert "24 months" in (g_obj.text or "")
    assert g_obj.candidates[0].grounding_metadata is not None

    # Streaming streamGenerateContent validated with google.genai.types
    s_resp = client.post(
        "/v1beta/models/gemini-2.0-flash:streamGenerateContent?alt=sse",
        json={
            "contents": [
                {"role": "user", "parts": [{"text": "Tell me about warranty"}]}
            ]
        },
    )
    assert s_resp.status_code == 200
    events = _parse_sse_json_events(s_resp.text)
    assert len(events) >= 2
    collected = []
    for ev in events:
        assert isinstance(ev, dict)
        chunk = gt.GenerateContentResponse.model_validate(ev)
        if chunk.text:
            collected.append(chunk.text)
    assert "24 months" in "".join(collected)

    # Info & Playground UI
    info = client.get("/bot/info").json()
    assert info["style"] == "gemini"
    assert "google-genai" in info["snippets"]["gemini"] or "genai.Client" in info["snippets"]["gemini"]
    ui_resp = client.get("/bot/ui")
    assert ui_resp.status_code == 200
    assert "AitherGem" in ui_resp.text
    assert client.get("/").status_code == 200


def test_openai_v1_chat_and_assistants_sdk_compliance(tmp_path: Path) -> None:
    bot_dir = tmp_path / "gpt-bot"
    scaffold_bot("Docs GPT", "openai", bot_dir)
    (bot_dir / "knowledge" / "guide.md").write_text(
        "Deploy locally with adk bot deploy --target local.", encoding="utf-8"
    )

    app = create_bot_app(bot_dir, backend="mock")
    client = TestClient(app)

    # Models
    models = client.get("/v1/models").json()
    assert models["object"] == "list"
    assert any(m["id"] == "gpt-4o-mini" for m in models["data"])

    # Non-stream ChatCompletion
    c_resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "How do I deploy locally?"}],
        },
    )
    assert c_resp.status_code == 200
    comp = ChatCompletion.model_validate(c_resp.json())
    assert "adk bot deploy" in (comp.choices[0].message.content or "")

    # Stream ChatCompletionChunk + [DONE]
    cs_resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "How do I deploy locally?"}],
            "stream": True,
        },
    )
    assert cs_resp.status_code == 200
    events = _parse_sse_json_events(cs_resp.text)
    assert events[-1] == "[DONE]"
    chunks = [ChatCompletionChunk.model_validate(ev) for ev in events[:-1] if isinstance(ev, dict)]
    streamed_text = "".join(c.choices[0].delta.content or "" for c in chunks)
    assert "adk bot deploy" in streamed_text

    # Assistants API lifecycle
    assts = client.get("/v1/assistants").json()["data"]
    assert len(assts) >= 1
    Assistant.model_validate(assts[0])
    asst_id = assts[0]["id"]

    t_resp = client.post("/v1/threads", json={})
    assert t_resp.status_code == 200
    thread = Thread.model_validate(t_resp.json())

    m_resp = client.post(
        f"/v1/threads/{thread.id}/messages",
        json={"role": "user", "content": "How do I deploy locally?"},
    )
    assert m_resp.status_code == 200
    BetaMessage.model_validate(m_resp.json())

    r_resp = client.post(
        f"/v1/threads/{thread.id}/runs",
        json={"assistant_id": asst_id},
    )
    assert r_resp.status_code == 200
    run = Run.model_validate(r_resp.json())
    assert run.status == "completed"

    msgs = client.get(f"/v1/threads/{thread.id}/messages").json()["data"]
    parsed_msgs = [BetaMessage.model_validate(m) for m in msgs]
    assert parsed_msgs[0].role == "assistant"
    assert "adk bot deploy" in parsed_msgs[0].content[0].text.value

    # UI themed for OpenAI
    ui_resp = client.get("/bot/ui")
    assert ui_resp.status_code == 200
    assert "AitherGPT" in ui_resp.text


def test_callable_backend_503_and_mount_prefix(tmp_path: Path) -> None:
    bot_dir = tmp_path / "custom-bot"
    scaffold_bot("Custom Bot", "gemini", bot_dir, backend="gemini")

    # Missing Gemini key produces clean 503 (never unhandled 500)
    app_unconfigured = create_bot_app(bot_dir)
    c_err = TestClient(app_unconfigured)
    r_err = c_err.post(
        "/v1beta/models/gemini-2.0-flash:generateContent",
        json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
    )
    assert r_err.status_code == 503
    assert r_err.json()["error"]["code"] == 503

    # Injected callable backend + prefixed mount without shadowing root
    def custom_llm(messages, model=None, **kwargs):
        return f"callable:{model}:{messages[-1].content}"

    host_app = FastAPI()

    @host_app.get("/")
    def host_root():
        return {"host": "ok"}

    rt = BotRuntime(bot_dir, backend=custom_llm)
    mount_bot_routes(
        host_app,
        rt,
        prefix="/bots/custom",
        include_openai_chat=False,
        include_root_ui=False,
    )
    c_host = TestClient(host_app)
    assert c_host.get("/").json() == {"host": "ok"}
    r_gem = c_host.post(
        "/bots/custom/v1beta/models/gemini-2.0-flash:generateContent",
        json={"contents": [{"role": "user", "parts": [{"text": "ping"}]}]},
    )
    assert r_gem.status_code == 200
    assert "callable:gemini-2.0-flash:ping" in r_gem.text


def test_tools_module_client_function_call_and_info_secret_safety(tmp_path: Path) -> None:
    from adk.bots.onboard import save_provider_key

    bot_dir = tmp_path / "tools-and-secrets"
    scaffold_bot("Tools Bot", "gemini", bot_dir, backend="mock")
    secret_val = "AIzaSyDoNotLeakInBotInfo1234567890123456"
    save_provider_key("gemini", secret_val)

    app = create_bot_app(bot_dir, backend="mock")
    client = TestClient(app)

    # /bot/info must never expose any saved BYOK secret
    info_resp = client.get("/bot/info")
    assert info_resp.status_code == 200
    assert secret_val not in info_resp.text
    assert "get_bot_status" in info_resp.json()["registered_tools"]

    # Local tools.py function executes end-to-end
    t_resp = client.post(
        "/v1beta/models/gemini-2.0-flash:generateContent",
        json={
            "contents": [
                {"role": "user", "parts": [{"text": "[tool:get_bot_status {}]"}]}
            ]
        },
    )
    assert t_resp.status_code == 200
    g_obj = gt.GenerateContentResponse.model_validate(t_resp.json())
    assert "ready" in (g_obj.text or "")

    # Client-declared functionDeclarations returns functionCall part cleanly
    fc_resp = client.post(
        "/v1beta/models/gemini-2.0-flash:generateContent",
        json={
            "contents": [
                {"role": "user", "parts": [{"text": '[tool:lookup_weather {"city": "NYC"}]'}]}
            ],
            "tools": [
                {
                    "functionDeclarations": [
                        {
                            "name": "lookup_weather",
                            "description": "Lookup city weather",
                            "parameters": {
                                "type": "object",
                                "properties": {"city": {"type": "string"}},
                            },
                        }
                    ]
                }
            ],
        },
    )
    assert fc_resp.status_code == 200
    fc_obj = gt.GenerateContentResponse.model_validate(fc_resp.json())
    fcs = fc_obj.function_calls
    assert fcs and fcs[0].name == "lookup_weather"
    assert fcs[0].args == {"city": "NYC"}


@pytest.mark.parametrize(
    "noncanonical_url",
    [
        "http://2130706433/",
        "http://0177.0.0.1/",
        "http://0x7f.0.0.1/",
        "http://0x7f000001/",
        "http://127.1/",
        "http://0x7f.1/",
    ],
)
def test_ssrf_blocks_noncanonical_ipv4_even_with_skip_dns(
    noncanonical_url: str,
) -> None:
    ok, reason = validate_action_url(noncanonical_url, skip_dns=True)
    assert not ok
    assert "non-canonical" in reason


def test_action_executor_encodes_path_params_and_strips_cross_origin_auth(
    tmp_path: Path, monkeypatch
) -> None:
    bot_dir = tmp_path / "redirect-auth-bot"
    scaffold_bot("Redirect Auth Bot", "openai", bot_dir)
    (bot_dir / "actions" / "items.yaml").write_text(
        yaml.safe_dump(
            {
                "openapi": "3.0.3",
                "servers": [{"url": "https://public.example.com"}],
                "paths": {
                    "/orders/{order_id}": {
                        "get": {
                            "operationId": "get_order",
                            "parameters": [
                                {
                                    "name": "order_id",
                                    "in": "path",
                                    "required": True,
                                    "schema": {"type": "string"},
                                },
                                {
                                    "name": "Cookie",
                                    "in": "header",
                                    "required": False,
                                    "schema": {"type": "string"},
                                },
                            ],
                        }
                    },
                    "/bounce": {
                        "get": {
                            "operationId": "bounce",
                            "parameters": [
                                {
                                    "name": "Cookie",
                                    "in": "header",
                                    "required": False,
                                    "schema": {"type": "string"},
                                },
                            ],
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    data = yaml.safe_load((bot_dir / "bot.yaml").read_text(encoding="utf-8"))
    data["actions"] = [
        {
            "name": "items",
            "openapi_spec": "actions/items.yaml",
            "auth_type": "bearer",
            "auth_env": "TEST_BOT_ACTION_TOKEN",
        }
    ]
    (bot_dir / "bot.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("TEST_BOT_ACTION_TOKEN", "super-secret-token")

    seen_requests: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append((str(request.url), dict(request.headers)))
        if request.url.path == "/bounce":
            return httpx.Response(
                302, headers={"Location": "https://other.example.org/collect"}
            )
        return httpx.Response(200, json={"path": request.url.raw_path.decode("ascii")})

    from adk.bots.actions import ActionExecutor, load_openapi_actions

    actions = load_openapi_actions(data["actions"][0], bot_dir)
    by_name = {a.operation_id: a for a in actions}
    executor = ActionExecutor(actions, transport=httpx.MockTransport(handler))

    import asyncio

    # Path traversal segment ".." is blocked outright
    res_dotdot = asyncio.run(executor.execute_action(by_name["get_order"], order_id=".."))
    assert res_dotdot.get("blocked") is True

    # Slashes / query / fragment inside path param are percent-encoded
    res_enc = asyncio.run(
        executor.execute_action(by_name["get_order"], order_id="../../admin?x=1#y")
    )
    assert res_enc["ok"] is True
    assert seen_requests[-1][0].startswith(
        "https://public.example.com/orders/..%2F..%2Fadmin%3Fx%3D1%23y"
    )

    # Cross-origin redirect strips Authorization and Cookie headers
    seen_requests.clear()
    res_redir = asyncio.run(
        executor.execute_action(by_name["bounce"], Cookie="session=secret")
    )
    assert res_redir["ok"] is True
    assert len(seen_requests) == 2
    hop1_url, hop1_headers = seen_requests[0]
    hop2_url, hop2_headers = seen_requests[1]
    assert hop1_url == "https://public.example.com/bounce"
    assert hop1_headers.get("authorization") == "Bearer super-secret-token"
    assert hop1_headers.get("cookie") == "session=secret"
    assert hop2_url == "https://other.example.org/collect"
    assert "authorization" not in hop2_headers
    assert "cookie" not in hop2_headers
    assert "x-api-key" not in hop2_headers
    assert "proxy-authorization" not in hop2_headers


def test_runtime_malformed_json_400_and_stream_json_array_and_function_call_turn(
    tmp_path: Path,
) -> None:
    bot_dir = tmp_path / "wire-hardening-bot"
    scaffold_bot("Wire Hardening Bot", "gemini", bot_dir, backend="mock")

    captured_messages = []

    def inspecting_responder(messages, meta):
        captured_messages.clear()
        captured_messages.extend(messages)
        return f"meta_fmt={meta.get('response_mime_type')}:{meta.get('response_format')}"

    from adk.bots.runtime import MockBotBackend

    mock_backend = MockBotBackend(bot_name="Wire", responder=inspecting_responder)
    app = create_bot_app(bot_dir, backend=mock_backend)
    client = TestClient(app)

    # Malformed JSON returns 400 across endpoints (never 500)
    for path in (
        "/v1/threads",
        "/v1/assistants",
        "/v1/chat/completions",
        "/v1beta/models/gemini-2.0-flash:generateContent",
    ):
        resp = client.post(
            path,
            content="{not-valid-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400, f"{path} returned {resp.status_code}"

    # Non-dict JSON also returns 400
    assert (
        client.post(
            "/v1/threads",
            content="[]",
            headers={"Content-Type": "application/json"},
        ).status_code
        == 400
    )

    # Empty body on /v1/threads still creates a thread cleanly
    t_resp = client.post("/v1/threads", content=b"")
    assert t_resp.status_code == 200
    tid = t_resp.json()["id"]
    assert (
        client.post(
            f"/v1/threads/{tid}/messages",
            content="{bad",
            headers={"Content-Type": "application/json"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/v1/threads/{tid}/runs",
            content="{bad",
            headers={"Content-Type": "application/json"},
        ).status_code
        == 400
    )

    # :streamGenerateContent without ?alt=sse returns application/json array of GenerateContentResponse
    s_json = client.post(
        "/v1beta/models/gemini-2.0-flash:streamGenerateContent",
        json={
            "contents": [{"role": "user", "parts": [{"text": "Stream as JSON"}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        },
    )
    assert s_json.status_code == 200
    assert s_json.headers["content-type"].startswith("application/json")
    arr = s_json.json()
    assert isinstance(arr, list) and len(arr) >= 1
    for item in arr:
        gt.GenerateContentResponse.model_validate(item)

    # Multi-turn Gemini functionCall + functionResponse preserves assistant tool_calls
    fc_resp = client.post(
        "/v1beta/models/gemini-2.0-flash:generateContent",
        json={
            "contents": [
                {"role": "user", "parts": [{"text": "Check weather"}]},
                {
                    "role": "model",
                    "parts": [
                        {"functionCall": {"name": "get_weather", "args": {"city": "NYC"}}}
                    ],
                },
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": "get_weather",
                                "response": {"temp": "72F"},
                            }
                        }
                    ],
                },
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": {"type": "object"},
            },
        },
    )
    assert fc_resp.status_code == 200
    roles = [m.role for m in captured_messages]
    assert roles == ["system", "user", "assistant", "tool"]
    asst_msg = captured_messages[2]
    tool_msg = captured_messages[3]
    assert asst_msg.tool_calls and asst_msg.tool_calls[0]["function"]["name"] == "get_weather"
    assert tool_msg.tool_call_id == asst_msg.tool_calls[0]["id"]
    assert "application/json" in fc_resp.text


def test_runtime_loads_tool_packs_and_forwards_response_format_and_escapes_ui(
    tmp_path: Path, monkeypatch
) -> None:
    packs_dir = tmp_path / "packs"
    pack_dir = packs_dir / "sample_pack"
    pack_dir.mkdir(parents=True)
    (pack_dir / ".toolpack.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "sample_pack",
                "name": "Sample Pack",
                "version": "0.1.0",
                "tier": "free",
                "tool_modules": ["sample_pack"],
            }
        ),
        encoding="utf-8",
    )
    (pack_dir / "__init__.py").write_text(
        "def ping_pack(name: str = 'world') -> dict:\n"
        "    return {'pong': name}\n\n"
        "def register(registry) -> int:\n"
        "    registry.register(ping_pack, name='ping_pack', description='Ping pack')\n"
        "    return 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AITHER_TOOLPACK_DIRS", str(packs_dir))

    bot_dir = tmp_path / "pack-bot"
    scaffold_bot("Pack Bot", "openai", bot_dir, backend="mock")
    data = yaml.safe_load((bot_dir / "bot.yaml").read_text(encoding="utf-8"))
    data["tools"]["packs"] = ["sample_pack"]
    (bot_dir / "bot.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")

    seen_kwargs: dict[str, object] = {}

    def custom_callable(messages, model=None, response_format=None, **kwargs):
        seen_kwargs["response_format"] = response_format
        return '{"ok": true}'

    rt = BotRuntime(bot_dir, backend=custom_callable)
    assert rt.registry.get("ping_pack") is not None

    app = create_bot_app(bot_dir, backend=custom_callable)
    client = TestClient(app)
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "json please"}],
            "response_format": {"type": "json_object"},
        },
    )
    assert resp.status_code == 200
    assert seen_kwargs.get("response_format") == {"type": "json_object"}

    # ui.py escapes style cleanly
    from adk.bots.ui import render_playground_html

    rendered = render_playground_html(
        {
            "id": "xss",
            "name": "XSS Test",
            "style": '</script><img src=x onerror=alert(1)>',
        }
    )
    assert "<img src=x" not in rendered
    assert "&lt;img src=x" in rendered


@pytest.mark.parametrize("backend", ["awzeal", "zero-libc"])
def test_awzeal_zero_libc_fails_closed_with_503_and_works_when_mocked(
    tmp_path: Path, monkeypatch, backend: str
) -> None:
    import sys
    import types

    bot_dir = tmp_path / f"rt-{backend}"
    scaffold_bot(f"RT {backend}", "gemini", bot_dir, backend=backend)

    # Ensure adk.llm.awzeal is absent -> 503 (never falls back to cloud providers)
    monkeypatch.setitem(sys.modules, "adk.llm.awzeal", None)
    app = create_bot_app(bot_dir)
    client = TestClient(app)
    r_gem = client.post(
        "/v1beta/models/bonsai2-27b:generateContent",
        json={"contents": [{"role": "user", "parts": [{"text": "ping"}]}]},
    )
    assert r_gem.status_code == 503
    assert r_gem.json()["error"]["code"] == 503

    r_oai = client.post(
        "/v1/chat/completions",
        json={"model": "bonsai2-27b", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r_oai.status_code == 503

    # Inject a fake adk.llm.awzeal module whose provider has empty binary/endpoint -> 503
    from adk.llm.base import LLMProvider, LLMResponse

    class _UnconfiguredAwzeal(LLMProvider):
        def __init__(self, model: str = "", **kwargs):
            self.binary = ""
            self.endpoint = ""
            self.model = model

        async def list_models(self) -> list[str]:
            return [self.model]

        async def chat(self, messages, **kwargs):
            raise AssertionError("should fail closed before chat")

    fake_mod_unconf = types.ModuleType("adk.llm.awzeal")
    fake_mod_unconf.AwzealZeroLibcProvider = _UnconfiguredAwzeal  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "adk.llm.awzeal", fake_mod_unconf)
    assert (
        client.post(
            "/v1/chat/completions",
            json={"model": "bonsai2-27b", "messages": [{"role": "user", "content": "ping"}]},
        ).status_code
        == 503
    )

    # Inject a configured fake adk.llm.awzeal provider -> 200
    class _ConfiguredAwzeal(LLMProvider):
        def __init__(self, model: str = "", **kwargs):
            self.binary = "/fake/asm-llm"
            self.model = model

        async def list_models(self) -> list[str]:
            return [self.model]

        async def chat(self, messages, model=None, **kwargs):
            return LLMResponse(content="zero-libc ok", model=model or self.model)

    fake_mod_ok = types.ModuleType("adk.llm.awzeal")
    fake_mod_ok.AwzealZeroLibcProvider = _ConfiguredAwzeal  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "adk.llm.awzeal", fake_mod_ok)
    ok_resp = client.post(
        "/v1/chat/completions",
        json={"model": "bonsai2-27b", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert ok_resp.status_code == 200
    assert ok_resp.json()["choices"][0]["message"]["content"] == "zero-libc ok"


def test_knowledge_and_actions_null_byte_paths_and_streaming_cap(tmp_path: Path) -> None:
    import asyncio
    from adk.bots.actions import ActionExecutor, OpenAPIAction, load_openapi_actions
    from adk.bots.knowledge import KnowledgeIndex

    bot_dir = tmp_path / "stream-cap-bot"
    scaffold_bot("Stream Cap", "openai", bot_dir, backend="mock")

    with pytest.raises(ValueError, match="Null byte"):
        KnowledgeIndex.from_manifest(bot_dir, {"paths": ["knowledge/doc\x00.md"]})

    with pytest.raises(ValueError, match="Null byte"):
        load_openapi_actions({"name": "a", "openapi_spec": "actions/spec\x00.yaml"}, bot_dir)

    chunks_yielded = 0

    class _HugeStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal chunks_yielded
            for _ in range(1000):
                chunks_yielded += 1
                yield b"A" * 4096

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_HugeStream())

    action = OpenAPIAction(
        name="big",
        operation_id="get_big",
        method="GET",
        path="/big",
        base_url="https://public.example.com",
        description="Get big payload",
    )
    executor = ActionExecutor(
        [action],
        transport=httpx.MockTransport(handler),
        max_response_bytes=64,
    )
    res = asyncio.run(executor.execute_action(action))
    assert res["status_code"] == 200
    assert res["ok"] is True
    assert len(res["data"]) == 64
    assert chunks_yielded <= 2


