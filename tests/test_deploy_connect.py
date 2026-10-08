"""`adk deploy connect` delegates to the one Awconnect installer.

It used to fetch ``releases/latest/download/Awconnect.zip`` (an asset no release
carries), skip the checksum, and unpack into ``~/.aither/Awconnect/`` rather than
the ``current/`` folder every other tool reads. These pin the delegation.
"""

from __future__ import annotations

import hashlib
import inspect

import pytest

from adk import awconnect_setup as aw
from adk import deploy


@pytest.fixture(autouse=True)
def _authed(monkeypatch):
    monkeypatch.setattr(deploy, "_require_auth", lambda key=None: True)


def test_deploy_connect_runs_awconnect_install(monkeypatch, tmp_path, capsys):
    seen = {}

    def fake_install(**kw):
        seen.update(kw)
        return {"staged": {"path": str(tmp_path / "current"), "version": "4.1.5"}}

    monkeypatch.setattr(aw, "install", fake_install)
    monkeypatch.setattr(deploy, "_download_bytes", lambda url: pytest.fail(f"fetched {url}"))
    assert deploy.deploy_connect() == 0
    assert seen["wait"] == 0
    out = capsys.readouterr().out
    assert aw.WEBSTORE_URL in out and str(tmp_path / "current") in out


def test_deploy_connect_reports_installer_refusal(monkeypatch):
    def refuse(**kw):
        raise aw.ChecksumMismatchError("checksum mismatch -- refusing to install it")

    monkeypatch.setattr(aw, "install", refuse)
    assert deploy.deploy_connect() == 1


def test_deploy_connect_dry_run_resolves_without_staging(monkeypatch, capsys):
    cand = aw.Candidate("release", "4.1.5", "https://x/aither-connect-unpacked-v4.1.5.zip",
                        sha256_url="https://x/aither-connect-unpacked-v4.1.5.zip.sha256")
    monkeypatch.setattr(aw, "latest_release", lambda fetch, api=None, variant=None: cand)
    monkeypatch.setattr(aw, "find_checkout", lambda *a, **k: None)
    monkeypatch.setattr(aw, "stage", lambda *a, **k: pytest.fail("dry run staged"))
    assert deploy.deploy_connect(dry_run=True) == 0
    out = capsys.readouterr().out
    assert "release 4.1.5" in out and "unpacked" in out


def test_deploy_connect_dry_run_with_no_source_names_store(monkeypatch, capsys):
    monkeypatch.setattr(aw, "latest_release", lambda *a, **k: None)
    monkeypatch.setattr(aw, "find_checkout", lambda *a, **k: None)
    assert deploy.deploy_connect(dry_run=True) == 1
    assert aw.WEBSTORE_URL in capsys.readouterr().out


def test_deploy_connect_no_longer_names_the_phantom_asset():
    src = inspect.getsource(deploy.deploy_connect)
    assert "Awconnect.zip" not in src.split('"""', 2)[2]
    assert "extractall" not in src


def test_deploy_connect_end_to_end_stages_current(monkeypatch, tmp_path):
    """Real install path, fake network: verified unpacked zip -> <ver>/ + current/."""
    import io
    import json
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json",
                    json.dumps({"manifest_version": 3, "name": "awconnect", "version": "4.1.5"}))
    blob = buf.getvalue()
    name = "aither-connect-unpacked-v4.1.5.zip"
    api = json.dumps([{"tag_name": "connect-v4.1.5", "assets": [
        {"name": name, "browser_download_url": f"https://x/{name}"},
        {"name": f"{name}.sha256", "browser_download_url": f"https://x/{name}.sha256"},
    ]}]).encode()
    files = {aw.DEFAULT_RELEASES_API: api, f"https://x/{name}": blob,
             f"https://x/{name}.sha256": f"{hashlib.sha256(blob).hexdigest()}  {name}".encode()}
    monkeypatch.setattr(aw, "_default_fetch", files.__getitem__)
    monkeypatch.setenv("AITHER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AITHER_AWCONNECT_HOME", str(tmp_path / "awc"))
    monkeypatch.setenv("AITHEROS_ROOT", str(tmp_path / "nowhere"))
    real_install = aw.install
    monkeypatch.setattr(aw, "install", lambda **kw: real_install(
        fetch=aw._default_fetch, browsers=[], prefer="release",
        run=lambda *a, **k: type("P", (), {"returncode": 0})(), **kw))
    assert deploy.deploy_connect() == 0
    marker = json.loads((tmp_path / "awc" / "current" / aw.MARKER_FILE).read_text())
    assert marker["source"] == "release" and marker["sha256"] == hashlib.sha256(blob).hexdigest()
    assert (tmp_path / "awc" / "4.1.5" / "manifest.json").is_file()
