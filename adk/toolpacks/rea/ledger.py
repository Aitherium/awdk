"""Reconstruction obligation ledger — fail-closed closure verdict.

PARITY COPY of the platform pack's ledger. The two packs MUST give the same closure
verdict, so the acceptance map here is identical to the platform's; a drift is caught
by the platform's parity check. Reimplemented on our primitives from rea-agents
(MIT, ReconstructionObligationAuthority.ts `proofAuthoritySatisfies`); not a port.

An obligation closes ONLY when it is required, a verifier passed, AND the verifier's
authority *satisfies* the required authority under the acceptance map — which is NOT
a single linear order: a weaker but comparable authority never authenticates a
stronger claim. Unknown authority, a missing verifier, or an empty ledger never
closes — absence of evidence is never closure.
"""
from __future__ import annotations

#: accepted[required] = the actual verifier authorities that satisfy `required`.
ACCEPTED = {
    "unit": ("unit", "integration", "protocol", "renderer", "packaged-process",
             "native-abi", "live-observation", "external"),
    "integration": ("integration", "protocol", "renderer", "packaged-process",
                    "native-abi", "live-observation"),
    "protocol": ("protocol", "packaged-process", "live-observation"),
    "renderer": ("renderer", "packaged-process", "live-observation"),
    "packaged-process": ("packaged-process", "live-observation"),
    "native-abi": ("native-abi",),
    "live-observation": ("live-observation",),
    "external": ("external",),
}

#: Legacy authority names the original pack used, mapped onto the upstream set.
_LEGACY = {
    "static": "unit",
    "runtime": "integration",
    "process": "packaged-process",
    "packaged-process": "packaged-process",
}


def _norm(authority):
    if authority in ACCEPTED:
        return authority
    return _LEGACY.get(authority)


def authority_satisfies(verifier_authority, required_authority) -> bool:
    """True iff a verifier of `verifier_authority` may close a `required_authority`
    obligation. Unknown authority on either side is never satisfied (fail-closed)."""
    req = _norm(required_authority)
    act = _norm(verifier_authority)
    if req is None or act is None:
        return False
    return act in ACCEPTED[req]


def close_ledger(obligations: list) -> dict:
    """Return {status, open, closed, reason?}. Both the upstream and the legacy
    field names are read (obligation_id/id, required_verifier_authority/
    original_authority, verifier_authority, verifier_passed)."""
    if not isinstance(obligations, list) or not obligations:
        return {"status": "unknown", "open": [], "closed": [],
                "reason": "no obligations: absence of evidence is not closure"}
    closed, open_ = [], []
    for raw in obligations:
        o = raw or {}
        oid = str(o.get("obligation_id", o.get("id", "?")))
        if not o.get("required", True):
            continue
        required_authority = o.get("required_verifier_authority",
                                   o.get("original_authority"))
        verifier_authority = o.get("verifier_authority")
        ok = (bool(o.get("verifier_passed"))
              and authority_satisfies(verifier_authority, required_authority))
        (closed if ok else open_).append(oid)
    return {"status": "closed" if not open_ else "open", "open": open_, "closed": closed}
