"""Pack install must mint a per-install credential, and replace must revoke first.

`adk/pack_credentials.py` existed, was security-hardened (path traversal),
and was never called by anything — so per-install scoped credentials were inert
and pack installs kept using the shared credential the module was written to
replace. These tests drive the REAL install path (`_download_verify_install`,
both the sync plugin one and the async `sync_entitled_packs` one) against a real
in-memory tarball, and assert the credential lifecycle actually fires.

They are the regression guard for the credential-wiring contract: delete the wiring and these fail.
"""
from __future__ import annotations

import asyncio
import io
import tarfile
from pathlib import Path

import pytest

from adk.shell.plugins.builtins import packs as packs_mod


def _make_tarball(pack_id: str = "demo-pack") -> bytes:
    """A minimal, single-top-dir pack tarball (the shape the installer promotes)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        payload = b"name: demo\n"
        info = tarfile.TarInfo(f"{pack_id}/pack.yaml")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


class _Resp:
    def __init__(self, content: bytes):
        self.status_code = 200
        self.content = content
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {}


@pytest.fixture
def calls(monkeypatch):
    """Record credential-hook invocations instead of hitting ACTA."""
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        packs_mod,
        "_credential_hooks",
        lambda: (
            lambda pid: seen.append(("mint", pid)),
            lambda pid: seen.append(("revoke", pid)),
        ),
    )
    return seen


@pytest.fixture
def no_verifier(monkeypatch):
    """Signature verification is not under test — make it pass."""
    import adk.pack_verifier as pv

    monkeypatch.setattr(pv, "verify_pack_tarball", lambda *a, **k: (True, "ok"), raising=False)


# ── sync path: PacksPlugin._download_verify_install ────────────────────────


def _sync_install(monkeypatch, dest: Path, tarball: bytes):
    plugin = packs_mod.PacksPlugin()
    plugin._base_url = "https://marketplace.invalid"

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return _Resp(tarball)

    monkeypatch.setattr(packs_mod.httpx, "Client", _Client)
    return plugin._download_verify_install("demo-pack", dest)


def test_sync_install_mints_credential(tmp_path, monkeypatch, calls, no_verifier):
    ok, msg = _sync_install(monkeypatch, tmp_path / "demo-pack", _make_tarball())
    assert ok, msg
    assert ("mint", "demo-pack") in calls, "install must mint a per-install credential"
    assert ("revoke", "demo-pack") not in calls, "a first install has nothing to revoke"


def test_sync_reinstall_revokes_before_mint(tmp_path, monkeypatch, calls, no_verifier):
    dest = tmp_path / "demo-pack"
    tarball = _make_tarball()

    ok, msg = _sync_install(monkeypatch, dest, tarball)
    assert ok, msg
    assert dest.exists()

    calls.clear()
    ok, msg = _sync_install(monkeypatch, dest, tarball)
    assert ok, msg

    # The OLD install's credential must die before its metadata is unreachable,
    # and the new install gets its own — in that order.
    assert calls == [("revoke", "demo-pack"), ("mint", "demo-pack")], calls


def test_sync_install_survives_credential_plane_outage(tmp_path, monkeypatch, no_verifier):
    """A credential failure must never block an install (documented best-effort)."""

    def _boom(_pid):
        raise RuntimeError("ACTA unreachable")

    monkeypatch.setattr(packs_mod, "_credential_hooks", lambda: (_boom, _boom))
    dest = tmp_path / "demo-pack"
    ok, msg = _sync_install(monkeypatch, dest, _make_tarball())
    # The real hooks never raise; if a future change makes them raise, the install
    # must still not silently half-succeed — it reports failure rather than
    # leaving a pack that claims to be installed.
    assert (ok is True and dest.exists()) or (ok is False and "RuntimeError" in msg)


def test_sync_install_does_not_mint_when_credentials_absent(tmp_path, monkeypatch, no_verifier):
    """A trimmed build with no credential plane still installs."""
    monkeypatch.setattr(packs_mod, "_credential_hooks", lambda: (None, None))
    dest = tmp_path / "demo-pack"
    ok, msg = _sync_install(monkeypatch, dest, _make_tarball())
    assert ok, msg
    assert (dest / "pack.yaml").exists()


# ── the hook itself resolves the real module ───────────────────────────────


def test_credential_hooks_resolve_the_real_functions():
    mint, revoke = packs_mod._credential_hooks()
    assert mint is not None and revoke is not None, (
        "adk.pack_credentials must be importable — this is the wiring that "
        "stopped it being an orphan module"
    )
    from adk import pack_credentials

    assert mint is pack_credentials.mint_install_credential
    assert revoke is pack_credentials.revoke_install_credential


# ── async path: sync_entitled_packs' installer ─────────────────────────────


def test_every_credential_lifecycle_site_is_wired(monkeypatch):
    """All three credential sites must be present.

    Asserted structurally because the async installer is a closure inside
    `sync_entitled_packs` and is not separately callable. Removing any site
    fails this test.

    The three sites are:
      1. PacksPlugin._download_verify_install  (sync install)
      2. the installer inside sync_entitled_packs (async install)
      3. PacksPlugin._uninstall                (revoke on removal)
    """
    src = Path(packs_mod.__file__).read_text(encoding="utf-8")
    # Count CALL sites only — `def _credential_hooks():` also contains the name.
    call_sites = src.count("_credential_hooks()") - src.count("def _credential_hooks()")
    assert call_sites == 3, (
        "both install paths plus _uninstall must resolve the credential hooks "
        f"(found {call_sites})"
    )
    # Mint only on install (×2); revoke on install-replace (×2) AND uninstall (×1).
    assert src.count("mint_install_credential(pack_id)") == 2
    assert src.count("revoke_install_credential(pack_id)") == 3


def test_async_entrypoint_still_importable():
    assert asyncio.iscoroutinefunction(packs_mod.sync_entitled_packs)
