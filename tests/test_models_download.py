"""`adk.models.download` against a server that behaves like the real mirror.

Measured 2026-10-02 on weights.aitherium.com: HEAD states the true size, ranges work,
and a plain GET can end early with HTTP 200 and no Content-Length. ``_Dropping`` is that
server (the same misbehaving server the shell installer's download test drives), so a fragment can only become "the model" if the code under test
skips its size check.
"""
from __future__ import annotations

import hashlib
import http.server
import socketserver
import threading

import pytest
from adk.models import download

SIZE = 10240
BLOB = bytes(i % 251 for i in range(SIZE))
SHA = hashlib.sha256(BLOB).hexdigest()


class _Dropping(http.server.BaseHTTPRequestHandler):
    """HEAD states the size; a GET carries no Content-Length and ends cleanly after
    ``cap`` bytes. Ranges are honoured unless ``ranges`` is off."""

    blob = BLOB
    cap = 1500
    ranges = True
    head_size = True
    gets: list = []

    def log_message(self, *args):  # noqa: D102 - silence the test output
        pass

    def do_HEAD(self):  # noqa: N802 - http.server naming
        self.send_response(200)
        if self.head_size:
            self.send_header("Content-Length", str(len(self.blob)))
        self.end_headers()

    def do_GET(self):  # noqa: N802 - http.server naming
        start = 0
        rng = self.headers.get("Range", "")
        type(self).gets.append(rng)
        if self.ranges and rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0])
            self.send_response(206)
            self.send_header("Content-Range",
                             f"bytes {start}-{len(self.blob) - 1}/{len(self.blob)}")
        else:
            self.send_response(200)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(self.blob[start:start + self.cap])


@pytest.fixture()
def serve():
    servers = []

    def _serve(**attrs):
        handler = type("_H", (_Dropping,), dict(attrs, gets=[]))
        srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return handler, f"http://127.0.0.1:{srv.server_address[1]}/blob.gguf"

    yield _serve
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def _quiet(_msg):
    pass


def test_stream_dropped_mid_file_is_resumed_to_the_whole_file(tmp_path, serve):
    """A GET that ends at 1500 of 10240 bytes with HTTP 200 must not become the model."""
    handler, url = serve()
    dest = tmp_path / "models" / "m.gguf"
    said = []
    assert download.fetch([url], dest, sha256=SHA, say=said.append) == dest
    assert dest.read_bytes() == BLOB
    assert not (tmp_path / "models" / "m.gguf.part").exists(), "a .part was left behind"
    assert len(handler.gets) == 7, "10240 bytes at 1500 per stream is seven requests"
    assert handler.gets[0] == "" and handler.gets[1] == "bytes=1500-"
    assert any("stream ended early" in s for s in said)


def test_fragment_that_never_grows_fails_and_leaves_no_part(tmp_path, serve):
    """A server that cannot resume: fail, and never install or keep the fragment."""
    handler, url = serve(ranges=False)
    dest = tmp_path / "m.gguf"
    with pytest.raises(download.DownloadError, match="no progress"):
        download.fetch([url], dest, say=_quiet)
    assert not dest.exists(), "a fragment was installed as the file"
    assert not (tmp_path / "m.gguf.part").exists(), "an unresumable fragment was kept"
    assert len(handler.gets) == 1 + download.MAX_STALLS


def test_size_is_checked_against_head_not_against_what_arrived(tmp_path, serve):
    """With no catalogue size, HEAD is the only truth -- and it is used."""
    _, url = serve(cap=SIZE)
    dest = tmp_path / "m.gguf"
    download.fetch([url], dest, say=_quiet)
    assert dest.stat().st_size == SIZE == download.head(url)


def test_head_disagreeing_with_the_catalogue_is_refused(tmp_path, serve):
    """A mirror serving a different file under the name: nothing is downloaded."""
    handler, url = serve()
    with pytest.raises(download.DownloadError, match="the catalogue says 999"):
        download.fetch([url], tmp_path / "m.gguf", size_bytes=999, say=_quiet)
    assert handler.gets == []


def test_no_stated_size_anywhere_is_refused(tmp_path, serve):
    handler, url = serve(head_size=False)
    with pytest.raises(download.DownloadError, match="cannot be checked"):
        download.fetch([url], tmp_path / "m.gguf", say=_quiet)
    assert handler.gets == [] and not (tmp_path / "m.gguf").exists()


def test_catalogue_size_is_used_when_head_is_silent(tmp_path, serve):
    _, url = serve(head_size=False)
    dest = tmp_path / "m.gguf"
    download.fetch([url], dest, size_bytes=SIZE, sha256=SHA, say=_quiet)
    assert dest.read_bytes() == BLOB


def test_sha256_mismatch_discards_the_download(tmp_path, serve):
    _, url = serve()
    dest = tmp_path / "m.gguf"
    with pytest.raises(download.DownloadError, match="sha256 mismatch"):
        download.fetch([url], dest, sha256="0" * 64, say=_quiet)
    assert not dest.exists() and not (tmp_path / "m.gguf.part").exists()


def test_existing_part_is_resumed_not_restarted(tmp_path, serve):
    handler, url = serve(cap=SIZE)
    dest = tmp_path / "m.gguf"
    (tmp_path / "m.gguf.part").write_bytes(BLOB[:4096])
    download.fetch([url], dest, sha256=SHA, say=_quiet)
    assert dest.read_bytes() == BLOB and handler.gets == ["bytes=4096-"]


def test_already_complete_part_is_adopted_without_a_request(tmp_path, serve):
    """The 416 case: a .part that IS the whole file is verified and renamed."""
    handler, url = serve()
    dest = tmp_path / "m.gguf"
    (tmp_path / "m.gguf.part").write_bytes(BLOB)
    download.fetch([url], dest, sha256=SHA, say=_quiet)
    assert dest.read_bytes() == BLOB and handler.gets == []


def test_stale_oversized_part_is_discarded(tmp_path, serve):
    _, url = serve(cap=SIZE)
    dest = tmp_path / "m.gguf"
    (tmp_path / "m.gguf.part").write_bytes(b"B" * (SIZE * 4))
    download.fetch([url], dest, sha256=SHA, say=_quiet)
    assert dest.read_bytes() == BLOB


def test_verified_file_on_disk_is_not_fetched_again(tmp_path, serve):
    handler, url = serve()
    dest = tmp_path / "m.gguf"
    dest.write_bytes(BLOB)
    download.fetch([url], dest, sha256=SHA, say=_quiet)
    assert handler.gets == []


def test_corrupt_file_on_disk_is_replaced(tmp_path, serve):
    """Right size, wrong bytes: the digest catches it and it is fetched again."""
    _, url = serve(cap=SIZE)
    dest = tmp_path / "m.gguf"
    dest.write_bytes(b"X" * SIZE)
    download.fetch([url], dest, sha256=SHA, say=_quiet)
    assert dest.read_bytes() == BLOB


def test_dead_first_url_falls_through_to_the_next(tmp_path, serve):
    _, url = serve(cap=SIZE)
    dest = tmp_path / "m.gguf"
    download.fetch(["http://127.0.0.1:9/never.gguf", url], dest, size_bytes=SIZE,
                   sha256=SHA, say=_quiet, timeout=5)
    assert dest.read_bytes() == BLOB


def test_dead_url_fails_and_installs_nothing(tmp_path):
    dest = tmp_path / "m.gguf"
    with pytest.raises(download.DownloadError):
        download.fetch(["http://127.0.0.1:9/never.gguf"], dest, size_bytes=SIZE,
                       say=_quiet, timeout=5)
    assert not dest.exists()


def test_fetch_model_single_file_reports_whether_it_was_hashed(tmp_path, serve):
    _, url = serve()
    model = {"id": "m", "file": "m.gguf", "urls": [url], "join": False, "sha256": SHA,
             "size_bytes": SIZE}
    path, hashed = download.fetch_model(model, tmp_path, say=_quiet)
    assert path.read_bytes() == BLOB and hashed is True
    del model["sha256"]
    (tmp_path / "m.gguf").unlink()
    assert download.fetch_model(model, tmp_path, say=_quiet)[1] is False


def test_fetch_model_joins_release_slices_and_checks_the_whole(tmp_path, serve):
    half = SIZE // 2
    _, a = serve(blob=BLOB[:half])
    _, b = serve(blob=BLOB[half:])
    model = {"id": "m", "file": "m.gguf", "urls": [a, b], "join": True, "sha256": SHA,
             "size_bytes": SIZE}
    path, hashed = download.fetch_model(model, tmp_path, say=_quiet)
    assert path.read_bytes() == BLOB and hashed
    assert sorted(p.name for p in tmp_path.iterdir()) == ["m.gguf"], "slices were left behind"
    # A joined file that is not what the catalogue describes is refused, not installed.
    path.unlink()
    with pytest.raises(download.DownloadError, match="sha256 mismatch after joining"):
        download.fetch_model(dict(model, sha256="0" * 64), tmp_path, say=_quiet)
    assert not path.exists()
