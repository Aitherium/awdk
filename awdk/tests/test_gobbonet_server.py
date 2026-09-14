"""The GobboNet server answers the endpoints that client's UI actually calls.

Every case here is a behaviour the UI depends on, and most of them fail SILENTLY
when wrong — which is why they are pinned rather than left to a manual poke:

  * /health non-200          -> the UI switches to its OFFLINE screen
  * /state 404 forever       -> "sync error: HTTP 404" and nothing persists
  * /v1/embeddings wrong     -> the semantic half of the hybrid retriever returns
                                nothing, the tag half carries on, and retrieval
                                quality drops with NO error anywhere
  * max_tokens: -1 forwarded -> a completion with zero tokens and a clean [DONE],
                                indistinguishable from a broken model
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from adk.packs.gobbonet.server import Engine, FileState, NotConfigured, serve


class _Stub(Engine):
    def __init__(self) -> None:
        self.opts: dict = {}

    def stream_chat(self, messages, **opts):
        self.opts = opts
        yield "Hello"
        yield ", world"

    def models(self):
        return [{"id": "local-model", "object": "model"}]


@pytest.fixture()
def ui(tmp_path: Path) -> Path:
    (tmp_path / "chat.html").write_text("<html>gobbonet</html>", encoding="utf-8")
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "01-config.js").write_text("// cfg", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def server(ui: Path):
    stub = _Stub()
    httpd = serve(ui, stub, port=0)  # port 0 -> the OS picks a free one
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, stub, httpd
    httpd.shutdown()


def _get(url: str):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read().decode()


def _post(url: str, obj: dict):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, r.read().decode()


def test_health_is_200_or_the_ui_goes_offline(server):
    base, _, _ = server
    status, body = _get(base + "/health")
    assert status == 200
    assert json.loads(body)["status"] == "ok"


def test_serves_the_clients_own_files_unmodified(server):
    base, _, _ = server
    assert _get(base + "/chat.html")[1] == "<html>gobbonet</html>"
    assert _get(base + "/js/01-config.js")[1] == "// cfg"


def test_path_traversal_is_refused(server):
    base, _, _ = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base + "/../../etc/passwd")
    assert exc.value.code == 404


def test_state_round_trips(server):
    """404 -> POST -> GET is the exact sequence the UI performs on first run."""
    base, _, _ = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base + "/state")
    assert exc.value.code == 404, "the UI reads 404 as 'no backup yet' and seeds"

    _post(base + "/state", {"threads": [{"id": "t1"}]})
    status, body = _get(base + "/state")
    assert status == 200
    assert json.loads(body)["threads"] == [{"id": "t1"}]


def test_info_404s_so_the_client_seeds(server):
    """A wrong answer here suppresses the seeding POST and nothing persists."""
    base, _, _ = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base + "/info")
    assert exc.value.code == 404


def test_chat_streams_sse_and_terminates(server):
    base, _, _ = server
    _, raw = _post(base + "/v1/chat/completions",
                   {"messages": [{"role": "user", "content": "hi"}]})
    text = "".join(
        json.loads(line[6:])["choices"][0]["delta"].get("content", "")
        for line in raw.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    )
    assert text == "Hello, world"
    assert raw.strip().endswith("[DONE]"), "an unterminated stream hangs the UI"


def test_negative_max_tokens_is_dropped_not_forwarded(server):
    """-1 means 'no limit' to several clients; forwarded literally it asks for
    minus-one tokens and yields an empty completion with a clean [DONE] — which
    reads as a broken model rather than a bad request."""
    base, stub, _ = server
    _post(base + "/v1/chat/completions",
          {"messages": [{"role": "user", "content": "hi"}],
           "max_tokens": -1, "temperature": 0.4})
    assert "max_tokens" not in stub.opts
    assert stub.opts["temperature"] == 0.4, "valid options must still pass through"


def test_zero_max_tokens_is_also_dropped(server):
    base, stub, _ = server
    _post(base + "/v1/chat/completions",
          {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 0})
    assert "max_tokens" not in stub.opts


def test_unconfigured_capability_reports_rather_than_faking(server):
    """The failure this guards against is an EMPTY answer that looks like a real
    one. Every unwired capability must say so."""
    base, _, _ = server
    for path, payload in (("/v1/embeddings", {"input": "x"}),
                          ("/web_search", {"query": "x"})):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(base + path, payload)
        assert exc.value.code == 503
        assert "not configured" in exc.value.read().decode()


def test_models_uses_the_openai_shape(server):
    base, _, _ = server
    body = json.loads(_get(base + "/v1/models")[1])
    assert body["object"] == "list"
    assert body["data"][0]["id"] == "local-model"


def test_file_state_is_atomic_and_survives_reload(tmp_path: Path):
    store = FileState(tmp_path / "s.json")
    assert store.load() is None
    store.save({"a": 1})
    assert FileState(tmp_path / "s.json").load() == {"a": 1}


def test_corrupt_state_raises_rather_than_reading_as_empty(tmp_path: Path):
    """Reporting a corrupt store as 'no backup yet' would make the client seed a
    fresh one straight over the user's history."""
    p = tmp_path / "s.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        FileState(p).load()


def test_not_configured_names_the_capability():
    assert NotConfigured("embeddings").what == "embeddings"
