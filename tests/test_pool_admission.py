"""adk.pool_admission: `adk grid join` needs a per-person awnboard invite first."""

from __future__ import annotations

import argparse
import sys

import pytest
from adk import pool_admission, pool_role
from adk.pool_admission import pool_gate
from adk.pool_role import GpuCaps, NodeCaps

MODEL = "deepseek-v4.1-flash-1017"
KEY = "pool-admission-test-key-0123"


def _args(**kw):
    base = {"model": "v41", "apply": False, "coordinator": "", "bind": "10.0.0.2",
            "rpc_server": "rpc-server", "role": "auto", "ram_cap_gib": None, "json": True,
            "invite": "", "invite_file": "", "invite_as": "", "invite_ledger": "",
            "invite_secret_file": ""}
    base.update(kw)
    return argparse.Namespace(**base)


def _host(_c=None):
    return NodeCaps("linux", 64.0, 56.0, 16, 32, 1,
                    [GpuCaps(0, "RTX 5090", 30.0, 32.6)], rtt_ms=3.0)


class _ProbeSpy:
    def __init__(self):
        self.called = False

    def __call__(self, _c):
        self.called = True
        return _host()


@pytest.fixture
def board(tmp_path, monkeypatch):
    awnboard = pytest.importorskip("awnboard")
    monkeypatch.setenv(pool_admission.SECRET_ENV, KEY)
    monkeypatch.delenv(pool_admission.INVITE_ENV, raising=False)
    ledger_path = tmp_path / "grants.json"
    ledger_path.write_text('{"revoked": {}, "used": {}}', encoding="utf-8")
    monkeypatch.setenv(pool_admission.LEDGER_ENV, str(ledger_path))
    signer = awnboard.HmacSigner(KEY)
    ledger = awnboard.GrantLedger(ledger_path)

    def mint(to="sam@example.com", uses=1, inv_id="inv-1", model=MODEL):
        return awnboard.invite(signer, gate=pool_gate(model), to=to, uses=uses,
                               ttl_s=3600, inv_id=inv_id)

    return argparse.Namespace(mint=mint, ledger=ledger, signer=signer)


def test_self_test_passes():
    pytest.importorskip("awnboard")
    assert pool_admission.self_test() == 0


def test_self_test_fails_when_revocation_is_ignored(monkeypatch):
    awnboard = pytest.importorskip("awnboard")
    monkeypatch.setattr(awnboard.GrantLedger, "is_revoked", lambda self, _i: False)
    assert pool_admission.self_test() == 1


def test_self_test_fails_when_addressee_is_ignored(monkeypatch):
    pytest.importorskip("awnboard")
    import awnboard.requirements as req

    monkeypatch.setattr(req.Invited, "_same", lambda self, a, b: True)
    assert pool_admission.self_test() == 1


def test_self_test_cannot_judge_without_awnboard(monkeypatch):
    monkeypatch.setitem(sys.modules, "awnboard", None)
    assert pool_admission.self_test() == 2


def test_missing_awnboard_is_cannot_judge_and_plans_nothing(monkeypatch):
    monkeypatch.setitem(sys.modules, "awnboard", None)
    spy = _ProbeSpy()
    assert pool_role.run(_args(invite="awb1.x.y", invite_as="sam@example.com"),
                         probe=spy) == 2
    assert not spy.called


def test_missing_invite_exits_nonzero_before_probing(board, capsys):
    spy = _ProbeSpy()
    assert pool_role.run(_args(invite_as="sam@example.com"), probe=spy) == 1
    assert not spy.called
    assert "no invite presented" in capsys.readouterr().err


def test_revoked_invite_exits_nonzero_before_probing(board, capsys):
    tok = board.mint(inv_id="inv-revoked", uses=5)
    board.ledger.revoke("inv-revoked", reason="left the project")
    spy = _ProbeSpy()
    rc = pool_role.run(_args(invite=tok, invite_as="sam@example.com"), probe=spy)
    assert rc == 1
    assert not spy.called
    err = capsys.readouterr().err
    assert "admission refused" in err and "revoked" in err


def test_revoking_one_invite_leaves_another_working(board):
    sam = board.mint(to="sam@example.com", inv_id="inv-sam")
    ana = board.mint(to="ana@example.com", inv_id="inv-ana")
    board.ledger.revoke("inv-sam")
    assert pool_role.run(_args(invite=sam, invite_as="sam@example.com"), probe=_host) == 1
    assert pool_role.run(_args(invite=ana, invite_as="ana@example.com"), probe=_host) == 0


def test_valid_invite_plans_and_does_not_spend(board, capsys):
    tok = board.mint(inv_id="inv-ok", uses=1)
    spy = _ProbeSpy()
    assert pool_role.run(_args(invite=tok, invite_as="Sam@Example.com"), probe=spy) == 0
    assert spy.called
    assert '"verdict": "ADMITTED"' in capsys.readouterr().out
    assert board.ledger.uses("inv-ok") == 0
    # A second dry run still works: a one-use invite survives being checked.
    assert pool_role.run(_args(invite=tok, invite_as="sam@example.com"), probe=_host) == 0


def test_forwarded_invite_is_refused(board):
    tok = board.mint(to="sam@example.com")
    assert pool_role.run(_args(invite=tok, invite_as="eve@example.com"), probe=_host) == 1


def test_invite_for_another_pool_is_refused(board):
    tok = board.mint(model="deepseek-v4-flash")
    assert pool_role.run(_args(invite=tok, invite_as="sam@example.com"), probe=_host) == 1


def test_alias_and_canonical_model_share_one_gate(board):
    tok = board.mint()
    for m in ("v41", "deepseek-v4.1", MODEL):
        assert pool_role.run(_args(model=m, invite=tok, invite_as="sam@example.com"),
                             probe=_host) == 0


def test_spent_invite_is_refused(board):
    tok = board.mint(inv_id="inv-once", uses=1)
    adm = pool_admission.verify_pool_invite(tok, "sam@example.com", model=MODEL,
                                            signer=board.signer, ledger=board.ledger,
                                            spend=True)
    assert adm.admitted and adm.spent
    assert pool_role.run(_args(invite=tok, invite_as="sam@example.com"), probe=_host) == 1


def test_no_ledger_is_cannot_judge(board, monkeypatch):
    tok = board.mint()
    monkeypatch.delenv(pool_admission.LEDGER_ENV)
    assert pool_role.run(_args(invite=tok, invite_as="sam@example.com"), probe=_host) == 2


def test_no_key_is_cannot_judge(board, monkeypatch):
    tok = board.mint()
    monkeypatch.delenv(pool_admission.SECRET_ENV)
    assert pool_role.run(_args(invite=tok, invite_as="sam@example.com"), probe=_host) == 2


def test_mistyped_ledger_path_is_cannot_judge_not_nothing_revoked(board, tmp_path,
                                                                  monkeypatch):
    # awnboard reads a missing ledger file as empty; a typo must not un-revoke.
    tok = board.mint(inv_id="inv-gone", uses=5)
    board.ledger.revoke("inv-gone")
    typo = tmp_path / "nope" / "grants.json"
    monkeypatch.setenv(pool_admission.LEDGER_ENV, str(typo))
    spy = _ProbeSpy()
    assert pool_role.run(_args(invite=tok, invite_as="sam@example.com"), probe=spy) == 2
    assert not spy.called
    assert not typo.parent.exists()


def test_unreadable_ledger_is_cannot_judge(board, tmp_path, monkeypatch):
    tok = board.mint()
    torn = tmp_path / "torn.json"
    torn.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv(pool_admission.LEDGER_ENV, str(torn))
    assert pool_role.run(_args(invite=tok, invite_as="sam@example.com"), probe=_host) == 2


def test_invite_from_file_and_env(board, tmp_path, monkeypatch):
    tok = board.mint()
    f = tmp_path / "invite.txt"
    f.write_text(tok + "\n", encoding="utf-8")
    assert pool_role.run(_args(invite_file=str(f), invite_as="sam@example.com"),
                         probe=_host) == 0
    monkeypatch.setenv(pool_admission.INVITE_ENV, tok)
    assert pool_role.run(_args(invite_as="sam@example.com"), probe=_host) == 0


def test_grid_join_cli_accepts_admission_flags():
    from adk.cli import _register_grid_parser

    root = argparse.ArgumentParser()
    _register_grid_parser(root.add_subparsers(dest="command"))
    ns = root.parse_args(["grid", "join", "--model", "v41", "--invite", "awb1.a.b",
                          "--as", "sam@example.com", "--invite-ledger", "g.json"])
    assert ns.invite == "awb1.a.b" and ns.invite_as == "sam@example.com"
    assert ns.invite_ledger == "g.json"
