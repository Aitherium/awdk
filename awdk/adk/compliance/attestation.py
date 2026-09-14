"""DataBoundaryAttestation — Compliance report generation for AitherShell.

Self-contained port of AitherOS lib/compliance/DataBoundaryAttestation.py.
Reads from the agent's local call log instead of LLMGateway singleton.
No AitherOS service dependencies.

Usage:
    from adk.compliance.attestation import generate_attestation_report

    report = await generate_attestation_report(
        start=datetime(2026, 4, 1, tzinfo=timezone.utc),
        end=datetime(2026, 4, 27, tzinfo=timezone.utc),
        call_log=agent._call_log,
    )
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("adk.compliance.attestation")


@dataclass
class LLMCallSummary:
    """Summary of LLM calls in the attestation window."""
    total_calls: int = 0
    local_vllm_calls: int = 0
    local_ollama_calls: int = 0
    cloud_calls: int = 0
    failed_calls: int = 0
    models_used: List[str] = field(default_factory=list)
    backend_breakdown: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_calls": self.total_calls,
            "local_vllm_calls": self.local_vllm_calls,
            "local_ollama_calls": self.local_ollama_calls,
            "cloud_calls": self.cloud_calls,
            "failed_calls": self.failed_calls,
            "models_used": self.models_used,
            "backend_breakdown": self.backend_breakdown,
        }


@dataclass
class AttestationReport:
    """Complete data boundary attestation report."""
    report_id: str = ""
    generated_at: str = ""
    window_start: str = ""
    window_end: str = ""
    air_gap_enforced: bool = False
    air_gap_mode: str = "disabled"
    air_gap_activated_at: Optional[str] = None
    llm_summary: LLMCallSummary = field(default_factory=LLMCallSummary)
    violations: List[Dict[str, Any]] = field(default_factory=list)
    outbound_connections: List[Dict[str, Any]] = field(default_factory=list)
    compliance_events: List[Dict[str, Any]] = field(default_factory=list)
    node_id: str = ""
    content_hash: str = ""
    signature: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "report_id": self.report_id,
            "generated_at": self.generated_at,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "air_gap": {
                "enforced": self.air_gap_enforced,
                "mode": self.air_gap_mode,
                "activated_at": self.air_gap_activated_at,
            },
            "llm_summary": self.llm_summary.to_dict(),
            "violations": self.violations,
            "outbound_connections": self.outbound_connections,
            "compliance_events": self.compliance_events,
            "node_id": self.node_id,
            "integrity": {
                "content_hash": self.content_hash,
                "signature": self.signature,
                "algorithm": "HMAC-SHA256",
            },
        }


def _get_signing_key() -> bytes:
    return os.environ.get(
        "AITHER_AUDIT_SIGNING_KEY", "aither-attestation-default"
    ).encode()


def _sign_report(report: AttestationReport) -> AttestationReport:
    """Compute content hash and HMAC signature for tamper evidence."""
    payload = (
        f"{report.report_id}:{report.window_start}:{report.window_end}:"
        f"{report.llm_summary.total_calls}:{report.llm_summary.cloud_calls}:"
        f"{len(report.violations)}:{report.air_gap_enforced}"
    )
    report.content_hash = hashlib.sha256(payload.encode()).hexdigest()
    report.signature = hmac_mod.new(
        _get_signing_key(), report.content_hash.encode(), hashlib.sha256
    ).hexdigest()
    return report


def verify_report_signature(report_dict: Dict[str, Any]) -> bool:
    """Verify a report's HMAC signature."""
    integrity = report_dict.get("integrity", {})
    content_hash = integrity.get("content_hash", "")
    signature = integrity.get("signature", "")
    if not content_hash or not signature:
        return False
    expected = hmac_mod.new(
        _get_signing_key(), content_hash.encode(), hashlib.sha256
    ).hexdigest()
    return hmac_mod.compare_digest(expected, signature)


def _collect_llm_summary(
    start: datetime, end: datetime, call_log: List[Dict[str, Any]]
) -> LLMCallSummary:
    """Collect LLM call statistics from the agent's call log."""
    summary = LLMCallSummary()
    start_iso = start.isoformat()
    end_iso = end.isoformat()

    for entry in call_log:
        ts = entry.get("timestamp", "")
        if ts and start_iso <= ts <= end_iso:
            summary.total_calls += 1
            backend = entry.get("backend_type", "unknown")
            summary.backend_breakdown[backend] = (
                summary.backend_breakdown.get(backend, 0) + 1
            )
            if backend in ("local_vllm", "vllm"):
                summary.local_vllm_calls += 1
            elif backend in ("local_ollama", "ollama"):
                summary.local_ollama_calls += 1
            elif backend.startswith("cloud_") or backend in ("openai", "anthropic", "gateway"):
                summary.cloud_calls += 1
            if entry.get("failed"):
                summary.failed_calls += 1
            model = entry.get("model", "")
            if model and model not in summary.models_used:
                summary.models_used.append(model)

    return summary


def _collect_violations(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """Collect air-gap violations from the enforcer."""
    try:
        from adk.compliance.air_gap import get_air_gap_enforcer
        enforcer = get_air_gap_enforcer()
        all_violations = enforcer.get_violations(limit=10000)
        start_iso = start.isoformat()
        end_iso = end.isoformat()
        return [
            v for v in all_violations
            if start_iso <= v.get("timestamp", "") <= end_iso
        ]
    except Exception:
        return []


def _collect_audit_events(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """Collect compliance audit events from the JSONL audit log."""
    events = []
    data_dir = Path(os.environ.get("AITHER_DATA_DIR", os.path.expanduser("~/.aither")))
    audit_path = data_dir / "compliance" / "audit.jsonl"

    if not audit_path.exists():
        return events

    start_iso = start.isoformat()
    end_iso = end.isoformat()

    try:
        with open(audit_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    event = record.get("event", {})
                    ts = event.get("timestamp", "")
                    if ts and start_iso <= ts <= end_iso:
                        events.append(event)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.debug("[Attestation] Audit event collection failed: %s", e)

    return events


def _get_node_id() -> str:
    """Get the current node's identity."""
    node_id = os.environ.get("AITHER_NODE_ID", "")
    if node_id:
        return node_id
    import platform
    return f"{platform.node()}-standalone"


async def generate_attestation_report(
    start: datetime,
    end: datetime,
    call_log: Optional[List[Dict[str, Any]]] = None,
) -> AttestationReport:
    """Generate a complete data boundary attestation report.

    Args:
        start: Window start (UTC)
        end: Window end (UTC)
        call_log: LLM call log entries (from AitherAgent._call_log)

    Returns:
        Signed AttestationReport
    """
    report = AttestationReport(
        report_id=f"att-{uuid.uuid4().hex[:12]}",
        generated_at=datetime.now(timezone.utc).isoformat(),
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        node_id=_get_node_id(),
    )

    # Air-gap state
    try:
        from adk.compliance.air_gap import get_air_gap_enforcer
        enforcer = get_air_gap_enforcer()
        att = enforcer.get_attestation_state()
        report.air_gap_enforced = att.enforced
        report.air_gap_mode = att.mode
        report.air_gap_activated_at = att.activated_at
    except Exception:
        pass

    # LLM call summary
    report.llm_summary = _collect_llm_summary(start, end, call_log or [])

    # Violations
    report.violations = _collect_violations(start, end)

    # Audit events
    report.compliance_events = _collect_audit_events(start, end)

    # Sign
    report = _sign_report(report)

    logger.info(
        "[Attestation] Report %s: %d LLM calls, %d cloud, %d violations",
        report.report_id,
        report.llm_summary.total_calls,
        report.llm_summary.cloud_calls,
        len(report.violations),
    )

    return report
