"""The four model endpoints, asserted against GobboNet's OWN reader.

These tests do not check "is it JSON". They check the specific keys
`js/02-model.js` dereferences, because a response that is well-formed and
missing `file` renders an empty dropdown — which looks like "no models
installed" rather than like a broken integration, and would ship.

Driven over real HTTP through `serve()` rather than by calling ModelManager
directly: the routing is half of what is being claimed, and a method that
returns the right dict from a route nobody reaches is worth nothing.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest
from adk.packs.gobbonet.models import ModelManager, _classify, _pretty
from adk.packs.gobbonet.server import Engine, serve


@pytest.fixture()
def models_dir(tmp_path: Path) -> Path:
    d = tmp_path / "models"
    d.mkdir()
    for name in (
        "DeepSeek-R1-Distill-8B-Q4_K_M.gguf",
        "gemma-3-12b-it-Q5_K_M.gguf",
        "Mistral-Small-Q4_K_M.gguf",
        # Sharded: only the first shard is a model you can pick.
        "Big-Model-Q4-00001-of-00003.gguf",
        "Big-Model-Q4-00002-of-00003.gguf",
        "Big-Model-Q4-00003-of-00003.gguf",
    ):
        (d / name).write_bytes(b"GGUF")
    return d


@pytest.fixture()
def client(tmp_path: Path, models_dir: Path):
    mgr = ModelManager(models_dir=models_dir, port=1, _spawn=lambda p, port: None)
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<html></html>", encoding="utf-8")
    httpd = serve(ui, Engine(), port=0, state=tmp_path / "state.json", models=mgr)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def get(path: str) -> dict:
        with urllib.request.urlopen(base + path, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def post(path: str, body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            base + path, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    yield get, post, mgr
    httpd.shutdown()


# ── /models-list.json ────────────────────────────────────────────────────────
def test_every_key_their_option_builder_reads_is_present(client):
    get, _, _ = client
    models = get("/models-list.json")["models"]
    assert models, "an empty list renders as 'no models' and would pass a laxer test"
    for m in models:
        # Exactly the dereferences in js/02-model.js. Named individually so a
        # failure says WHICH key upstream would have read as undefined.
        for key in ("file", "name", "id", "family", "thinkingFormat", "active"):
            assert key in m, f"upstream reads m.{key}"
        assert m["file"].endswith(".gguf")


def test_sharded_weights_appear_once_as_the_first_shard(client):
    get, _, _ = client
    files = [m["file"] for m in get("/models-list.json")["models"]]
    shards = [f for f in files if "Big-Model" in f]
    assert shards == ["Big-Model-Q4-00001-of-00003.gguf"], (
        "listing every shard puts fragments in the picker; selecting one loads "
        "a partial model and fails with no useful message")


def test_thinking_format_is_not_uniformly_none(client):
    """The rigged-metric guard: defaulting everything to `none` would pass a
    'the key exists' test while leaking <think> blocks into every chat."""
    get, _, _ = client
    formats = {m["thinkingFormat"] for m in get("/models-list.json")["models"]}
    assert "deepseek" in formats
    assert formats != {"none"}


# ── /active-model.json ───────────────────────────────────────────────────────
def test_active_model_is_honest_when_nothing_is_loaded(client):
    get, _, _ = client
    active = get("/active-model.json")
    for key in ("id", "name", "ggufFile", "thinkingFormat"):
        assert key in active
    # Naming a model that is not loaded is worse than saying so: the user picks
    # a character, sends a message, and gets an error naming a different model.
    assert active["ggufFile"] == ""


def test_active_model_reports_the_loaded_file_after_a_swap(client):
    get, post, mgr = client
    mgr._wait_until_answering = staticmethod(lambda port, deadline: True)
    status, _ = post("/swap-model", {"file": "gemma-3-12b-it-Q5_K_M.gguf"})
    assert status == 200
    _await_phase(get, "ready")
    active = get("/active-model.json")
    assert active["ggufFile"] == "gemma-3-12b-it-Q5_K_M.gguf"
    assert active["thinkingFormat"] == "gemma"
    assert [m for m in get("/models-list.json")["models"] if m["active"]][0]["file"] \
        == "gemma-3-12b-it-Q5_K_M.gguf"


# ── /swap-model + /swap-status ───────────────────────────────────────────────
def test_an_unknown_file_is_refused_with_a_message_not_a_silent_200(client):
    get, post, _ = client
    status, body = post("/swap-model", {"file": "not-installed.gguf"})
    assert status == 400
    # `message` is the field their error path reads; anything else renders
    # "undefined" in their toast.
    assert "message" in body and "not-installed.gguf" in body["message"]


def test_ready_means_answering_not_merely_spawned(client):
    """The trap this exists for: reporting ready when the process started makes
    the user's first message hang with no explanation. A model that is loading
    accepts the socket long before it can answer."""
    get, post, mgr = client
    mgr._wait_until_answering = staticmethod(lambda port, deadline: False)
    post("/swap-model", {"file": "Mistral-Small-Q4_K_M.gguf"})
    final = _await_phase(get, "error")
    assert final["phase"] == "error"
    assert final.get("message")


def test_a_timeout_cites_the_last_probe_error(client):
    """A bare "did not answer in time" is the same message whether the process
    died on startup or is merely slow, and those need opposite responses. Runs
    the REAL probe against a dead port so the error is a real one."""
    get, post, mgr = client
    real = ModelManager._wait_until_answering
    mgr._wait_until_answering = lambda port, deadline: real(mgr, port, 2)
    post("/swap-model", {"file": "Mistral-Small-Q4_K_M.gguf"})
    final = _await_phase(get, "error", tries=120)
    assert "did not answer in time" in final["message"]
    # Nothing listens on port 1, so the probe must name a connection failure.
    assert "URLError" in final["message"] or "Error" in final["message"], final


def test_a_failing_spawn_reaches_the_ui_rather_than_a_log(client):
    get, post, mgr = client

    def boom(path, port):
        raise RuntimeError("llama-server is not installed")

    mgr._spawn = boom
    post("/swap-model", {"file": "Mistral-Small-Q4_K_M.gguf"})
    final = _await_phase(get, "error")
    assert "llama-server is not installed" in final["message"]


def test_a_second_swap_while_loading_is_refused(client):
    get, post, mgr = client
    mgr._wait_until_answering = staticmethod(_slow_true)
    post("/swap-model", {"file": "Mistral-Small-Q4_K_M.gguf"})
    status, body = post("/swap-model", {"file": "gemma-3-12b-it-Q5_K_M.gguf"})
    assert status == 400
    assert "already loading" in body["message"]


def test_status_is_pollable_before_any_swap(client):
    """Their poller may run on load. `idle` must be a real answer, not a 404."""
    get, _, _ = client
    assert get("/swap-status")["phase"] == "idle"


# ── the pure functions ───────────────────────────────────────────────────────
@pytest.mark.parametrize("name,family,thinking", [
    ("DeepSeek-R1-Q4.gguf", "deepseek", "deepseek"),
    ("Qwen3-8B-Q4.gguf", "qwen", "deepseek"),
    ("gemma-3-12b.gguf", "gemma", "gemma"),
    ("something-nobody-has-heard-of.gguf", "custom", "none"),
])
def test_classification(name, family, thinking):
    assert _classify(name) == (family, thinking)


def test_pretty_strips_the_shard_suffix():
    assert _pretty("Big-Model-Q4-00001-of-00003.gguf") == "Big Model Q4"


def _slow_true(port, deadline):
    import time
    time.sleep(2.0)
    return True


def _await_phase(get, want: str, tries: int = 40) -> dict:
    import time
    for _ in range(tries):
        st = get("/swap-status")
        if st["phase"] == want:
            return st
        time.sleep(0.1)
    raise AssertionError(f"phase never reached {want}; last was {st}")
