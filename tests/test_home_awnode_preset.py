"""``adk home model --local awnode``: the local awnode gateway as Agent Home's model.

awnode serves an OpenAI-compatible ``/v1`` on 127.0.0.1:8090 and resolves the
unpinned model ``auto`` to whatever its local backend serves. The trap this preset
must not fall into: awnode answers ``GET /v1/models`` with **200 and an empty list**
when no backend is up, so a status-code-only probe reports a model that 503s on the
first chat.
"""

from __future__ import annotations

import httpx
import pytest
from adk.home import cli as home_cli
from adk.home import config as hc
from adk.home import models


class _Resp:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(hc.HOME_ENV, str(tmp_path / "agent-home"))
    hc.init_home(name="pip")
    return tmp_path / "agent-home"


def test_awnode_is_a_local_preset_on_loopback_with_auto_model():
    assert "awnode" in models.LOCAL
    cfg = models.choose_model("awnode")
    assert (cfg.mode, cfg.base_url, cfg.model) == ("local", models.AWNODE_URL, "auto")
    assert cfg.api_key_env == ""
    assert models.AWNODE_URL.startswith("http://127.0.0.1:8090/")


def test_awnode_builds_an_openai_compatible_router_without_a_key():
    router = models.build_llm(models.choose_model("awnode"))
    assert type(router).__name__ == "LLMRouter"


def test_probe_empty_model_list_is_not_ok(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, timeout: _Resp(200, {"data": []}))
    out = models.probe(models.choose_model("awnode"))
    assert out["ok"] is False
    assert "serves no model" in out["detail"]


def test_probe_lists_served_models(monkeypatch):
    seen = {}

    def fake_get(url, timeout):
        seen["url"] = url
        return _Resp(200, {"object": "list", "data": [
            {"id": "bonsai-selfhost", "object": "model", "owned_by": "bonsai"},
            {"id": "gemma4:4b", "object": "model", "owned_by": "ollama"}]})

    monkeypatch.setattr(httpx, "get", fake_get)
    out = models.probe(models.choose_model("awnode"))
    assert out["ok"] is True
    assert out["models"] == ["bonsai-selfhost", "gemma4:4b"]
    assert seen["url"] == "http://127.0.0.1:8090/v1/models"
    assert "bonsai, ollama" in out["detail"]


def test_probe_non_json_200_is_not_ok(monkeypatch):
    monkeypatch.setattr(httpx, "get",
                        lambda url, timeout: _Resp(200, ValueError("html")))
    assert models.probe(models.choose_model("awnode"))["ok"] is False


def test_probe_unreachable_names_how_to_start_awnode(monkeypatch):
    def refuse(url, timeout):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", refuse)
    out = models.probe(models.choose_model("awnode"))
    assert out["ok"] is False and "awnode start" in out["detail"]


def test_other_local_presets_keep_status_only_probe(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, timeout: _Resp(200, {"data": []}))
    assert models.probe(models.choose_model("llamacpp"))["ok"] is True


def test_cli_model_local_awnode_saves_and_check_fails_on_empty(home, monkeypatch, capsys):
    monkeypatch.setattr(httpx, "get", lambda url, timeout: _Resp(200, {"data": []}))
    assert home_cli.main(["model", "--local", "awnode"]) == 0
    assert hc.load_config().model.provider == "awnode"
    assert home_cli.main(["model", "--check"]) == 1


def test_against_the_real_awnode_app_with_no_backend(monkeypatch):
    """The empty-list shape is awnode's, not a guess: ask its real /v1/models."""
    server = pytest.importorskip("awnode.server")
    from fastapi.testclient import TestClient

    for flag in ("bonsai", "vllm", "llamacpp", "ollama"):
        monkeypatch.setattr(server._state, flag, False, raising=False)
    client = TestClient(server.app)
    monkeypatch.setattr(httpx, "get", lambda url, timeout: client.get(
        "/v1/" + url.split("/v1/", 1)[1]))
    out = models.probe(models.choose_model("awnode"))
    assert out["ok"] is False and "serves no model" in out["detail"]
