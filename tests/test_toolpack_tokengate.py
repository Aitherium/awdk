"""The TokenGate toolpack against a REAL local HTTP server.

The pack talks ``urllib`` -> HTTP, and every error shape it must surface faithfully
(an HTTPError's JSON body, a Retry-After header, a refused socket) lives BELOW any
transport mock. So each test dials a real socket on 127.0.0.1; the server is a
scripted fake of the lane contract in ``adk/toolpacks/tokengate/README.md``, not a
reimplementation of the gate. Nothing here needs a GPU.
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from adk.toolpacks.tokengate import register
from adk.toolpacks.tokengate import tools as tg
from adk.tools import ToolRegistry

PACK_DIR = Path(__file__).parent.parent / "adk" / "toolpacks" / "tokengate"


class _State:
    """Scripted routes + every request the fake lane received."""

    def __init__(self) -> None:
        self.routes: dict = {}
        self.requests: list = []
        self.lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    state: _State = None  # type: ignore[assignment] — set by the fixture

    def log_message(self, *args) -> None:  # noqa: D102 — silence the test output
        return

    def _serve(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else None
        with self.state.lock:
            self.state.requests.append({
                "method": method,
                "path": self.path,
                "headers": dict(self.headers),
                "body": body,
            })
            route = self.state.routes.get((method, self.path))
        if route is None:
            status, headers, payload = 404, {}, {"detail": {"reason": "no route"}}
        else:
            status, headers, payload = route
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's API
        self._serve("GET")

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's API
        self._serve("POST")


def _answer(question: str = "30 days", **extra) -> dict:
    payload = {
        "answer": question,
        "evidence": [{"text": "Retention is 30 days.", "start": 15, "end": 39, "kept": 6}],
        "kept_tokens": 6,
        "state_tokens": 900,
        "max_kept_tokens": 2000,
        "prefill_ms": 1.5,
        "request_ms": 3.0,
        "model": "qwen3-1.7b",
        "checkpoint": "liana313/tokengate-niah-qwen3-1.7b",
    }
    payload.update(extra)
    return payload


@pytest.fixture
def lane(monkeypatch):
    """A real lane on a real socket. Yields ``(base_url, state)``."""
    state = _State()
    handler = type("_LaneHandler", (_Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    monkeypatch.setenv("AITHER_TOKENGATE_URL", base)
    try:
        yield base, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# ── registration / manifest ──────────────────────────────────────────────────


def test_register_exposes_both_tools():
    registry = ToolRegistry()
    assert register(registry) == 2
    assert {t.name for t in registry.list_tools()} == {"tokengate_ask", "tokengate_status"}


def test_toolpack_yaml_glob_matches_every_tool_name():
    import yaml

    manifest = yaml.safe_load((PACK_DIR / ".toolpack.yaml").read_text(encoding="utf-8"))
    assert manifest["id"] == "tokengate"
    assert manifest["mcp_tools"] == ["tokengate_*"]
    assert all(name.startswith("tokengate_") for name in tg._TOOL_NAMES)


# ── ask: the happy path, both input shapes ───────────────────────────────────


def test_ask_text_returns_answer_evidence_and_budget(lane):
    base, state = lane
    state.routes[("POST", "/ask")] = (200, {}, _answer())

    document = "Project notes. Retention is 30 days."
    out = tg.tokengate_ask("How long is retention?", text=document)

    assert out["ok"] is True
    assert out["answer"] == "30 days"
    assert out["evidence"] == _answer()["evidence"]
    assert out["kept_tokens"] == 6
    assert out["state_tokens"] == 900
    assert out["checkpoint"] == "liana313/tokengate-niah-qwen3-1.7b"
    assert out["document_source"] == "text"
    assert out["document_chars"] == len(document)
    assert out["url"] == base + "/ask"
    sent = state.requests[-1]
    assert sent["method"] == "POST" and sent["path"] == "/ask"
    assert sent["body"] == {"document": document, "question": "How long is retention?"}


def test_ask_passes_max_kept_tokens_only_when_given(lane):
    _base, state = lane
    state.routes[("POST", "/ask")] = (200, {}, _answer())

    tg.tokengate_ask("q", text="doc", max_kept_tokens=500)
    assert state.requests[-1]["body"]["max_kept_tokens"] == 500

    tg.tokengate_ask("q", text="doc")
    assert "max_kept_tokens" not in state.requests[-1]["body"]


def test_ask_reads_a_file_and_reports_what_it_sent(lane, tmp_path):
    _base, state = lane
    state.routes[("POST", "/ask")] = (200, {}, _answer())

    # Bytes, not write_text: text mode would translate \n to \r\n on Windows and
    # this test would stop asserting what is actually on disk.
    content = "# Notes\n\nRetention is 30 days.\n"
    path = tmp_path / "notes.md"
    path.write_bytes(content.encode("utf-8"))
    out = tg.tokengate_ask("How long?", path=str(path))

    assert out["ok"] is True
    assert out["document_source"] == "path"
    assert out["document_path"] == str(path)
    assert out["document_chars"] == len(content)
    assert out["document_encoding"] in ("utf-8-sig", "utf-8")
    assert state.requests[-1]["body"]["document"] == content


def test_a_trailing_slash_in_the_url_is_normalised(lane, monkeypatch):
    base, state = lane
    monkeypatch.setenv("AITHER_TOKENGATE_URL", base + "/")
    state.routes[("POST", "/ask")] = (200, {}, _answer())

    out = tg.tokengate_ask("q", text="doc")
    assert out["ok"] is True
    assert out["url"] == base + "/ask"


# ── ask: the three failures the caller must be able to tell apart ────────────


def test_413_surfaces_both_token_counts(lane):
    _base, state = lane
    state.routes[("POST", "/ask")] = (413, {}, {"detail": {
        "reason": "document over the lane ceiling",
        "state_tokens": 41233,
        "max_state_tokens": 32768,
    }})

    out = tg.tokengate_ask("q", text="a long document")

    assert out["ok"] is False
    assert out["error_kind"] == "document_too_long"
    assert out["status"] == 413
    assert out["state_tokens"] == 41233
    assert out["max_state_tokens"] == 32768
    assert out["reason"] == "document over the lane ceiling"
    assert "hint" in out


def test_503_surfaces_the_lane_as_unavailable_with_retry_after(lane):
    _base, state = lane
    state.routes[("POST", "/ask")] = (
        503, {"Retry-After": "7"},
        {"detail": {"reason": "waiting for a GPU lease"}},
    )

    out = tg.tokengate_ask("q", text="doc")

    assert out["ok"] is False
    assert out["error_kind"] == "lane_unavailable"
    assert out["status"] == 503
    assert out["retry_after_s"] == "7"
    assert out["reason"] == "waiting for a GPU lease"


def test_an_unreachable_lane_is_typed_not_an_exception(monkeypatch):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("AITHER_TOKENGATE_URL", "http://127.0.0.1:%d" % port)

    out = tg.tokengate_ask("q", text="doc")

    assert out["ok"] is False
    assert out["error_kind"] == "unreachable"
    assert out["url"] == "http://127.0.0.1:%d/ask" % port
    assert "hint" in out
    assert "error" in out


def test_other_refusals_are_lane_refused(lane):
    _base, state = lane
    state.routes[("POST", "/ask")] = (400, {}, {"detail": {"reason": "bad request"}})

    out = tg.tokengate_ask("q", text="doc")

    assert out["ok"] is False
    assert out["error_kind"] == "lane_refused"
    assert out["status"] == 400
    assert out["reason"] == "bad request"


def test_a_200_without_an_answer_is_not_reported_as_success(lane):
    _base, state = lane
    state.routes[("POST", "/ask")] = (200, {}, {"kept_tokens": 3})

    out = tg.tokengate_ask("q", text="doc")

    assert out["ok"] is False
    assert out["error_kind"] == "lane_refused"


# ── ask: input validation happens BEFORE any request ─────────────────────────


def test_neither_path_nor_text_is_refused_locally(lane):
    _base, state = lane
    out = tg.tokengate_ask("q")
    assert out["ok"] is False
    assert out["error_kind"] == "bad_input"
    assert state.requests == []


def test_both_path_and_text_is_refused_locally(lane, tmp_path):
    _base, state = lane
    path = tmp_path / "a.txt"
    path.write_text("hello", encoding="utf-8")
    out = tg.tokengate_ask("q", path=str(path), text="hello")
    assert out["ok"] is False
    assert out["error_kind"] == "bad_input"
    assert state.requests == []


def test_blank_question_is_refused_locally(lane):
    _base, state = lane
    out = tg.tokengate_ask("   ", text="doc")
    assert out["ok"] is False
    assert out["error_kind"] == "bad_input"
    assert state.requests == []


def test_zero_max_kept_tokens_is_refused_locally(lane):
    _base, state = lane
    out = tg.tokengate_ask("q", text="doc", max_kept_tokens=0)
    assert out["ok"] is False
    assert out["error_kind"] == "bad_input"
    assert state.requests == []


def test_a_missing_file_is_a_visible_file_not_found(lane, tmp_path):
    _base, state = lane
    out = tg.tokengate_ask("q", path=str(tmp_path / "nope.txt"))
    assert out["ok"] is False
    assert out["error_kind"] == "file_not_found"
    assert state.requests == []


def test_a_binary_file_is_an_unsupported_document(lane, tmp_path):
    _base, state = lane
    path = tmp_path / "blob.dat"
    path.write_bytes(b"\xff\xfe\x81\x8d\x00")
    out = tg.tokengate_ask("q", path=str(path))
    assert out["ok"] is False
    assert out["error_kind"] == "unsupported_document"
    assert state.requests == []


@pytest.mark.parametrize("name", [".env", ".env.local", "server.pem", "id_rsa",
                                  "credentials.json", "store.p12"])
def test_a_credential_file_is_refused_not_shipped(lane, tmp_path, name):
    _base, state = lane
    path = tmp_path / name
    path.write_bytes(b"API_KEY=not-a-real-key\n")
    out = tg.tokengate_ask("what key is in here?", path=str(path))
    assert out["ok"] is False
    assert out["error_kind"] == "secret_file_refused"
    assert "text=" in out["hint"]
    assert state.requests == []


def test_pdf_without_an_extractor_names_the_install(monkeypatch, lane, tmp_path):
    _base, state = lane
    import adk.docconvert as docconvert

    monkeypatch.setattr(docconvert, "available_converters", lambda: [])
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-1.4")
    out = tg.tokengate_ask("q", path=str(path))
    assert out["ok"] is False
    assert out["error_kind"] == "extraction_unavailable"
    assert "awdk[docs]" in out["hint"]
    assert state.requests == []


def test_a_converted_document_is_what_gets_sent(monkeypatch, lane, tmp_path):
    _base, state = lane
    import adk.docconvert as docconvert

    monkeypatch.setattr(docconvert, "available_converters", lambda: ["pypdf"])
    monkeypatch.setattr(docconvert, "convert_document", lambda p: "extracted pdf text")
    state.routes[("POST", "/ask")] = (200, {}, _answer())

    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-1.4")
    out = tg.tokengate_ask("q", path=str(path))

    assert out["ok"] is True
    assert out["document_encoding"] == "converted:pdf"
    assert state.requests[-1]["body"]["document"] == "extracted pdf text"


# ── status ───────────────────────────────────────────────────────────────────


def test_status_reports_the_lanes_health(lane):
    base, state = lane
    health = {"status": "ok", "loaded": True, "checkpoint": "cp",
              "lease": {"held": True, "vram_mb": 8192}}
    state.routes[("GET", "/health")] = (200, {}, health)

    out = tg.tokengate_status()

    assert out["ok"] is True
    assert out["reachable"] is True
    assert out["url"] == base + "/health"
    assert out["health"] == health


def test_status_reports_a_503_as_unavailable(lane):
    _base, state = lane
    state.routes[("GET", "/health")] = (
        503, {"Retry-After": "3"}, {"detail": {"reason": "loading"}})

    out = tg.tokengate_status()

    assert out["ok"] is False
    assert out["error_kind"] == "lane_unavailable"
    assert out["retry_after_s"] == "3"


def test_status_on_an_unreachable_lane_is_typed(monkeypatch):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("AITHER_TOKENGATE_URL", "http://127.0.0.1:%d" % port)

    out = tg.tokengate_status()

    assert out["ok"] is False
    assert out["error_kind"] == "unreachable"


# ── awsh harness sessions reach the same tools ──────────────────────────────


def test_awsh_mcp_server_lists_every_tokengate_tool():
    from adk.harnesses import mcp_stdio

    names = {t["name"] for t in mcp_stdio.TOOLS}
    assert set(tg._TOOL_NAMES) <= names


def test_awsh_tools_call_reaches_the_lane(lane):
    _base, state = lane
    from adk.harnesses import mcp_stdio

    state.routes[("GET", "/health")] = (200, {}, {"status": "ok", "loaded": True})
    resp = mcp_stdio._handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "tokengate_status", "arguments": {}},
    })
    out = json.loads(resp["result"]["content"][0]["text"])
    assert out["ok"] is True
    assert out["health"]["loaded"] is True

