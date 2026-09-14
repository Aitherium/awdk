"""probe_inference: the ladder, the explicit-url rule, and what the heartbeat carries.

Stub HTTP servers on ephemeral loopback ports stand in for llama-server, awnode,
Ollama and vLLM. The rule under test is awnode's: only a 200 on ``/v1/models``
proves an OpenAI-compatible server (Ollama proves itself with ``/api/tags``, awnode
needs ``/health`` too).
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Iterator, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adk import enrollment, fleet_enroll
from adk.enrollment import (
    Candidate,
    InferenceProbe,
    build_registration,
    default_candidates,
    heartbeat_loop,
    probe_inference,
)

Routes = Dict[str, Tuple[int, object]]

LLAMA_ROUTES: Routes = {
    "/v1/models": (200, {"object": "list", "data": [{"id": "tiny-model-a"}]}),
    "/health": (200, {"status": "ok"}),
    "/props": (200, {"total_slots": 1}),
}
AWNODE_ROUTES: Routes = {
    "/v1/models": (200, {"data": [{"id": "node-model-b"}]}),
    "/health": (200, {"status": "healthy", "service": "awnode", "mode": "llamacpp"}),
}
OLLAMA_ROUTES: Routes = {
    "/api/tags": (200, {"models": [{"name": "local-model-d"}, {"model": "local-model-e"}]}),
    "/v1/models": (200, {"data": [{"id": "local-model-d"}]}),
}
VLLM_ROUTES: Routes = {
    "/v1/models": (200, {"data": [{"id": "served-model-c"}]}),
    "/version": (200, {"version": "0.9"}),
}


def _make_handler(routes: Routes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            hit = routes.get(self.path.split("?")[0])
            if hit is None:
                self.send_response(404)
                self.end_headers()
                return
            status, body = hit
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_a):  # silence
            return

    return Handler


class _Stub:
    def __init__(self, routes: Routes):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(routes))
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub() -> Iterator:
    """Factory fixture: ``stub(routes) -> _Stub``; every stub is shut down after."""
    made = []

    def _make(routes: Routes) -> _Stub:
        s = _Stub(routes)
        made.append(s)
        return s

    yield _make
    for s in made:
        s.close()


def _dead_port() -> int:
    """A loopback port nothing listens on (bound then released)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _dead_url() -> str:
    return f"http://127.0.0.1:{_dead_port()}"


# ---------------------------------------------------------------------------
# ladder shape
# ---------------------------------------------------------------------------


def test_default_candidates_order_and_bonsai_port(monkeypatch):
    monkeypatch.setenv("BONSAI_PORT", "9123")
    ladder = default_candidates()
    assert [(c.url.rsplit(":", 1)[1], c.kind) for c in ladder] == [
        ("9123", "llama-server"),
        ("8099", "llama-server"),
        ("8090", "awnode"),
        ("11434", "ollama"),
        ("8120", "vllm"),
    ]


def test_default_candidates_default_bonsai_port_is_8080(monkeypatch):
    monkeypatch.delenv("BONSAI_PORT", raising=False)
    assert default_candidates()[0] == Candidate("http://127.0.0.1:8080", "llama-server")


def test_default_candidates_dedups_a_repeated_port(monkeypatch):
    monkeypatch.setenv("BONSAI_PORT", "8099")
    urls = [c.url for c in default_candidates()]
    assert len(urls) == len(set(urls)) == 4


# ---------------------------------------------------------------------------
# each kind is proven by ITS check
# ---------------------------------------------------------------------------


def test_llama_server_on_8099_slot_detected_as_llama_server(stub):
    s = stub(LLAMA_ROUTES)
    got = probe_inference(None, candidates=[Candidate(s.url, "llama-server")])
    assert got == InferenceProbe(["tiny-model-a"], s.url, "llama-server", True)


def test_awnode_on_8090_slot_detected_as_awnode(stub):
    s = stub(AWNODE_ROUTES)
    got = probe_inference(None, candidates=[Candidate(s.url, "awnode")])
    assert got.inference_kind == "awnode"
    assert got.ready is True
    assert got.inference_url == s.url
    assert got.models == ["node-model-b"]


def test_awnode_slot_without_health_is_not_awnode(stub):
    """A bare OpenAI server on the awnode port is not awnode: /health is required."""
    s = stub({"/v1/models": (200, {"data": [{"id": "x"}]})})
    got = probe_inference(None, candidates=[Candidate(s.url, "awnode")])
    assert got.ready is False and got.inference_kind == "none"


def test_ollama_slot_uses_api_tags(stub):
    s = stub(OLLAMA_ROUTES)
    got = probe_inference(None, candidates=[Candidate(s.url, "ollama")])
    assert got.inference_kind == "ollama"
    assert got.models == ["local-model-d", "local-model-e"]


def test_vllm_slot_detected(stub):
    s = stub(VLLM_ROUTES)
    got = probe_inference(None, candidates=[Candidate(s.url, "vllm")])
    assert got.inference_kind == "vllm" and got.ready


def test_a_200_that_is_not_json_does_not_prove_a_server(stub):
    """A web page on the port answers 200; that is not an inference server."""

    class Html(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b"<html>hi</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Html)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        got = probe_inference(None, candidates=[Candidate(url, "llama-server")])
        assert got.ready is False
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# order + the explicit-url rule
# ---------------------------------------------------------------------------


def test_probe_order_first_live_rung_wins(stub):
    llama = stub(LLAMA_ROUTES)
    awnode = stub(AWNODE_ROUTES)
    got = probe_inference(None, candidates=[
        Candidate(_dead_url(), "llama-server"),
        Candidate(llama.url, "llama-server"),
        Candidate(awnode.url, "awnode"),
    ])
    assert got.inference_url == llama.url and got.inference_kind == "llama-server"


def test_nothing_listening_is_not_ready():
    got = probe_inference(None, candidates=[
        Candidate(_dead_url(), "llama-server"),
        Candidate(_dead_url(), "awnode"),
        Candidate(_dead_url(), "ollama"),
    ])
    assert got == InferenceProbe([], "", "none", False)
    assert tuple(got) == ([], "", "none", False)


def test_explicit_url_wins_over_the_ladder(stub):
    llama = stub(LLAMA_ROUTES)
    awnode = stub(AWNODE_ROUTES)
    got = probe_inference(awnode.url, candidates=[Candidate(llama.url, "llama-server")])
    assert got.inference_url == awnode.url
    assert got.inference_kind == "awnode"
    assert got.ready is True


def test_explicit_dead_url_is_kept_and_never_falls_back(stub):
    llama = stub(LLAMA_ROUTES)
    dead = _dead_url()
    got = probe_inference(dead, candidates=[Candidate(llama.url, "llama-server")])
    assert got == InferenceProbe([], dead, "none", False)


def test_explicit_url_fingerprints_llama_server_and_strips_v1(stub):
    s = stub(LLAMA_ROUTES)
    got = probe_inference(s.url + "/v1/")
    assert got.inference_url == s.url
    assert got.inference_kind == "llama-server"


def test_explicit_url_fingerprints_ollama_and_vllm(stub):
    assert probe_inference(stub(OLLAMA_ROUTES).url).inference_kind == "ollama"
    assert probe_inference(stub(VLLM_ROUTES).url).inference_kind == "vllm"


def test_explicit_unfingerprinted_openai_server_reports_llama_server(stub):
    s = stub({"/v1/models": (200, {"data": [{"id": "gpt-ish"}]})})
    got = probe_inference(s.url)
    assert got.ready and got.inference_kind == "llama-server"


def test_auto_string_means_walk_the_ladder(stub):
    s = stub(LLAMA_ROUTES)
    got = probe_inference("auto", candidates=[Candidate(s.url, "llama-server")])
    assert got.inference_url == s.url


# ---------------------------------------------------------------------------
# registration payload + heartbeat
# ---------------------------------------------------------------------------


@pytest.fixture
def no_hardware_probe(monkeypatch):
    """Skip the real hardware probe (slow, machine-specific)."""
    import adk.hardware_probe as hp

    def _boom():
        raise RuntimeError("stubbed")

    monkeypatch.setattr(hp, "detect_system", _boom)


def test_build_registration_carries_the_contract_fields(stub, no_hardware_probe):
    s = stub(LLAMA_ROUTES)
    reg = build_registration("adk-test-1", inference_url=s.url, node_class="phone")
    assert reg["inference_url"] == s.url
    assert reg["inference_kind"] == "llama-server"
    assert reg["node_class"] == "phone"
    assert reg["inference_ready"] is True
    assert reg["available_models"] == ["tiny-model-a"]
    assert reg["ollama_available"] is False and reg["vllm_available"] is False


def test_build_registration_rejects_an_unknown_class(no_hardware_probe):
    with pytest.raises(ValueError):
        build_registration("adk-test-2", inference_url=_dead_url(), node_class="toaster")


def test_build_registration_nothing_found(no_hardware_probe):
    reg = build_registration(
        "adk-test-3", inference_url=_dead_url(), node_class="laptop")
    assert reg["inference_ready"] is False and reg["inference_kind"] == "none"


def test_heartbeat_reprobes_the_persisted_url_and_sends_it(stub, no_hardware_probe):
    s = stub(LLAMA_ROUTES)
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"status": "ok"}

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None
        client.post.return_value = response
        client_cls.return_value = client

        asyncio.run(heartbeat_loop(
            "https://identity.test/", "tok", "adk-hb-1",
            interval=0, inference_url=s.url, node_class="phone", max_beats=1,
        ))

    client.post.assert_called_once()
    url = client.post.call_args[0][0]
    body = client.post.call_args[1]["json"]
    assert url == "https://identity.test/v1/nodes/heartbeat"
    assert body["inference_url"] == s.url
    assert body["inference_kind"] == "llama-server"
    assert body["inference_ready"] is True
    assert body["node_id"] == "adk-hb-1"


def test_heartbeat_goes_not_ready_when_the_persisted_server_dies(no_hardware_probe):
    dead = _dead_url()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {}

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None
        client.post.return_value = response
        client_cls.return_value = client
        asyncio.run(heartbeat_loop(
            "https://identity.test", "tok", "adk-hb-2",
            interval=0, inference_url=dead, max_beats=1,
        ))

    body = client.post.call_args[1]["json"]
    assert body["inference_url"] == dead
    assert body["inference_ready"] is False and body["inference_kind"] == "none"


# ---------------------------------------------------------------------------
# fleet_enroll: persisted url reaches the heartbeat; refusals are not papered over
# ---------------------------------------------------------------------------


@pytest.fixture
def aither_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_enroll, "_AITHER_DIR", tmp_path)
    monkeypatch.setattr(fleet_enroll, "_NODE_AUTH_FILE", tmp_path / "node_auth.json")
    monkeypatch.setattr(fleet_enroll, "_AUTH_FILE", tmp_path / "auth.json")
    monkeypatch.setattr(fleet_enroll, "_CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(fleet_enroll, "_AGENTS_FILE", tmp_path / "agents.json")
    monkeypatch.setattr(fleet_enroll, "_heartbeat_task", None)
    monkeypatch.setenv("AITHER_FLEET_ENROLL", "1")
    monkeypatch.setenv("AITHER_ENROLL_BASE", "https://identity.test")
    return tmp_path


def test_already_enrolled_restarts_rich_heartbeat_with_persisted_url(aither_dir):
    (aither_dir / "node_auth.json").write_text(json.dumps({
        "node_id": "adk-persisted",
        "mode": "rich",
        "inference_url": "http://127.0.0.1:4242",
        "inference_kind": "llama-server",
        "node_class": "phone",
        "enroll_base": "https://identity.persisted",
    }), encoding="utf-8")
    captured = {}

    async def fake_heartbeat(base, token, node_id, **kw):
        captured.update(base=base, node_id=node_id, **kw)

    async def run():
        with patch.object(enrollment, "heartbeat_loop", fake_heartbeat):
            result = await fleet_enroll.enroll_on_boot(enable_heartbeat=True)
            await asyncio.sleep(0)  # let the task start
        return result

    result = asyncio.run(run())
    assert result["already_registered"] is True
    assert result["inference_url"] == "http://127.0.0.1:4242"
    assert captured["inference_url"] == "http://127.0.0.1:4242"
    assert captured["node_class"] == "phone"
    assert captured["node_id"] == "adk-persisted"
    assert captured["base"] == "https://identity.persisted"


def test_rich_success_persists_the_url_the_probe_settled_on(aither_dir):
    async def fake_rich(base, token, node_id, **kw):
        return {
            "enrolled": True,
            "node_id": node_id,
            "workspace": {},
            "workspace_id": "ws-1",
            "public_url": "https://gateway.test/nodes/" + node_id,
            "registration": {
                "inference_url": "http://127.0.0.1:8099",
                "inference_kind": "llama-server",
                "inference_ready": True,
                "node_class": kw.get("node_class"),
            },
        }

    async def nothing(*_a, **_k):
        return True

    with patch.object(enrollment, "rich_enroll", fake_rich), \
            patch.object(fleet_enroll, "_upsert_agents_to_portal", nothing), \
            patch.object(fleet_enroll, "_sync_entitled_packs_best_effort",
                         AsyncMock(return_value=(0, 0))):
        result = asyncio.run(fleet_enroll.enroll_on_boot(
            enable_heartbeat=False, inference_url="auto", node_class="phone"))

    assert result["enrolled"] is True
    saved = json.loads((aither_dir / "node_auth.json").read_text(encoding="utf-8"))
    assert saved["inference_url"] == "http://127.0.0.1:8099"
    assert saved["inference_kind"] == "llama-server"
    assert saved["node_class"] == "phone"
    assert saved["public_url"].endswith(result["node_id"])
    assert "_heartbeat_started" not in saved


@pytest.mark.parametrize("status,detail", [
    (402, "subscription_required"),
    (403, "device_quota_exceeded"),
    (401, "invalid token"),
])
def test_identity_refusal_is_surfaced_verbatim_not_federated(aither_dir, status, detail):
    body = json.dumps({"detail": detail, "upgrade_url": "https://aitherium.com/pricing"})

    async def fake_rich(base, token, node_id, **kw):
        return {"enrolled": False, "error": f"HTTP {status}", "http_status": status,
                "body": body, "url": base + "/v1/nodes/register"}

    async def must_not_run(*_a, **_k):
        raise AssertionError("federation fallback ran after an identity refusal")

    with patch.object(enrollment, "rich_enroll", fake_rich), \
            patch.object(fleet_enroll, "_register_node_with_federation", must_not_run), \
            patch.object(fleet_enroll, "_register_node_with_genesis", must_not_run):
        result = asyncio.run(fleet_enroll.enroll_on_boot(enable_heartbeat=False))

    assert result["enrolled"] is False
    assert result["http_status"] == status
    assert result["body"] == body
    assert not (aither_dir / "node_auth.json").exists()


def test_unreachable_identity_still_falls_back(aither_dir):
    """No answer at all (offline, older control plane) keeps the legacy path."""

    async def fake_rich(base, token, node_id, **kw):
        return {"enrolled": False, "error": "connection refused"}

    async def fed(hub_url, api_key, node_id=None):
        return {"node_id": node_id or "fed-1", "api_key": api_key, "hub_url": hub_url}

    with patch.object(enrollment, "rich_enroll", fake_rich), \
            patch.object(fleet_enroll, "_register_node_with_federation", fed), \
            patch.object(fleet_enroll, "_upsert_agents_to_portal", AsyncMock(return_value=True)), \
            patch.object(fleet_enroll, "_sync_entitled_packs_best_effort",
                         AsyncMock(return_value=(0, 0))):
        result = asyncio.run(fleet_enroll.enroll_on_boot(enable_heartbeat=False))
    assert result["enrolled"] is True


# ---------------------------------------------------------------------------
# auth.json: the layout `adk login` writes must be the layout enrollment reads
# ---------------------------------------------------------------------------


def test_load_auth_config_flattens_the_active_login_profile(aither_dir):
    (aither_dir / "auth.json").write_text(json.dumps({
        "version": 1,
        "active_profile": "cloud",
        "profiles": {
            "local": {"access_token": "aither_root_local", "is_local_root": True},
            "cloud": {"access_token": "tok-cloud", "user": {"tenant_slug": "acme"}},
        },
    }), encoding="utf-8")
    auth = fleet_enroll._load_auth_config()
    assert auth["access_token"] == "tok-cloud"
    assert auth["tenant_slug"] == "acme"
    assert fleet_enroll._extract_tenant_slug() == "acme"


def test_load_auth_config_root_placeholder_is_not_an_identity(aither_dir):
    (aither_dir / "auth.json").write_text(json.dumps({
        "version": 1, "active_profile": "local",
        "profiles": {"local": {"access_token": "aither_root_local", "is_local_root": True}},
    }), encoding="utf-8")
    assert fleet_enroll._load_auth_config() == {}


def test_load_auth_config_legacy_flat_file_passes_through(aither_dir):
    (aither_dir / "auth.json").write_text(
        json.dumps({"access_token": "flat-tok", "tenant_slug": "flat"}), encoding="utf-8")
    assert fleet_enroll._load_auth_config()["access_token"] == "flat-tok"
