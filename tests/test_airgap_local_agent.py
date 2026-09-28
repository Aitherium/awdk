"""examples/airgap_local_agent.py: local model on loopback, sealed process, cited rows.

A stub OpenAI-compatible server on 127.0.0.1 stands in for llama.cpp. The example
runs as a child process (its own guard state), so these tests prove the whole path:
discover the model, stream a completion, validate page citations, write evidence.
The egress case points the example at TEST-NET-3 and must be refused by the guard
(exit 1, verdict egress-blocked) without the stub ever seeing a request.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

AWDK_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = AWDK_ROOT / "examples" / "airgap_local_agent.py"
_STRIP = ("AITHER_AIR_GAP", "AITHER_AIR_GAP_CONFIG", "AITHER_LLM_BASE_URL", "AITHER_MODEL",
          "AITHER_DATA_DIR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
          "https_proxy", "all_proxy")


class _Stub:
    """OpenAI-compatible /v1/models + streaming /v1/chat/completions."""

    def __init__(self, answer: str):
        self.answer = answer
        self.requests: list = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def _json(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                stub.requests.append(("GET", self.path))
                if self.path.rstrip("/") == "/v1/models":
                    self._json({"object": "list", "data": [{"id": "bonsai-stub"}]})
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                stub.requests.append(("POST", self.path, body))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for piece in (stub.answer[:10], stub.answer[10:]):
                    chunk = {"id": "x", "object": "chat.completion.chunk", "model": "bonsai-stub",
                             "choices": [{"index": 0, "delta": {"content": piece},
                                          "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


GOOD = json.dumps({"rows": [{"claim": "The pump is rated 100 hours", "page": 1},
                            {"claim": "Warranty is 24 calendar months", "page": 2}]})
BAD_PAGE = json.dumps({"rows": [{"claim": "Invented", "page": 9}]})


@pytest.fixture
def corpus(tmp_path):
    d = tmp_path / "corpus"
    d.mkdir()
    (d / "manual.txt").write_text("The pump is rated 100 hours.\fWarranty: 24 calendar months.",
                                  encoding="utf-8")
    return d


def _run(corpus: Path, out: Path, base_url: str, *extra: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP}
    env["PYTHONPATH"] = str(AWDK_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["AITHER_DATA_DIR"] = str(out.parent / "data")
    env["AITHER_AIR_GAP_CONFIG"] = str(out.parent / "absent.yaml")  # example seals itself
    env["NO_PROXY"] = "*"
    return subprocess.run([sys.executable, str(EXAMPLE), "--corpus", str(corpus),
                           "--out", str(out), "--base-url", base_url, "--timeout", "10",
                           *extra], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=180, env=env,
                          cwd=str(AWDK_ROOT))


def test_local_model_ok(corpus, tmp_path):
    stub = _Stub(GOOD)
    try:
        out = tmp_path / "ev.json"
        p = _run(corpus, out, stub.url)
    finally:
        stub.close()
    assert p.returncode == 0, p.stdout + p.stderr
    ev = json.loads(out.read_text(encoding="utf-8"))
    assert ev["verdict"] == "ok"
    assert ev["model"] == "bonsai-stub"
    assert ev["air_gap"] == {"mode": "strict", "sealed": True}
    assert ev["egress_violations"] == []
    assert [(r["source"], r["page"]) for r in ev["rows"]] == [("manual.txt", 1),
                                                              ("manual.txt", 2)]
    assert len(ev["sha256_inputs"]["manual.txt"]) == 64
    assert ev["ttft_s"] is not None and ev["wall_s"] is not None
    posts = [r for r in stub.requests if r[0] == "POST"]
    assert posts and posts[0][1].endswith("/chat/completions")
    assert posts[0][2]["stream"] is True and posts[0][2]["model"] == "bonsai-stub"


def test_citation_outside_document_fails(corpus, tmp_path):
    stub = _Stub(BAD_PAGE)
    try:
        out = tmp_path / "ev.json"
        p = _run(corpus, out, stub.url)
    finally:
        stub.close()
    assert p.returncode == 1, p.stdout + p.stderr
    ev = json.loads(out.read_text(encoding="utf-8"))
    assert ev["verdict"] == "fail" and ev["rows"] == []
    assert any("outside" in x for x in ev["problems"])


def test_remote_base_url_is_blocked_by_the_guard(corpus, tmp_path):
    out = tmp_path / "ev.json"
    p = _run(corpus, out, "http://203.0.113.1:8199/v1")
    assert p.returncode == 1, p.stdout + p.stderr
    ev = json.loads(out.read_text(encoding="utf-8"))
    assert ev["verdict"] == "egress-blocked"
    assert ev["egress_violations"] and "203.0.113.1" in ev["egress_violations"][0]["detail"]
    assert "airgap_local_agent.py" in ev["egress_violations"][0]["caller"]


def test_no_model_is_exit_2(corpus, tmp_path):
    out = tmp_path / "ev.json"
    p = _run(corpus, out, "http://127.0.0.1:1/v1")
    assert p.returncode == 2, p.stdout + p.stderr
    assert json.loads(out.read_text(encoding="utf-8"))["verdict"] == "no-model"


def test_unsealable_process_refuses(corpus, tmp_path):
    """No guard available -> exit 2 'unsealed', never evidence that looks air-gapped."""
    shim = tmp_path / "shim"
    (shim / "adk" / "compliance").mkdir(parents=True)
    (shim / "adk" / "__init__.py").write_text("", encoding="utf-8")
    (shim / "adk" / "compliance" / "__init__.py").write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in _STRIP}
    env["PYTHONPATH"] = str(shim)
    out = tmp_path / "ev.json"
    p = subprocess.run([sys.executable, str(EXAMPLE), "--corpus", str(corpus), "--out",
                        str(out), "--base-url", "http://127.0.0.1:1/v1"],
                       capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120, env=env,
                       cwd=str(tmp_path))
    assert p.returncode == 2, p.stdout + p.stderr
    ev = json.loads(out.read_text(encoding="utf-8"))
    assert ev["verdict"] == "unsealed" and ev["air_gap"]["sealed"] is False


def test_audit_mode_is_not_sealed(corpus, tmp_path):
    """Audit records and lets the dial through: never report it as sealed."""
    env = {k: v for k, v in os.environ.items() if k not in _STRIP}
    env["PYTHONPATH"] = str(AWDK_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["AITHER_DATA_DIR"] = str(tmp_path / "data")
    env["AITHER_AIR_GAP_CONFIG"] = str(tmp_path / "absent.yaml")
    env["AITHER_AIR_GAP"] = "audit"
    out = tmp_path / "ev.json"
    p = subprocess.run([sys.executable, str(EXAMPLE), "--corpus", str(corpus), "--out",
                        str(out), "--base-url", "http://127.0.0.1:1/v1"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=120, env=env, cwd=str(AWDK_ROOT))
    assert p.returncode == 2, p.stdout + p.stderr
    ev = json.loads(out.read_text(encoding="utf-8"))
    assert ev["verdict"] == "unsealed"
    assert ev["air_gap"] == {"mode": "audit", "sealed": False}


def test_recorded_violations_fail_the_run(tmp_path, monkeypatch):
    """An audit-mode violation during the run turns 'ok' into exit 1, in process."""
    import importlib.util

    from adk.compliance import air_gap as ag

    spec = importlib.util.spec_from_file_location("airgap_example", EXAMPLE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    monkeypatch.setenv("AITHER_DATA_DIR", str(tmp_path))
    prev = ag.set_enforcer(ag.AirGapEnforcer(enabled=True, mode="audit"))
    try:
        base = mod._violation_count()
        assert mod._violations_since(base) == []
        ag.get_air_gap_enforcer().enforce_destination("http://203.0.113.9/x")  # records only
        got = mod._violations_since(base)
        assert len(got) == 1 and "203.0.113.9" in json.dumps(got)
    finally:
        ag.set_enforcer(prev)
