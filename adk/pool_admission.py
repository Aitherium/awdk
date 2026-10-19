"""Pool admission: a node joins a pooled model only with a per-person awnboard invite.

This is piece 2 of `adk grid join`. Before the planner probes anything, the node
operator presents an invitation that the pool owner issued with awnboard:

    awnboard invite --gate pool:deepseek-v4.1-flash-1017 --to sam@example.com --uses 1

The invite is checked with awnboard's own verifier (`Gate` + `Invited`), never a
copy of it. It holds only when all of these hold:

* the signature verifies under the pool's key;
* it was issued for THIS pool's gate (``pool:<model>``), so an invite for one pool
  cannot open another;
* it has not expired and is not dated in the future;
* it names the person presenting it (``--as``), so a forwarded invite is refused;
* the grant ledger does not list it as revoked, and it has uses left.

Revocation is per invite: ``awnboard revoke <id>`` withdraws one person's access
and leaves everyone else's working. That only means something if the ledger is
read, so a ledger that is missing or unreadable is "cannot judge", never "fine".

A dry-run plan reads the ledger and does not spend a use. A one-use invite must
survive being checked before the operator decides to join; the use is spent by
the step that registers the node (``spend=True``).

Inputs (none has a default, because a guessed key or ledger is an open door):

    --invite TOKEN | --invite-file PATH | env ADK_GRID_INVITE
    --as PERSON                          (who the invite must name)
    env AWNBOARD_SECRET | --invite-secret-file PATH
    env AWNBOARD_LEDGER | --invite-ledger PATH

Exit codes used by `adk grid join`: 1 means the invite was refused (missing,
revoked, expired, forwarded, wrong pool, spent). 2 means admission could not be
judged (awnboard not installed, no key, no ledger, unreadable ledger).

    python -m adk.pool_admission --self-test
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

INVITE_ENV = "ADK_GRID_INVITE"
SECRET_ENV = "AWNBOARD_SECRET"
LEDGER_ENV = "AWNBOARD_LEDGER"

ADMITTED = "ADMITTED"
REFUSED = "REFUSED"
CANNOT_JUDGE = "CANNOT_JUDGE"


@dataclass
class PoolAdmission:
    """The verdict. Never carries the token or the key."""

    verdict: str  # ADMITTED | REFUSED | CANNOT_JUDGE
    gate: str
    why: str
    remedy: str = ""
    invitation_id: str = ""
    to: str = ""
    spent: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def admitted(self) -> bool:
        return self.verdict == ADMITTED

    @property
    def exit_code(self) -> int:
        return {ADMITTED: 0, REFUSED: 1}.get(self.verdict, 2)

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "gate": self.gate, "why": self.why,
                "remedy": self.remedy, "invitation_id": self.invitation_id,
                "to": self.to, "spent": self.spent}

    def message(self) -> str:
        if self.admitted:
            return (f"admission   {ADMITTED} gate={self.gate} invite={self.invitation_id} "
                    f"to={self.to}" + ("" if self.spent else " (use not spent: dry run)"))
        head = "admission refused" if self.verdict == REFUSED else "admission cannot be judged"
        out = f"{head} for gate {self.gate}: {self.why}"
        if self.remedy:
            out += f"\n  {self.remedy}"
        return out


def pool_gate(model: str) -> str:
    """The awnboard gate id for one pooled model."""
    return f"pool:{model}"


def _refused(gate: str, why: str, remedy: str, **kw: Any) -> PoolAdmission:
    return PoolAdmission(REFUSED, gate, why, remedy, **kw)


def _cannot(gate: str, why: str, remedy: str) -> PoolAdmission:
    return PoolAdmission(CANNOT_JUDGE, gate, why, remedy)


def verify_pool_invite(token: str, presenter: str, *, model: str, signer: Any,
                       ledger: Any, spend: bool = False) -> PoolAdmission:
    """Check one invite for one pool. Pure apart from reading (or spending in) the ledger.

    The registration route calls this with ``spend=True``; the dry-run planner
    calls it with ``spend=False``.
    """
    gate_id = pool_gate(model)
    try:
        from awnboard import Gate, InvitationError, Invited, read_invitation
    except ImportError:
        return _cannot(gate_id, "awnboard is not installed",
                       "pip install awnboard  (the pool's admission gate)")
    if not token:
        return _refused(gate_id, "no invite presented",
                        f"Pass --invite TOKEN (or set {INVITE_ENV}). Ask the pool owner for "
                        "an invite addressed to you.")
    if not presenter:
        return _refused(gate_id, "no --as given; the invite must name who presents it",
                        "Pass --as with the address the invite was sent to.")
    if ledger is None:
        return _cannot(gate_id, "no grant ledger, so revocation cannot be checked",
                       f"Set {LEDGER_ENV} or pass --invite-ledger.")

    # Signature, gate, expiry and addressee: awnboard's own rung, with no ledger so
    # it does not spend. The ledger is consulted below, read-only unless `spend`.
    rung = Invited(gate_id, signer, None, match="casefold")
    a = Gate(gate_id, requirements=[rung]).admit({"invitation": token, "as": presenter})
    if not a.admitted:
        failed = a.failed[0] if a.failed else None
        return _refused(gate_id, failed.why if failed else "invite refused",
                        a.message())
    inv = read_invitation(token, signer, gate=gate_id)
    kw = {"invitation_id": inv.id, "to": inv.to}
    try:
        if ledger.is_revoked(inv.id):
            return _refused(gate_id, f"invite {inv.id} was revoked",
                            "That invite has been withdrawn. Ask the pool owner for a new one.",
                            **kw)
        if spend:
            if not ledger.spend(inv.id, inv.uses, who=presenter):
                return _refused(gate_id, f"invite {inv.id} has no uses left",
                                "That invite has already been used. Ask for a new one.", **kw)
        elif ledger.uses(inv.id) >= inv.uses:
            return _refused(gate_id, f"invite {inv.id} has no uses left",
                            "That invite has already been used. Ask for a new one.", **kw)
    except InvitationError as exc:
        return _cannot(gate_id, f"grant ledger unreadable: {exc}",
                       "Fix or restore the ledger file; an unreadable ledger is never "
                       "read as 'nothing revoked'.")
    return PoolAdmission(ADMITTED, gate_id, "invite verified", invitation_id=inv.id,
                         to=inv.to, spent=spend)


# ── CLI wiring ──────────────────────────────────────────────────────────────


def add_arguments(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("admission (awnboard invite)")
    g.add_argument("--invite", default="", help=f"invite token (or env {INVITE_ENV})")
    g.add_argument("--invite-file", default="", help="read the invite token from a file")
    g.add_argument("--as", dest="invite_as", default="",
                   help="who you are: the address the invite was sent to")
    g.add_argument("--invite-ledger", default="",
                   help=f"awnboard grant ledger (or env {LEDGER_ENV})")
    g.add_argument("--invite-secret-file", default="",
                   help=f"file holding the pool's awnboard key (or env {SECRET_ENV})")


def _read_text(path: str) -> str:
    return Path(path).expanduser().read_text(encoding="utf-8").strip()


def admit_from_args(args: argparse.Namespace, model: str,
                    spend: bool = False) -> PoolAdmission:
    """Resolve token, presenter, key and ledger from args/env and verify."""
    gate_id = pool_gate(model)
    try:
        from awnboard import GrantLedger, HmacSigner
    except ImportError:
        return _cannot(gate_id, "awnboard is not installed",
                       "pip install awnboard  (the pool's admission gate)")
    try:
        token = (getattr(args, "invite", "") or "").strip()
        if not token and getattr(args, "invite_file", ""):
            token = _read_text(args.invite_file)
        if not token:
            token = os.environ.get(INVITE_ENV, "").strip()
        secret_file = getattr(args, "invite_secret_file", "")
        secret = _read_text(secret_file) if secret_file else os.environ.get(SECRET_ENV, "")
    except OSError as exc:
        return _cannot(gate_id, f"could not read an admission input: {exc}",
                       "Check the --invite-file / --invite-secret-file path.")
    if not secret:
        return _cannot(gate_id, "no pool key to verify the invite with",
                       f"Set {SECRET_ENV} or pass --invite-secret-file.")
    try:
        signer = HmacSigner(secret)
    except ValueError as exc:
        return _cannot(gate_id, str(exc), f"Use the pool's real key in {SECRET_ENV}.")
    ledger_path = getattr(args, "invite_ledger", "") or os.environ.get(LEDGER_ENV, "")
    ledger = None
    if ledger_path:
        # awnboard reads a MISSING ledger file as empty ("nothing revoked"), so a
        # mistyped path would silently un-revoke every invite. Require the file to
        # exist; checking first also stops GrantLedger creating directories.
        lp = Path(ledger_path).expanduser()
        if not lp.is_file():
            return _cannot(gate_id, f"grant ledger {lp} does not exist, so revocation "
                           "cannot be checked",
                           "Point --invite-ledger at the pool's ledger file. A new pool "
                           'starts it as {"revoked": {}, "used": {}}.')
        ledger = GrantLedger(lp)
    return verify_pool_invite(token, getattr(args, "invite_as", "") or "", model=model,
                              signer=signer, ledger=ledger, spend=spend)


# ── self-test ───────────────────────────────────────────────────────────────


def self_test() -> int:
    """0 pass, 1 a gate let something through, 2 cannot judge (awnboard absent)."""
    import tempfile

    try:
        from awnboard import GrantLedger, HmacSigner, invite
    except ImportError:
        print("pool_admission self-test: CANNOT JUDGE (awnboard not installed)",
              file=sys.stderr)
        return 2

    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    model = "deepseek-v4.1-flash-1017"
    gate_id = pool_gate(model)
    signer = HmacSigner("pool-admission-self-test-key")
    with tempfile.TemporaryDirectory() as d:
        ledger = GrantLedger(os.path.join(d, "grants.json"))
        tok = invite(signer, gate=gate_id, to="sam@example.com", uses=1, ttl_s=3600,
                     inv_id="inv-sam")

        ok = verify_pool_invite(tok, "Sam@Example.com", model=model, signer=signer,
                                ledger=ledger)
        check(ok.admitted, f"the addressee was refused: {ok.why}")
        check(ledger.uses("inv-sam") == 0, "a dry-run check spent a use")

        check(verify_pool_invite("", "sam@example.com", model=model, signer=signer,
                                 ledger=ledger).verdict == REFUSED, "a missing invite admitted")
        check(verify_pool_invite(tok, "eve@example.com", model=model, signer=signer,
                                 ledger=ledger).verdict == REFUSED, "a forwarded invite admitted")
        check(verify_pool_invite(tok, "sam@example.com", model="deepseek-v4-flash",
                                 signer=signer, ledger=ledger).verdict == REFUSED,
              "an invite for one pool opened another")
        check(verify_pool_invite(tok, "sam@example.com", model=model,
                                 signer=HmacSigner("a-different-pool-key-xx"),
                                 ledger=ledger).verdict == REFUSED,
              "an invite verified under the wrong key")
        check(verify_pool_invite(tok, "sam@example.com", model=model, signer=signer,
                                 ledger=None).verdict == CANNOT_JUDGE,
              "no ledger must be CANNOT_JUDGE, not admitted")

        spent = verify_pool_invite(tok, "sam@example.com", model=model, signer=signer,
                                   ledger=ledger, spend=True)
        check(spent.admitted and spent.spent, "registration could not spend the invite")
        check(verify_pool_invite(tok, "sam@example.com", model=model, signer=signer,
                                 ledger=ledger).verdict == REFUSED, "a spent invite admitted")

        tok2 = invite(signer, gate=gate_id, to="ana@example.com", uses=3, ttl_s=3600,
                      inv_id="inv-ana")
        ledger.revoke("inv-ana", reason="self-test")
        r = verify_pool_invite(tok2, "ana@example.com", model=model, signer=signer,
                               ledger=ledger)
        check(r.verdict == REFUSED and "revoked" in r.why, "a REVOKED invite admitted")

        bad = Path(d) / "torn.json"
        bad.write_text("{not json", encoding="utf-8")
        t = verify_pool_invite(tok2, "ana@example.com", model=model, signer=signer,
                               ledger=GrantLedger(bad))
        check(t.verdict == CANNOT_JUDGE, "an unreadable ledger was read as 'nothing revoked'")

    for f in failures:
        print(f"FAIL {f}", file=sys.stderr)
    print(f"pool_admission self-test: {'PASS' if not failures else 'FAIL'} "
          f"({len(failures)} failure(s))")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="adk.pool_admission", description=__doc__.split("\n")[0])
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--model", default="deepseek-v4.1-flash-1017")
    add_arguments(p)
    args = p.parse_args(argv)
    if args.self_test:
        return self_test()
    adm = admit_from_args(args, args.model)
    print(adm.message(), file=sys.stdout if adm.admitted else sys.stderr)
    return adm.exit_code


if __name__ == "__main__":
    sys.exit(main())
