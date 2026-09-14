"""The catalog: what you COULD have, and the checks that make it safe to fetch.

The claims under test are the ones whose failure is SILENT. A wrong size does
not raise — it produces a truncated GGUF that loads far enough to look real and
dies in the loader hours later, naming the model rather than the transfer. A
recommendation built from a failed memory probe is indistinguishable from a
real one. A picker that lists a model the mirror will not serve fails only on
the user's machine, after the download.

Every download test runs against a local HTTP server rather than a mock, because
the behaviour being claimed is Range/resume/206 negotiation, and a mock of a
thing is not evidence about the thing.
"""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path

import pytest
from adk.packs.gobbonet import catalog


# ── a real, tiny, range-capable origin ────────────────────────────────────────
class _Origin(http.server.BaseHTTPRequestHandler):
    payload = b""
    #: When True the server IGNORES Range and sends the whole body — the
    #: behaviour `download()` has to survive without corrupting a resume.
    ignore_range = False
    status_override = 0

    def log_message(self, *a):  # noqa: D102 - silence the test run
        pass

    def do_GET(self):  # noqa: N802 - stdlib contract
        if self.status_override:
            self.send_error(self.status_override)
            return
        total = len(self.payload)
        rng = self.headers.get("Range")
        if rng and not self.ignore_range:
            start = int(rng.split("=")[1].split("-")[0])
            end_s = rng.split("-")[1]
            end = int(end_s) if end_s else total - 1
            body = self.payload[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        else:
            body = self.payload
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def origin():
    _Origin.payload = bytes(range(256)) * 40  # 10240 bytes, non-uniform
    _Origin.ignore_range = False
    _Origin.status_override = 0
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Origin)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}/model.gguf"
    srv.shutdown()


def _entry(url: str, size: int = 10240) -> catalog.CatalogEntry:
    return catalog.CatalogEntry(
        filename="model.gguf", label="Test", params_b=1.0,
        size_bytes=size, min_ram_gb=1.0, url=url,
    )


# ── the shipped list ──────────────────────────────────────────────────────────
def test_catalog_is_not_empty_and_is_sorted_smallest_first():
    es = catalog.entries()
    assert es, "an empty catalog is the failure this module exists to prevent"
    assert [e.size_bytes for e in es] == sorted(e.size_bytes for e in es)


def test_every_entry_declares_a_real_size_and_a_reachable_shape():
    for e in catalog.CATALOG:
        assert e.size_bytes > 1_000_000, f"{e.filename}: implausible size"
        assert e.filename.endswith(".gguf")
        assert e.resolve_url().startswith("https://")
        assert e.min_ram_gb > 0, f"{e.filename}: curated entries must be judgeable"


def test_smallest_entry_fits_a_modest_machine():
    """A curated list whose smallest entry needs a GPU excludes most visitors."""
    smallest = catalog.entries()[0]
    assert catalog.fits(smallest, ram_gb=4.0)


# ── sizing ────────────────────────────────────────────────────────────────────
def test_fits_uses_the_larger_of_ram_and_vram():
    """llama.cpp runs CPU-only; sizing off VRAM alone excludes workstations."""
    e = _entry("http://x", size=1)
    e.min_ram_gb = 16.0
    assert catalog.fits(e, ram_gb=64.0, vram_gb=0.0)
    assert catalog.fits(e, ram_gb=4.0, vram_gb=24.0)
    assert not catalog.fits(e, ram_gb=8.0, vram_gb=8.0)


def test_unknown_requirement_does_not_fit_anything():
    """An arbitrary HF pick has no min_ram_gb. Answering True on no
    information is how a picker recommends something that gets OOM-killed."""
    e = _entry("http://x")
    e.min_ram_gb = 0.0
    assert not catalog.fits(e, ram_gb=1024.0, vram_gb=1024.0)


def test_recommended_returns_none_when_the_probe_read_nothing():
    """0.0 GB means the memory probe failed. It must NOT silently become
    'recommend the smallest', which is indistinguishable from a real answer."""
    assert catalog.recommended(ram_gb=0.0, vram_gb=0.0) is None


def test_recommended_picks_the_largest_that_fits():
    big = catalog.recommended(ram_gb=512.0)
    small = catalog.recommended(ram_gb=2.0)
    assert big is not None and small is not None
    assert big.size_bytes >= small.size_bytes
    assert catalog.fits(small, ram_gb=2.0)


def test_recommended_is_none_when_nothing_fits():
    assert catalog.recommended(ram_gb=0.5) is None


# ── download ──────────────────────────────────────────────────────────────────
def test_download_writes_the_file_and_reports_progress(origin, tmp_path):
    seen = []
    p = catalog.download(_entry(origin), dest_dir=tmp_path,
                         progress=lambda d, t: seen.append((d, t)))
    assert p.read_bytes() == _Origin.payload
    assert seen and seen[-1] == (10240, 10240)
    assert not (tmp_path / "model.gguf.part").exists(), "part file left behind"


def test_download_refuses_a_size_mismatch_and_installs_nothing(origin, tmp_path):
    """The declared size is the truncation detector. A file that is not exactly
    it must never reach the models folder."""
    with pytest.raises(catalog.SizeMismatchError):
        catalog.download(_entry(origin, size=99999), dest_dir=tmp_path)
    assert not (tmp_path / "model.gguf").exists()
    assert not (tmp_path / "model.gguf.part").exists()


def test_download_resumes_from_a_partial_file(origin, tmp_path):
    part = tmp_path / "model.gguf.part"
    part.write_bytes(_Origin.payload[:4000])
    p = catalog.download(_entry(origin), dest_dir=tmp_path)
    assert p.read_bytes() == _Origin.payload, "resume produced the wrong bytes"


def test_resume_survives_a_host_that_ignores_range(origin, tmp_path):
    """A host answering 200 to a Range request is about to send the WHOLE file.
    Appending it to the partial one yields an oversized file that no size check
    can repair — so the transfer must restart instead."""
    (tmp_path / "model.gguf.part").write_bytes(_Origin.payload[:4000])
    _Origin.ignore_range = True
    p = catalog.download(_entry(origin), dest_dir=tmp_path)
    assert p.read_bytes() == _Origin.payload


def test_existing_file_of_the_right_size_is_not_refetched(origin, tmp_path):
    (tmp_path / "model.gguf").write_bytes(_Origin.payload)
    _Origin.status_override = 500  # any fetch at all now fails
    p = catalog.download(_entry(origin), dest_dir=tmp_path)
    assert p.read_bytes() == _Origin.payload


def test_existing_file_of_the_wrong_size_is_replaced(origin, tmp_path):
    (tmp_path / "model.gguf").write_bytes(b"truncated")
    p = catalog.download(_entry(origin), dest_dir=tmp_path)
    assert p.read_bytes() == _Origin.payload


def test_gated_host_error_names_the_cause(origin, tmp_path):
    _Origin.status_override = 401
    with pytest.raises(RuntimeError) as ei:
        catalog.download(_entry(origin), dest_dir=tmp_path)
    assert "gated" in str(ei.value), "a 401 must point at the mirror, not just say 401"


# ── remote size + HF lane ─────────────────────────────────────────────────────
def test_remote_size_reads_content_range(origin):
    assert catalog.remote_size(origin) == 10240


def test_from_hf_builds_a_resolve_url():
    e = catalog.from_hf("owner/repo", "m.gguf", size_bytes=5)
    assert e.resolve_url() == "https://huggingface.co/owner/repo/resolve/main/m.gguf"
    assert e.min_ram_gb == 0.0, "an arbitrary pick must not claim to be judgeable"


# ── remote catalog refresh ────────────────────────────────────────────────────
def test_refresh_rejects_a_document_with_no_models(origin, monkeypatch, tmp_path):
    before = list(catalog.CATALOG)
    doc = tmp_path / "c.json"
    doc.write_text(json.dumps({"models": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        catalog.refresh_from(doc.as_uri())
    assert catalog.CATALOG == before, "a bad document must leave the catalog intact"


def test_refresh_rejects_an_entry_missing_a_size(tmp_path):
    before = list(catalog.CATALOG)
    doc = tmp_path / "c.json"
    doc.write_text(json.dumps({"models": [{"filename": "a.gguf", "label": "A"}]}),
                   encoding="utf-8")
    with pytest.raises(ValueError):
        catalog.refresh_from(doc.as_uri())
    assert catalog.CATALOG == before


def test_refresh_replaces_rather_than_merges(tmp_path):
    before = list(catalog.CATALOG)
    doc = tmp_path / "c.json"
    doc.write_text(json.dumps({"models": [
        {"filename": "only.gguf", "label": "Only", "size_bytes": 123, "min_ram_gb": 1}
    ]}), encoding="utf-8")
    try:
        got = catalog.refresh_from(doc.as_uri())
        assert [e.filename for e in got] == ["only.gguf"]
        # A withdrawn entry must really go away, not linger because nothing removes it.
        assert catalog.find("Bonsai-4B-Q1_0.gguf") is None
    finally:
        catalog.CATALOG[:] = before
