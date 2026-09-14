"""`/packs sync` — license-driven auto-install on a node.

Covers: entitlement polling, skip-already-present, download→verify→extract,
and the path-traversal guard on archive extraction.
"""

import io
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from adk.shell.plugins.builtins.packs import PacksPlugin, _safe_extract


def _make_tarball(top: str = "demo-pack") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        data = b"tools: []\n"
        info = tarfile.TarInfo(f"{top}/brain_pack.yaml")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture()
def plugin(tmp_path, monkeypatch):
    # Redirect the install root into tmp.
    monkeypatch.setattr(PacksPlugin, "_packs_dir",
                        staticmethod(lambda: tmp_path / "packs"))
    (tmp_path / "packs").mkdir()
    p = PacksPlugin()
    p._base_url = "https://portal.test"
    return p


def _resp(status=200, json_body=None, content=b"", headers=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_body or {}
    r.content = content
    r.headers = headers or {}
    r.raise_for_status = MagicMock()
    if status >= 400:
        import httpx
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=r)
    return r


class TestSync:
    def test_no_entitlements(self, plugin):
        with patch("httpx.Client") as C:
            C.return_value.__enter__.return_value.get.return_value = _resp(
                json_body={"licenses": []})
            out = plugin._sync([])
        assert "No entitled packs" in out

    def test_dry_run_lists_without_installing(self, plugin, tmp_path):
        with patch("httpx.Client") as C:
            C.return_value.__enter__.return_value.get.return_value = _resp(
                json_body={"licenses": [
                    {"listing_id": "demo-pack", "status": "active"}]})
            out = plugin._sync(["--dry-run"])
        assert "would install" in out
        assert not (tmp_path / "packs" / "demo-pack").exists()

    def test_skips_already_present(self, plugin, tmp_path):
        present = tmp_path / "packs" / "demo-pack"
        present.mkdir()
        (present / "brain_pack.yaml").write_text("tools: []\n")
        with patch("httpx.Client") as C:
            C.return_value.__enter__.return_value.get.return_value = _resp(
                json_body={"licenses": [
                    {"listing_id": "demo-pack", "status": "active"}]})
            out = plugin._sync([])
        assert "already present: 1" in out

    def test_download_verify_install(self, plugin, tmp_path, monkeypatch):
        tarball = _make_tarball("demo-pack")
        license_resp = _resp(json_body={"licenses": [
            {"listing_id": "demo-pack", "status": "active"}]})
        download_resp = _resp(content=tarball,
                              headers={"X-Aither-Pack-Signature": "deadbeef"})

        calls = {"n": 0}

        def _get(url, **kw):
            calls["n"] += 1
            return license_resp if "license/mine" in url else download_resp

        # Signature verification passes (unit-isolated from the real key).
        monkeypatch.setattr("adk.pack_verifier.verify_pack_tarball",
                            lambda *a, **k: (True, "ok"))
        with patch("httpx.Client") as C:
            C.return_value.__enter__.return_value.get.side_effect = _get
            out = plugin._sync([])
        assert "installed: 1" in out
        dest = tmp_path / "packs" / "demo-pack"
        assert (dest / "brain_pack.yaml").is_file()

    def test_rejects_bad_signature(self, plugin, tmp_path, monkeypatch):
        tarball = _make_tarball("demo-pack")
        license_resp = _resp(json_body={"licenses": [
            {"listing_id": "demo-pack", "status": "active"}]})
        download_resp = _resp(content=tarball,
                              headers={"X-Aither-Pack-Signature": "bad"})

        def _get(url, **kw):
            return license_resp if "license/mine" in url else download_resp

        monkeypatch.setattr("adk.pack_verifier.verify_pack_tarball",
                            lambda *a, **k: (False, "invalid signature"))
        with patch("httpx.Client") as C:
            C.return_value.__enter__.return_value.get.side_effect = _get
            out = plugin._sync([])
        assert "failed: 1" in out
        assert not (tmp_path / "packs" / "demo-pack").exists()


class TestSafeExtract:
    def test_blocks_path_traversal(self, tmp_path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            data = b"x"
            info = tarfile.TarInfo("../escape.txt")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            with pytest.raises(ValueError, match="unsafe path"):
                _safe_extract(tar, str(tmp_path / "out"))

    def test_allows_normal_members(self, tmp_path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            data = b"ok"
            info = tarfile.TarInfo("pack/file.txt")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        buf.seek(0)
        out = tmp_path / "out"
        out.mkdir()
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            _safe_extract(tar, str(out))
        assert (out / "pack" / "file.txt").read_text() == "ok"
