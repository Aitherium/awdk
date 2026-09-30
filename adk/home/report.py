"""``adk home report`` -- a device-signed data-boundary report for Agent Home.

One document a household, a school or an auditor can keep: where the agent's model
runs, what the egress guard is set to and what it logged, what the agent did (counted
from the signed receipts, never from the model's own account), and whether that
receipts log verifies. Composed from the modules that already hold each fact:

* model and boundary -- :mod:`adk.home.models` + the home config
* model license      -- :mod:`adk.compliance.model_licenses`
* egress policy      -- the ``air_gap.yaml`` layer (READ, never activated: building an
                        enforcer flips process-wide offline flags, and a report must not
                        change what it reports on -- same rule as ``adk home trust``)
* egress events      -- ``compliance/audit.jsonl`` via :mod:`adk.compliance.attestation`
* receipts verdict   -- :func:`adk.receipts.check` (0 intact, 1 tampered, 2 cannot judge)
* PDF                -- :func:`adk.compliance.pdf_report.generate_attestation_pdf`

SIGNING

The report is signed with the SAME device key as the receipts (the awseal key, else
``~/.aither/agent-home/receipt.key``), Ed25519 over the canonical JSON of everything
but the ``integrity`` block. The signature therefore names a ``key_id`` that also
appears on every receipt row, and the public key is embedded so a recipient can pin
it out of band. With no key, the report says ``signed: false`` -- never a fake
signature. :func:`verify_report` returns 0 valid, 1 tampered, 2 cannot judge (no
signature, or a key this machine does not trust).

PRIVACY

Only counts, tool NAMES, the model endpoint and egress destinations are included.
Receipt argument and result previews are never copied into the report.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from . import config as hc
from . import models

REPORT_KIND = "agent-home-data-boundary"
#: How many egress events / tool names the report lists (all are COUNTED).
LIST_LIMIT = 50


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _receipt_ts(dt: datetime) -> str:
    """The receipts log's own timestamp shape (``adk.receipts.append``)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _host_is_loopback(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def model_boundary(cfg: hc.ModelConfig) -> Dict[str, Any]:
    """Where the model's prompts go. ``on-device`` only for a loopback endpoint."""
    info = models.describe(cfg)
    info.pop("hint", None)
    preset = models.PRESETS.get(cfg.provider)
    # An unset base_url means the preset's endpoint (``build_llm`` passes None and the
    # router falls back to it), so judge THAT endpoint, not an empty string.
    url = cfg.base_url or (preset.base_url if preset else "")
    info["base_url"] = url
    info["model"] = cfg.model or (preset.model if preset else "")
    host = urlparse(url).hostname or ""
    if cfg.mode == "byo":
        boundary = "cloud"
        why = (f"bring-your-own-key: prompts go to {cfg.provider} "
               f"({host or 'provider default endpoint'})")
    elif _host_is_loopback(host):
        boundary = "on-device"
        why = f"local model on loopback ({host})"
    else:
        boundary = "local-network"
        why = f"local-mode model, but the endpoint host {host or '?'} is not loopback"
    info.update({"boundary": boundary, "boundary_reason": why})
    return info


def model_license(model_id: str) -> Optional[Dict[str, Any]]:
    """The bundled license entry for this model id, or None when it is not listed."""
    if not model_id:
        return None
    from adk.compliance.model_licenses import get_model_license_registry

    info = get_model_license_registry().get_license(model_id)
    return info.to_dict() if info is not None else None


def egress_policy(air_gap_path: Path) -> Dict[str, Any]:
    """The configured egress mode, read from the file (the enforcer is NOT built)."""
    from adk.compliance import air_gap

    cfg, _digest, err = air_gap.AirGapEnforcer._read_layer(air_gap_path)
    mode = "disabled"
    if cfg and cfg.get("enabled"):
        mode = str(cfg.get("enforcement") or "strict")
    return {"config": str(air_gap_path), "present": air_gap_path.exists(), "mode": mode,
            "env_override": os.environ.get("AITHER_AIR_GAP") or None,
            "config_error": err}


def egress_events(start: datetime, end: datetime) -> Tuple[List[Dict[str, Any]],
                                                              Dict[str, int]]:
    """(egress rows in the window, counts of every audit action in the window)."""
    from adk.compliance.attestation import _collect_audit_events

    events = _collect_audit_events(start, end)
    actions = Counter(str(e.get("action") or "?") for e in events)
    rows: List[Dict[str, Any]] = []
    for e in events:
        meta = e.get("metadata") or {}
        if e.get("action") != "air_gap_violation":
            continue
        rows.append({"timestamp": str(e.get("timestamp") or ""),
                     "subsystem": str(meta.get("subsystem") or ""),
                     "detail": str(meta.get("detail") or ""),
                     "action_taken": str(meta.get("action_taken") or "")})
    return rows, dict(actions)


def receipts_summary(path: Path, start: datetime, end: datetime) -> Dict[str, Any]:
    """Verdict over the WHOLE log plus counts for the window. No previews copied."""
    from adk import receipts

    code, reason = receipts.check(path)
    verdict = {0: "intact", 1: "TAMPERED"}.get(code, "cannot judge")
    lo, hi = _receipt_ts(start), _receipt_ts(end)
    kinds: Counter = Counter()
    tools: Counter = Counter()
    refused = unsigned = total = in_window = 0
    key_ids = set()
    if path.is_file():
        for raw in path.read_bytes().split(b"\n"):
            if not raw.strip():
                continue
            total += 1
            try:
                row = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue  # check() above already reports an unparseable line
            if not isinstance(row, dict):
                continue
            if row.get("key_id"):
                key_ids.add(str(row["key_id"]))
            ts = str(row.get("ts") or "")
            if not (lo <= ts <= hi):
                continue
            in_window += 1
            kind = str(row.get("kind") or "?")
            kinds[kind] += 1
            if kind == "tool":
                tools[str(row.get("name") or "?")] += 1
            if str(row.get("approval") or "").startswith("refused"):
                refused += 1
            if not row.get("signed"):
                unsigned += 1
    return {"path": str(path), "verify": {"code": code, "verdict": verdict,
                                          "reason": reason},
            "rows_total": total, "rows_in_window": in_window,
            "by_kind": dict(kinds),
            "tools": dict(tools.most_common(LIST_LIMIT)),
            "refused_in_window": refused, "unsigned_in_window": unsigned,
            "key_ids": sorted(key_ids)}


# ── signing ─────────────────────────────────────────────────────────────────────

def _canonical_body(report: Dict[str, Any]) -> bytes:
    body = {k: v for k, v in report.items() if k != "integrity"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str).encode("utf-8")


def sign_report(report: Dict[str, Any], key_path: Any = None) -> Dict[str, Any]:
    """Attach the ``integrity`` block, signed with the receipts' device key."""
    from adk import receipts

    body = _canonical_body(report)
    integrity: Dict[str, Any] = {"content_hash": hashlib.sha256(body).hexdigest(),
                                 "algorithm": "Ed25519", "signed": False,
                                 "signature": "", "key_id": "", "public_key": ""}
    key = receipts._signing_key(key_path) if receipts._crypto() is not None else None
    if key is not None:
        pub = receipts._pub_hex(key)
        integrity.update({"signed": True, "signature": key.sign(body).hex(),
                          "key_id": receipts._key_id(pub), "public_key": pub})
    report["integrity"] = integrity
    return report


def verify_report(report: Dict[str, Any], pubkey: Optional[str] = None,
                  key_path: Any = None) -> Tuple[int, str]:
    """0 valid · 1 tampered · 2 cannot judge. Trusts only keys this machine holds
    (or ``pubkey`` / ``$AITHER_RECEIPTS_PUBKEY``), never the embedded public key."""
    from adk import receipts

    integ = report.get("integrity") if isinstance(report, dict) else None
    if not isinstance(integ, dict):
        return 2, "no integrity block"
    body = _canonical_body(report)
    if hashlib.sha256(body).hexdigest() != integ.get("content_hash"):
        return 1, "content hash does not match the report body (edited after signing)"
    if not integ.get("signed"):
        return 2, "report is unsigned (no device key when it was made)"
    crypto = receipts._crypto()
    if crypto is None:
        return 2, "cryptography is not installed; cannot check an Ed25519 signature"
    key_id = str(integ.get("key_id") or "")
    trusted = receipts._trusted_pubkeys(pubkey, key_path)
    pub_hex = trusted.get(key_id)
    if pub_hex is None:
        return 2, (f"signed by key_id {key_id or '?'}, which this machine does not "
                   "trust; pass --pubkey <hex> obtained from the device owner")
    try:
        crypto[2].from_public_bytes(bytes.fromhex(pub_hex)).verify(
            bytes.fromhex(str(integ.get("signature") or "")), body)
    except Exception:  # noqa: BLE001 -- InvalidSignature or malformed hex
        return 1, "bad signature"
    return 0, f"valid: signed by key_id {key_id}"


# ── compose ─────────────────────────────────────────────────────────────────────

def build_report(cfg: hc.HomeConfig, receipts_file: Path, air_gap_path: Path,
                 days: int = 30, now: Optional[datetime] = None,
                 key_path: Any = None) -> Dict[str, Any]:
    """The signed report dict (also the input :func:`render_pdf` takes)."""
    from adk.compliance.attestation import _get_node_id

    end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = end - timedelta(days=max(1, int(days)))
    model = model_boundary(cfg.model)
    policy = egress_policy(air_gap_path)
    egress_rows, audit_actions = egress_events(start, end)
    rcpt = receipts_summary(Path(receipts_file), start, end)
    report: Dict[str, Any] = {
        "kind": REPORT_KIND,
        "report_id": f"home-{uuid.uuid4().hex[:12]}",
        "generated_at": _iso(end),
        "window_start": _iso(start),
        "window_end": _iso(end),
        "node_id": _get_node_id(),
        "agent": cfg.name,
        "harness": cfg.harness.kind,
        "model": model,
        "model_license": model_license(cfg.model.model),
        "air_gap": {"enforced": policy["mode"] == "strict", "mode": policy["mode"],
                    "activated_at": None},
        "egress_policy": policy,
        "audit_actions": audit_actions,
        "egress_events_total": len(egress_rows),
        "violations": egress_rows[:LIST_LIMIT],
        "receipts": rcpt,
    }
    return sign_report(report, key_path=key_path)


def render_pdf(report: Dict[str, Any]) -> bytes:
    """PDF bytes through the shared compliance renderer (needs ``awdk[pdf]``)."""
    from adk.compliance.pdf_report import generate_attestation_pdf

    return generate_attestation_pdf(report)


__all__ = ["REPORT_KIND", "build_report", "egress_policy", "model_boundary",
           "model_license", "receipts_summary", "render_pdf", "sign_report",
           "verify_report"]
