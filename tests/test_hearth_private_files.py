"""owner.json, the approval store, the receipts key and the receipts log are owner-only.

All four go through :mod:`adk._private_file` -- the helper the local channel token
uses: 0600 on POSIX; on Windows ``icacls /inheritance:r /grant:r <user>:F`` applied
to the EMPTY file before anything is written, and a failure refuses the write.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from adk import _private_file as pf
from adk import approval, receipts
from adk.home import config as hc
from adk.home import hearth


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "agent-home"
    root.mkdir()
    monkeypatch.setenv(hc.HOME_ENV, str(root))
    monkeypatch.delenv(receipts.KEY_ENV, raising=False)
    monkeypatch.delenv("AITHER_RECEIPTS_PATH", raising=False)
    monkeypatch.setattr(receipts, "_home_dir", lambda: root)
    monkeypatch.setattr(receipts, "_awseal_private_key", lambda: None)
    return root


def _fake_icacls(monkeypatch, result=0):
    """Pretend to be Windows; record (path, bytes in it) at each icacls call."""
    seen = []

    def run(argv, **kw):
        assert argv[0] == "icacls" and "/inheritance:r" in argv and "/grant:r" in argv
        seen.append((Path(argv[1]), Path(argv[1]).read_bytes()))
        if isinstance(result, BaseException):
            raise result
        return SimpleNamespace(returncode=result, stdout="", stderr="")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", run)
    return seen


# ── Windows (icacls faked) ─────────────────────────────────────────────────────

def test_owner_json_is_restricted_while_empty(home, monkeypatch):
    seen = _fake_icacls(monkeypatch)
    reg = hearth.OwnerRegistry(hearth.owner_path(home))
    reg.bind("relay", "david")
    (path, content), = seen
    assert path.parent == home and content == b""        # restricted BEFORE the write
    assert hearth.OwnerRegistry(hearth.owner_path(home)).owner("relay") == "david"


@pytest.mark.parametrize("result", [5, OSError("no icacls"),
                                    subprocess.TimeoutExpired("icacls", 15)])
def test_owner_json_refuses_when_icacls_fails(home, monkeypatch, result):
    _fake_icacls(monkeypatch, result)
    reg = hearth.OwnerRegistry(hearth.owner_path(home))
    with pytest.raises(hc.HomeError, match="refusing to save the owner binding"):
        reg.bind("relay", "david")
    assert not hearth.owner_path(home).exists()
    assert list(home.iterdir()) == []                     # no temp file left either


def test_approval_store_is_restricted_and_fails_closed(tmp_path, monkeypatch):
    seen = _fake_icacls(monkeypatch)
    store = approval.ApprovalStore(tmp_path / "paused_turns.json")
    store.put_pending("s", user_message="m", agent="a", pending=[])
    assert seen and seen[-1][1] == b""
    assert store.get("s")["user_message"] == "m"

    _fake_icacls(monkeypatch, 5)
    with pytest.raises(pf.PrivateFileError):
        store.record_decisions("s", [{"tool": "web_fetch", "result": "allow"}])
    assert store.decision_for("s", "web_fetch") is None  # the allow never landed
    assert not list(tmp_path.glob("*.tmp"))


def test_receipts_log_and_key_are_restricted_while_empty(home, monkeypatch):
    pytest.importorskip("cryptography")
    seen = _fake_icacls(monkeypatch)
    log = home / "actions.jsonl"
    receipts.append("tool", "x", {"a": 1}, "ok", "auto", path=log)
    restricted = {p.name: content for p, content in seen}
    assert restricted == {"actions.jsonl": b"", "receipt.key": b""}
    assert receipts.check(log)[0] == 0
    seen.clear()
    receipts.append("tool", "y", {"a": 2}, "ok", "auto", path=log)
    assert seen == []                                     # created once, then appended


def test_receipts_refuse_a_log_that_cannot_be_restricted(home, monkeypatch):
    _fake_icacls(monkeypatch, 5)
    log = home / "actions.jsonl"
    with pytest.raises(pf.PrivateFileError):
        receipts.append("tool", "x", {"a": 1}, "ok", "auto", path=log)
    assert not log.exists()


def test_receipt_key_is_not_written_when_it_cannot_be_restricted(home, monkeypatch):
    pytest.importorskip("cryptography")
    _fake_icacls(monkeypatch, 5)
    with pytest.warns(RuntimeWarning, match="receipt key NOT created"):
        assert receipts._fallback_private_key(create=True) is None
    assert not receipts.fallback_key_path().exists()


# ── POSIX ────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_posix_modes_are_0600(home):
    pytest.importorskip("cryptography")
    hearth.OwnerRegistry(hearth.owner_path(home)).bind("relay", "david")
    store = approval.ApprovalStore(home / "paused_turns.json")
    store.put_pending("s", user_message="m", agent="a", pending=[])
    receipts.append("tool", "x", {"a": 1}, "ok", "auto", path=home / "actions.jsonl")
    for name in ("owner.json", "paused_turns.json", "actions.jsonl", "receipt.key"):
        assert stat.S_IMODE((home / name).stat().st_mode) == 0o600, name


# ── real Windows ───────────────────────────────────────────────────────────────

@pytest.mark.skipif(sys.platform != "win32", reason="real icacls")
def test_real_icacls_leaves_only_the_owner(home):
    path = pf.write_private_text(home / "owner.json", "{}")
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=15).stdout
    user = os.environ.get("USERNAME", "")
    assert user and user.lower() in out.lower()
    for broad in ("BUILTIN\\Users", "Everyone", "Authenticated Users"):
        assert broad.lower() not in out.lower(), out
    assert "(I)" not in out                               # nothing inherited
