"""`adk deploy desktop` installs Desk exactly like the setup page: latest awdesk release,
SHA256SUMS-verified, never the legacy AitherDesktop zip; every "update Desk" message
says how (Desk's own update notice, or the workspace setup page)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from adk import deploy
from adk import desk_release as dr
from adk.desk_bridge import DeskBridgeClient

INSTALLER = b"MZ fake desk installer"
GOOD = hashlib.sha256(INSTALLER).hexdigest()


def _sums(sha: str = GOOD) -> bytes:
    return (
        f"{'a' * 64}  Desk-0.1.17-linux-x86_64.AppImage\n"
        f"{'b' * 64}  Desk-0.1.17-macos-arm64.dmg\n"
        f"{sha}  Desk-0.1.17-windows-x64-setup.exe\n"
    ).encode()


class _Net:
    def __init__(self, sums: bytes, payload: bytes = INSTALLER):
        self.sums, self.payload, self.urls = sums, payload, []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        if url.endswith("SHA256SUMS.txt"):
            return self.sums
        return self.payload


def _install(tmp_path: Path, net: _Net, **kw):
    ran: list[Path] = []

    def run(path: Path) -> int:
        assert path.read_bytes() == INSTALLER  # verified bytes are what runs
        ran.append(path)
        return 0

    res = dr.install_desk(key="windows-x64", homedir=tmp_path, get=net,
                          installed_fn=lambda: None, run_installer=run,
                          declares=lambda label: False, elevated_fn=lambda: False,
                          log=lambda m: None, **kw)
    return res, ran


def test_picks_the_awdesk_windows_asset_and_verifies(tmp_path: Path) -> None:
    net = _Net(_sums())
    res, ran = _install(tmp_path, net)
    assert net.urls[0] == ("https://github.com/Aitherium/awdesk/releases/latest/download/"
                           "SHA256SUMS.txt")
    assert net.urls[1] == ("https://github.com/Aitherium/awdesk/releases/download/v0.1.17/"
                           "Desk-0.1.17-windows-x64-setup.exe")
    assert not any("AitherDesktop" in u or "AitherOS/releases" in u for u in net.urls)
    assert res.installed and res.asset.version == "0.1.17"
    assert [p.name for p in ran] == ["Desk-0.1.17-windows-x64-setup.exe"]


def test_checksum_mismatch_refuses_and_never_runs(tmp_path: Path) -> None:
    net = _Net(_sums(sha="c" * 64))
    with pytest.raises(dr.DeskInstallError, match="SHA256 mismatch"):
        _install(tmp_path, net)


def test_unlisted_asset_refuses_before_download(tmp_path: Path) -> None:
    net = _Net(b"%s  Desk-0.1.17-macos-arm64.dmg\n" % (b"d" * 64))
    with pytest.raises(dr.DeskInstallError, match="unverified"):
        _install(tmp_path, net)
    assert len(net.urls) == 1


def test_already_latest_is_not_reinstalled(tmp_path: Path) -> None:
    net = _Net(_sums())
    res = dr.install_desk(key="windows-x64", homedir=tmp_path, get=net,
                          installed_fn=lambda: {"version": (0, 1, 17), "exe": ""},
                          run_installer=lambda p: pytest.fail("must not reinstall"),
                          declares=lambda label: False, log=lambda m: None)
    assert res.skipped_current and len(net.urls) == 1


def test_explicit_tenant_writes_the_setup_page_shape(tmp_path: Path) -> None:
    res, _ = _install(tmp_path, _Net(_sums()), explicit_label="acme")
    data = json.loads((tmp_path / ".aither" / "desk-tenant.json").read_text())
    assert data["host"] == "acme.aitherium.com" and data["url"] == "https://acme.aitherium.com"
    assert data["schema"] == 1 and data["app"] == "acme"
    assert res.tenant_label == "acme"


def _signed_in(tmp_path: Path, tenant_id: str) -> None:
    auth = {"active_profile": "default",
            "profiles": {"default": {"access_token": "x", "user": {"tenant_id": tenant_id}}}}
    (tmp_path / ".aither").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".aither" / "auth.json").write_text(json.dumps(auth))


def test_signed_in_account_writes_desks_sign_in_shape_only_when_host_declares(
        tmp_path: Path) -> None:
    _signed_in(tmp_path, "tnt_acme_co")
    label, file, _ = dr.resolve_and_write_tenant(None, homedir=tmp_path,
                                                 declares=lambda label: label == "acme-co")
    assert label == "acme-co"
    data = json.loads(file.read_text())
    assert data == {"host": "acme-co.aitherium.com", "source": "sign-in",
                    "tenant_id": "tnt_acme_co"}


def test_no_workspace_known_writes_nothing(tmp_path: Path) -> None:
    _signed_in(tmp_path, "tnt_acme")
    label, file, _ = dr.resolve_and_write_tenant(None, homedir=tmp_path,
                                                 declares=lambda label: False)
    assert (label, file) == (None, None)
    assert not (tmp_path / ".aither" / "desk-tenant.json").exists()


def test_installer_written_file_is_never_replaced_by_account_guess(tmp_path: Path) -> None:
    dr.write_installer_tenant("other", homedir=tmp_path)
    _signed_in(tmp_path, "tnt_acme")
    _, file, note = dr.resolve_and_write_tenant(None, homedir=tmp_path,
                                                declares=lambda label: True)
    assert file is None and "kept" in note
    assert json.loads((tmp_path / ".aither" / "desk-tenant.json").read_text())["app"] == "other"


def test_deploy_desktop_uses_desk_release(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = {}

    def fake_install(**kw):
        seen.update(kw)
        return dr.DeskInstallResult(asset=dr.DeskAsset("Desk-0.1.17-windows-x64-setup.exe",
                                                       "0.1.17", GOOD), installed=True)

    monkeypatch.setattr(dr, "install_desk", fake_install)
    assert deploy.deploy_desktop(tenant="acme", force=True) == 0
    assert seen["explicit_label"] == "acme" and seen["force"] is True


def test_deploy_desktop_failure_prints_the_setup_page(monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture) -> None:
    def boom(**kw):
        raise dr.DeskInstallError("SHA256 mismatch")

    monkeypatch.setattr(dr, "install_desk", boom)
    assert deploy.deploy_desktop(tenant="acme") == 1
    assert "https://acme.aitherium.com/setup-pc" in capsys.readouterr().out


def test_update_hint_is_tenant_aware(tmp_path: Path) -> None:
    assert dr.desk_update_hint(homedir=tmp_path).endswith(
        "https://<your-workspace>.aitherium.com/setup-pc")
    dr.write_installer_tenant("acme", homedir=tmp_path)
    hint = dr.desk_update_hint(homedir=tmp_path)
    assert "Aither from its own update notice" in hint
    assert hint.endswith("https://acme.aitherium.com/setup-pc")


def test_old_desk_message_says_how_to_update() -> None:
    bridge = DeskBridgeClient(url="http://127.0.0.1:1", transport=lambda *a, **k: (404, ""))
    code, doc = bridge.call_desktop("overlay")
    assert code == 2
    assert "/setup-pc" in doc["error"] and "update notice" in doc["error"]


def test_installed_display_name_matches_old_and_renamed_app() -> None:
    for name in ("Desk 0.1.10", "Desk", "Aither Desk", "Aither", "Aither 0.1.18"):
        assert dr.INSTALLED_DISPLAY_NAME_RE.match(name), name
    for name in ("Aither Studio", "Desktop 1.0", "My Desk"):
        assert not dr.INSTALLED_DISPLAY_NAME_RE.match(name), name
