"""`/packs uninstall` — completes the per-install credential lifecycle.

Install mints a credential scoped to pack_id + install_id. Before this verb
existed the ONLY revoke path was a reinstall, so removing a pack any other way
left a live credential whose metadata was gone — unrevocable forever.

Order is the load-bearing part: revoke while the metadata is still readable,
THEN delete the directory.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from adk.shell.plugins.builtins import packs as packs_mod


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A fake pack root containing one installed pack."""
    root = tmp_path / "packs"
    (root / "demo-pack").mkdir(parents=True)
    (root / "demo-pack" / "pack.yaml").write_text("name: demo\n", encoding="utf-8")
    monkeypatch.setattr(packs_mod.PacksPlugin, "_packs_dir", staticmethod(lambda: root))
    return root


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        packs_mod,
        "_credential_hooks",
        lambda: (
            lambda pid: seen.append(("mint", pid)),
            lambda pid: (seen.append(("revoke", pid)) or True),
        ),
    )
    return seen


def test_uninstall_revokes_then_removes(installed, calls):
    out = packs_mod.PacksPlugin()._uninstall("demo-pack")
    assert ("revoke", "demo-pack") in calls, "credential must be revoked"
    assert not (installed / "demo-pack").exists(), "pack directory must be gone"
    assert "Removed pack 'demo-pack'" in out
    assert "revoked" in out


def test_revoke_happens_while_metadata_is_still_readable(installed, monkeypatch):
    """Revoke must run BEFORE the directory disappears, not after."""
    order: list[str] = []

    def _revoke(pid: str) -> bool:
        order.append("revoke:dir_exists=%s" % (installed / "demo-pack").exists())
        return True

    monkeypatch.setattr(packs_mod, "_credential_hooks", lambda: (None, _revoke))
    packs_mod.PacksPlugin()._uninstall("demo-pack")
    assert order == ["revoke:dir_exists=True"], order


def test_uninstall_of_absent_pack_is_a_no_op(installed, calls):
    out = packs_mod.PacksPlugin()._uninstall("never-installed")
    assert "not installed" in out
    assert calls == [], "nothing to revoke for a pack that was never installed"


def test_a_failed_revoke_is_reported_not_swallowed(installed, monkeypatch):
    monkeypatch.setattr(
        packs_mod, "_credential_hooks", lambda: (None, lambda pid: False)
    )
    out = packs_mod.PacksPlugin()._uninstall("demo-pack")
    assert "revoke failed" in out
    # The pack is still removed — a dead credential must not pin the install.
    assert not (installed / "demo-pack").exists()


def test_a_raising_revoke_does_not_block_removal(installed, monkeypatch):
    def _boom(_pid):
        raise RuntimeError("ACTA down")

    monkeypatch.setattr(packs_mod, "_credential_hooks", lambda: (None, _boom))
    out = packs_mod.PacksPlugin()._uninstall("demo-pack")
    assert "revoke error" in out and "RuntimeError" in out
    assert not (installed / "demo-pack").exists()


def test_works_without_a_credential_plane(installed, monkeypatch):
    monkeypatch.setattr(packs_mod, "_credential_hooks", lambda: (None, None))
    out = packs_mod.PacksPlugin()._uninstall("demo-pack")
    assert "not applicable" in out
    assert not (installed / "demo-pack").exists()


# ── path confinement: this verb calls rmtree ────────────────────────────────


@pytest.mark.parametrize(
    "evil",
    ["../../secrets", "..", "../", "/etc", "demo-pack/../..", "\\..\\.."],
)
def test_uninstall_refuses_to_escape_the_pack_root(installed, evil, calls):
    """An id that resolves outside the pack root must never reach rmtree."""
    sibling = installed.parent / "DO-NOT-DELETE"
    sibling.mkdir(exist_ok=True)
    (sibling / "keep.txt").write_text("keep", encoding="utf-8")

    out = packs_mod.PacksPlugin()._uninstall(evil)

    assert out.startswith("ERROR: refusing to remove") or "not installed" in out, out
    assert sibling.exists() and (sibling / "keep.txt").exists()
    assert installed.exists(), "the pack root itself must survive"
    assert calls == [], "no credential call for a rejected path"


def test_uninstall_never_removes_the_pack_root_itself(installed):
    out = packs_mod.PacksPlugin()._uninstall("")
    assert out.startswith("ERROR: refusing to remove"), out
    assert installed.exists()


# ── wired into the command surface ─────────────────────────────────────────


@pytest.mark.parametrize("verb", ["uninstall", "remove", "rm"])
def test_verb_and_aliases_are_dispatched(installed, calls, verb):
    out = packs_mod.PacksPlugin().execute([verb, "demo-pack"])
    assert "Removed pack 'demo-pack'" in out
    assert ("revoke", "demo-pack") in calls


def test_verb_requires_a_pack_id(installed):
    out = packs_mod.PacksPlugin().execute(["uninstall"])
    assert "requires <pack-id>" in out


def test_uninstall_is_documented_in_help():
    out = packs_mod.PacksPlugin().execute(["help"])
    assert "uninstall" in out, "a destructive verb must be discoverable in help"


def test_docstring_lists_uninstall():
    assert "uninstall" in (packs_mod.PacksPlugin.__doc__ or "")
