"""Slumber's consolidation as a library: ``consolidate_contracts()`` (design section 6),
the replacement for the phase-4 placeholder in ``AitherSlumber._promote_memories``.

1. **Promote** -- a ``rule:`` fact that is ``verified``, HELD in at least ``min_scopes``
   distinct scopes (across the saved records) with the same source, and not contradicted
   in any record, becomes a contract line in ``contracts/<domain>.jsonl`` with its
   evidence refs, scopes, source and stored sample transitions.
2. **Prune** -- a contract whose key is ``contradicted`` in the latest scope that saw it
   is removed, and its source fingerprint joins the refuted list so it cannot return.
3. **Replay-check** -- every remaining contract replays its stored samples; a failure
   demotes it (status ``contradicted`` in the file).

Owner records are never promoted.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List

from .bus import MemoryStrata
from .daydream import audit_contract
from .hub import contracts_path

__all__ = ["consolidate_contracts", "fingerprint", "refuted_path"]


def fingerprint(source: str) -> str:
    return hashlib.sha256(" ".join(source.split()).encode("utf-8")).hexdigest()[:16]


def refuted_path(tenant: str, domain: str) -> str:
    return "aither://warm/contracts/%s/%s.refuted.jsonl" % (tenant, domain)


def _records(strata: MemoryStrata, tenant: str) -> List[Dict[str, Any]]:
    out = []
    for p in strata.list("aither://warm/context/%s/" % tenant):
        if p.endswith("/record.json"):
            out.append(strata.read_json(p))
    return out


def consolidate_contracts(
    strata: MemoryStrata, tenant: str, domain: str, *, min_scopes: int = 2
) -> Dict[str, Any]:
    path = contracts_path(tenant, domain)
    existing = {c["key"]: c for c in strata.read_lines(path)}
    refuted = {r["fingerprint"] for r in strata.read_lines(refuted_path(tenant, domain))}
    held: Dict[str, Dict[str, Any]] = {}
    contradicted: Dict[str, List[str]] = {}
    for rec in _records(strata, tenant):
        for key, f in rec.get("facts", {}).items():
            if not key.startswith("rule:") or f["provenance"] != "verified":
                continue
            if f["status"] == "contradicted":
                contradicted.setdefault(key, []).append(f["scope"])
                continue
            if f["status"] != "held":
                continue
            v = f["value"] or {}
            fp = fingerprint(v.get("source", ""))
            h = held.setdefault(
                key + "#" + fp,
                {"key": key, "fp": fp, "value": v, "scopes": set(), "refs": []},
            )
            h["scopes"].add(f["scope"])
            h["refs"].extend(e[1] for e in f["evidence"] if e[0] == "replay")
    report: Dict[str, List[str]] = {"promoted": [], "pruned": [], "demoted": [], "kept": []}

    # 2. prune (before promotion, so a contradicted rule cannot be re-promoted)
    for key, c in list(existing.items()):
        if key in contradicted:
            existing.pop(key)
            if c.get("fingerprint") not in refuted:
                strata.append_line(
                    refuted_path(tenant, domain),
                    {"key": key, "fingerprint": c.get("fingerprint"), "scopes": contradicted[key]},
                )
                refuted.add(c.get("fingerprint"))
            report["pruned"].append(key)

    # 1. promote
    for h in sorted(held.values(), key=lambda h: (h["key"], h["fp"])):
        if h["key"] in contradicted or h["fp"] in refuted or len(h["scopes"]) < min_scopes:
            continue
        if h["key"] in existing and existing[h["key"]].get("fingerprint") == h["fp"]:
            c = existing[h["key"]]
            c["scopes"] = sorted(set(c["scopes"]) | h["scopes"])
            report["kept"].append(h["key"])
            continue
        existing[h["key"]] = {
            "key": h["key"],
            "domain": domain,
            "status": "held",
            "provenance": "verified",
            "fingerprint": h["fp"],
            "source": h["value"].get("source", ""),
            "samples": h["value"].get("samples", []),
            "scopes": sorted(h["scopes"]),
            "evidence": sorted(set(h["refs"])),
        }
        report["promoted"].append(h["key"])

    # 3. replay-check every contract on its stored samples (ground truth)
    for key, c in existing.items():
        if c.get("status") != "held":
            continue
        for s in c.get("samples") or []:
            ok, why = audit_contract(
                {"source": c["source"], "samples": [s]}, s["before"], s["world"], key
            )
            if not ok:
                c["status"] = "contradicted"
                c["demoted_because"] = why
                report["demoted"].append(key)
                break

    strata.write(path, b"")
    for key in sorted(existing):
        strata.append_line(path, existing[key])
    return report
