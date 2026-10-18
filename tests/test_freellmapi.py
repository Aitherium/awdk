"""FreeLLMAPI as the non-local route: settings, detection, routing order, lifecycle."""

from __future__ import annotations

import json
import subprocess

import httpx
import pytest

from adk import freellmapi as fl

PING = {"status": "ok", "timestamp": "2026-10-18T00:00:00Z"}
MODELS = {"object": "list", "data": [{"id": "auto"}, {"id": "fusion"},
                                     {"id": "llama-3.3-70b-versatile"}, {"id": "gemini-2.5-flash"}]}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _proxy(key: str = "freellmapi-test", models: dict = MODELS):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/ping":
            return httpx.Response(200, json=PING)
        if req.url.path == "/v1/models":
            if req.headers.get("authorization") != f"Bearer {key}":
                return httpx.Response(401, json={"error": {"message": "Invalid API key",
                                                           "type": "authentication_error"}})
            return httpx.Response(200, json=models)
        return httpx.Response(404)
    return handler


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    mods = tmp_path / "mods.json"
    monkeypatch.setenv(fl.ENV_MODS_FILE, str(mods))
    for var in (fl.ENV_ENABLED, fl.ENV_URL, fl.ENV_MODELS, fl.ENV_KEY):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(fl.Path, "home", classmethod(lambda cls: tmp_path))
    return mods


# -- settings ---------------------------------------------------------------


def test_off_by_default(isolated):
    assert fl.load_settings().enabled is False


def test_save_keeps_other_mods_keys_and_env_overrides(isolated, monkeypatch):
    isolated.write_text(json.dumps({"version": 1, "aw": {"harness": "codex"}}), encoding="utf-8")
    fl.save_settings(enabled=True, url="http://10.0.0.5:3001", preferred_models=["a", "b"])
    data = json.loads(isolated.read_text(encoding="utf-8"))
    assert data["aw"] == {"harness": "codex"}
    assert data["freellmapi"] == {"enabled": True, "url": "http://10.0.0.5:3001",
                                  "preferred_models": ["a", "b"]}
    monkeypatch.setenv(fl.ENV_ENABLED, "0")
    monkeypatch.setenv(fl.ENV_MODELS, "x, y")
    s = fl.load_settings()
    assert s.enabled is False and s.preferred_models == ["x", "y"]


def test_save_refuses_a_malformed_mods_file_and_unknown_fields(isolated):
    isolated.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError):
        fl.save_settings(enabled=True)
    assert isolated.read_text(encoding="utf-8") == "{ not json"
    with pytest.raises(ValueError):
        fl.save_settings(api_key="nope")


def test_the_unified_key_is_never_written_to_the_synced_settings(isolated, monkeypatch):
    monkeypatch.setenv(fl.ENV_KEY, "freellmapi-secret")
    fl.save_settings(enabled=True)
    assert "freellmapi-secret" not in isolated.read_text(encoding="utf-8")
    assert fl.resolve_api_key() == "freellmapi-secret"


def test_key_falls_back_to_the_local_key_store(isolated, tmp_path):
    kf = tmp_path / "provider_keys.json"
    kf.write_text(json.dumps({"freellmapi": "freellmapi-stored"}), encoding="utf-8")
    assert fl.resolve_api_key(env={}, keys_file=kf) == "freellmapi-stored"


# -- detection --------------------------------------------------------------


def test_candidates_configured_first_then_default_ports():
    s = fl.FreeLLMAPISettings(url="http://box:3001/v1")
    assert fl.candidate_urls(s) == [("http://box:3001", "configured"),
                                    ("http://127.0.0.1:3001", "port"),
                                    ("http://127.0.0.1:31415", "port")]


async def test_detects_and_lists_models_with_the_key():
    async with _client(_proxy()) as c:
        d = await fl.probe("http://127.0.0.1:3001", "freellmapi-test", client=c)
    assert d.found and d.authorized
    assert "llama-3.3-70b-versatile" in d.models
    assert d.base_url == "http://127.0.0.1:3001/v1"


async def test_found_but_unauthorized_without_the_key():
    async with _client(_proxy()) as c:
        d = await fl.probe("http://127.0.0.1:3001", "", client=c)
    assert d.found and not d.authorized and "no unified key" in d.detail


async def test_a_generic_dev_server_on_3001_is_not_freellmapi():
    def handler(req):
        return httpx.Response(200, text="<html>vite</html>")
    async with _client(handler) as c:
        d = await fl.probe("http://127.0.0.1:3001", "k", client=c)
    assert not d.found


def test_callable_models_drops_router_entries():
    assert fl.callable_models(["auto", "fusion", "auto:smart", "m1"]) == ["m1"]


def test_pick_model_prefers_the_users_choice_else_auto():
    ids = [m["id"] for m in MODELS["data"]]
    assert fl.pick_model(ids, ["missing", "gemini-2.5-flash"]) == "gemini-2.5-flash"
    assert fl.pick_model(ids, ["missing"]) == "auto"


# -- routing order ----------------------------------------------------------


def _fake_detect(det):
    async def _d(*a, **k):
        return det
    return _d


async def test_router_skips_freellmapi_unless_enabled(isolated, monkeypatch):
    from adk.llm import LLMRouter
    monkeypatch.setenv(fl.ENV_KEY, "freellmapi-test")
    called = []

    async def _never(*a, **k):
        called.append(1)
        return fl.Detection(True, "http://127.0.0.1:3001", authorized=True, models=["m"])
    monkeypatch.setattr(fl, "detect", _never)
    assert await LLMRouter()._try_freellmapi() is None
    assert not called, "disabled must not even probe"


async def test_router_adopts_enabled_freellmapi_with_a_ready_model(isolated, monkeypatch):
    from adk.llm import LLMRouter
    fl.save_settings(enabled=True, preferred_models=["gemini-2.5-flash"])
    monkeypatch.setenv(fl.ENV_KEY, "freellmapi-test")
    monkeypatch.setattr(fl, "detect", _fake_detect(fl.Detection(
        True, "http://127.0.0.1:3001", authorized=True,
        models=["auto", "gemini-2.5-flash"])))
    r = LLMRouter()
    p = await r._try_freellmapi()
    assert p is not None and r.provider_name == "freellmapi"
    assert p.default_model == "gemini-2.5-flash"


async def test_router_refuses_a_proxy_with_no_provider_key_connected(isolated, monkeypatch):
    from adk.llm import LLMRouter
    fl.save_settings(enabled=True)
    monkeypatch.setenv(fl.ENV_KEY, "freellmapi-test")
    monkeypatch.setattr(fl, "detect", _fake_detect(fl.Detection(
        True, "http://127.0.0.1:3001", authorized=True, models=["auto", "fusion"])))
    assert await LLMRouter()._try_freellmapi() is None


async def test_local_wins_and_freellmapi_beats_paid_keys(isolated, monkeypatch):
    """local model first, freellmapi next, paid/BYO API last."""
    from adk.llm import LLMRouter
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    sentinel_local, sentinel_free = object(), object()

    async def none(self):
        return None

    async def local(self):
        self._provider_name = "local"
        return sentinel_local

    async def free(self):
        self._provider_name = "freellmapi"
        return sentinel_free

    monkeypatch.setattr(LLMRouter, "_try_desktop", none)
    monkeypatch.setattr(LLMRouter, "_try_vllm", none)
    monkeypatch.setattr(LLMRouter, "_try_freellmapi", free)
    monkeypatch.setattr(LLMRouter, "_try_local_selfhost", local)
    assert await LLMRouter()._auto_detect() is sentinel_local
    monkeypatch.setattr(LLMRouter, "_try_local_selfhost", none)
    assert await LLMRouter()._auto_detect() is sentinel_free
    monkeypatch.setattr(LLMRouter, "_try_freellmapi", none)
    r = LLMRouter()
    await r._auto_detect()
    assert r.provider_name == "anthropic"


# -- lifecycle (the free-cloud pack) -----------------------------------------


class FakeRuntime:
    def __init__(self, state=None):
        self.state = state
        self.calls: list[list[str]] = []

    def __call__(self, args, **kw):
        self.calls.append(args)
        verb = args[1]
        if verb == "inspect":
            if self.state is None:
                return subprocess.CompletedProcess(args, 1, "", "no such container")
            return subprocess.CompletedProcess(
                args, 0, f"{str(self.state['running']).lower()}|{self.state['image']}\n", "")
        if verb == "run":
            self.state = {"running": True, "image": fl.PINNED_IMAGE}
        if verb == "rm":
            self.state = None
        return subprocess.CompletedProcess(args, 0, "", "")


def test_up_runs_the_pinned_digest_on_loopback_with_a_named_volume(tmp_path):
    rt = FakeRuntime()
    envf = tmp_path / "fl.env"
    rc, _ = fl.up(runtime="docker", env_file=envf, run=rt)
    run = next(c for c in rt.calls if c[1] == "run")
    assert rc == 0
    assert fl.PINNED_IMAGE in run and "@sha256:" in fl.PINNED_IMAGE
    assert "127.0.0.1:3001:3001" in run
    assert f"{fl.VOLUME}:{fl.DATA_MOUNT}" in run
    assert envf.read_text(encoding="utf-8").startswith("ENCRYPTION_KEY=")


def test_up_is_idempotent_and_never_rotates_the_encryption_key(tmp_path):
    rt = FakeRuntime()
    envf = tmp_path / "fl.env"
    fl.up(runtime="docker", env_file=envf, run=rt)
    first = envf.read_text(encoding="utf-8")
    rc, msg = fl.up(runtime="docker", env_file=envf, run=rt)
    assert rc == 0 and "already running" in msg
    assert sum(1 for c in rt.calls if c[1] == "run") == 1
    assert envf.read_text(encoding="utf-8") == first


def test_down_removes_the_container_never_the_volume():
    rt = FakeRuntime({"running": True, "image": fl.PINNED_IMAGE})
    rc, msg = fl.down(runtime="docker", run=rt)
    assert rc == 0 and fl.VOLUME in msg
    flat = [" ".join(c) for c in rt.calls]
    assert not any("volume" in c for c in flat)
    assert any(c.startswith("docker rm -f") for c in flat)
