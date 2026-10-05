"""MirrorClient.download must resume a stream that drops mid-file and land the exact size.

Before: a mid-body drop ended download() with a short .partial; within the 1% size
tolerance a truncated GGUF even passed _verify.
"""
import http.server
import threading

import pytest

from adk.models import mirror

PAYLOAD = bytes(range(256)) * 4000  # 1,024,000 bytes


class _Flaky(http.server.BaseHTTPRequestHandler):
    drops = 2  # first N responses die mid-body

    def do_GET(self):
        start = 0
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
        body = PAYLOAD[start:]
        self.send_response(206 if rng else 200)
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if _Flaky.drops > 0:
            _Flaky.drops -= 1
            self.wfile.write(body[: len(body) // 3])
            self.wfile.flush()
            self.connection.shutdown(2)  # drop mid-body
            return
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def flaky_mirror(monkeypatch):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Flaky)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(mirror, "MIRROR_BASE_URL", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setitem(mirror.CATALOG, "t.gguf", mirror.WeightCatalogEntry(
        filename="t.gguf", human_name="t", family="t", quantization="Q8_0",
        approx_size_bytes=len(PAYLOAD), min_vram_gb=0))
    monkeypatch.setattr(mirror.time, "sleep", lambda s: None)
    _Flaky.drops = 2
    yield
    srv.shutdown()


def test_download_resumes_after_mid_stream_drops(flaky_mirror, tmp_path):
    client = mirror.MirrorClient(rate_limit_bytes_per_sec=0)
    out = client.download("t.gguf", str(tmp_path / "t.gguf"))
    assert open(out, "rb").read() == PAYLOAD


def test_download_gives_up_when_no_progress(flaky_mirror, tmp_path, monkeypatch):
    monkeypatch.setattr(_Flaky, "drops", 10**6)
    monkeypatch.setattr(mirror.MirrorClient, "_stream_download", lambda self, r, f: None)
    with pytest.raises(mirror.MirrorError):
        mirror.MirrorClient(rate_limit_bytes_per_sec=0).download("t.gguf", str(tmp_path / "t.gguf"))
