"""AirGapEnforcer — Proactive air-gap enforcement for regulated deployments.

Self-contained port of AitherOS lib/compliance/AirGapEnforcer.py.
No FluxEmitter, no CustomAuditEvents — uses file-based JSONL audit log instead.

Two modes:
  - strict: raises AirGapViolation on any outbound attempt
  - audit:  logs violations but allows (for migration testing)

Usage:
    from adk.compliance.air_gap import get_air_gap_enforcer, AirGapViolation

    enforcer = get_air_gap_enforcer()
    if enforcer.is_enforced():
        # Cloud calls are blocked
        ...

    # Guard a specific call:
    enforcer.check_allowed("cloud_providers", detail="anthropic chat()")
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import ipaddress
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("adk.compliance.air_gap")


class AirGapViolation(Exception):
    """Raised when an air-gap-violating operation is attempted in strict mode."""

    def __init__(self, subsystem: str, detail: str = ""):
        self.subsystem = subsystem
        self.detail = detail
        msg = f"Air-gap violation: {subsystem}"
        if detail:
            msg += f" -- {detail}"
        super().__init__(msg)


class EnforcementMode(str, Enum):
    STRICT = "strict"
    AUDIT = "audit"


@dataclass
class AirGapAttestation:
    """Snapshot of air-gap enforcement state."""
    enforced: bool = False
    mode: str = "disabled"
    activated_at: Optional[str] = None
    violations_total: int = 0
    violations_since_last_attestation: int = 0
    blocked_subsystems: List[str] = field(default_factory=list)
    allowed_subnets: List[str] = field(default_factory=list)
    config_hash: str = ""
    attestation_time: str = ""
    signature: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enforced": self.enforced,
            "mode": self.mode,
            "activated_at": self.activated_at,
            "violations_total": self.violations_total,
            "violations_since_last_attestation": self.violations_since_last_attestation,
            "blocked_subsystems": self.blocked_subsystems,
            "allowed_subnets": self.allowed_subnets,
            "config_hash": self.config_hash,
            "attestation_time": self.attestation_time,
            "signature": self.signature,
        }


@dataclass
class ViolationRecord:
    """A recorded air-gap violation."""
    timestamp: str
    subsystem: str
    detail: str
    action_taken: str  # "blocked" or "logged"


class AirGapEnforcer:
    """Singleton air-gap enforcement engine.

    Config resolution order:
    1. Explicit config_path argument
    2. ~/.aither/air_gap.yaml (user config)
    3. Bundled default (enforcement disabled)
    """

    # Default blocked subsystems for regulated deployments
    _DEFAULT_BLOCKED = [
        "cloud_providers", "cloud_llm", "external_api",
        "phonehome", "telemetry", "mesh_network",
    ]

    def __init__(
        self,
        config_path: Optional[Path] = None,
        *,
        enabled: Optional[bool] = None,
        mode: Optional[str] = None,
    ):
        self._config_path = config_path or self._default_config_path()
        self._enabled: bool = False
        self._mode: EnforcementMode = EnforcementMode.STRICT
        self._activated_at: Optional[str] = None
        self._blocked_subsystems: List[str] = []
        self._allowed_subsystems: List[str] = []
        self._allowed_networks: List[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        self._allowed_subnets_raw: List[str] = []
        self._attestation_interval: int = 300
        self._violations: List[ViolationRecord] = []
        self._violations_since_attestation: int = 0
        self._config_hash: str = ""
        self._signing_key: bytes = os.environ.get(
            "AITHER_AUDIT_SIGNING_KEY", "aither-air-gap-default"
        ).encode()

        # Audit log path
        data_dir = Path(os.environ.get("AITHER_DATA_DIR", os.path.expanduser("~/.aither")))
        self._audit_log_path = data_dir / "compliance" / "audit.jsonl"

        self._load_config()

        # CLI overrides (--air-gap flag or config.yaml air_gap: true)
        if enabled is not None:
            if enabled and not self._enabled:
                self._enabled = True
                self._blocked_subsystems = self._blocked_subsystems or list(self._DEFAULT_BLOCKED)
                self._activate()
            elif not enabled and self._enabled:
                self._enabled = False
        if mode is not None:
            self._mode = EnforcementMode(mode)

    @staticmethod
    def _default_config_path() -> Path:
        return Path(os.environ.get(
            "AITHER_DATA_DIR", os.path.expanduser("~/.aither")
        )) / "air_gap.yaml"

    def _load_config(self):
        if not self._config_path.exists():
            logger.debug("[AirGapEnforcer] No config found at %s -- enforcement disabled", self._config_path)
            return

        try:
            import yaml
        except ImportError:
            logger.warning("[AirGapEnforcer] PyYAML not available -- enforcement disabled")
            return

        with open(self._config_path, "r") as f:
            raw = f.read()
            cfg = yaml.safe_load(raw) or {}

        self._config_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
        self._enabled = bool(cfg.get("enabled", False))
        self._mode = EnforcementMode(cfg.get("enforcement", "strict"))
        self._blocked_subsystems = cfg.get("blocked_subsystems", list(self._DEFAULT_BLOCKED))
        self._allowed_subsystems = cfg.get("allowed_subsystems", [])
        self._allowed_subnets_raw = cfg.get("allowed_subnets", [])
        self._attestation_interval = cfg.get("attestation_interval", 300)

        self._allowed_networks = []
        for subnet in self._allowed_subnets_raw:
            try:
                self._allowed_networks.append(ipaddress.ip_network(subnet, strict=False))
            except ValueError:
                logger.warning("[AirGapEnforcer] Invalid subnet: %s", subnet)

        if self._enabled:
            self._activate()

    def _activate(self):
        """Set env vars to signal air-gap mode to all subsystems."""
        self._activated_at = datetime.now(timezone.utc).isoformat()

        os.environ["AITHER_CLOUD_MODE"] = "local_only"
        os.environ["AITHER_LLM_OFFLINE_MODE"] = "true"
        os.environ["AITHER_PHONEHOME_DISABLED"] = "true"

        logger.info(
            "[AirGapEnforcer] ACTIVATED -- mode=%s, blocked=%s",
            self._mode.value, self._blocked_subsystems,
        )

        self._write_audit_event("air_gap_enforced", {
            "mode": self._mode.value,
            "blocked_subsystems": self._blocked_subsystems,
            "config_hash": self._config_hash,
        })

    # ---- Public API ----

    def is_enforced(self) -> bool:
        return self._enabled

    def get_mode(self) -> str:
        return self._mode.value if self._enabled else "disabled"

    def check_allowed(self, subsystem: str, detail: str = ""):
        """Check if a subsystem operation is allowed. Raises or logs."""
        if not self._enabled:
            return
        if subsystem in self._blocked_subsystems:
            self._record_violation(subsystem, detail)
            if self._mode == EnforcementMode.STRICT:
                raise AirGapViolation(subsystem, detail)

    def check_destination_allowed(self, address: str) -> bool:
        """Check if a network destination is within allowed subnets."""
        if not self._enabled:
            return True
        try:
            addr = ipaddress.ip_address(address)
            return any(addr in net for net in self._allowed_networks)
        except ValueError:
            if self._mode == EnforcementMode.STRICT:
                return False
            return True

    def get_attestation_state(self) -> AirGapAttestation:
        """Generate a signed attestation of current air-gap state."""
        now = datetime.now(timezone.utc).isoformat()
        att = AirGapAttestation(
            enforced=self._enabled,
            mode=self._mode.value if self._enabled else "disabled",
            activated_at=self._activated_at,
            violations_total=len(self._violations),
            violations_since_last_attestation=self._violations_since_attestation,
            blocked_subsystems=list(self._blocked_subsystems),
            allowed_subnets=list(self._allowed_subnets_raw),
            config_hash=self._config_hash,
            attestation_time=now,
        )

        payload = (
            f"{att.enforced}:{att.mode}:{att.violations_total}:"
            f"{att.config_hash}:{att.attestation_time}"
        )
        att.signature = hmac_mod.new(
            self._signing_key, payload.encode(), hashlib.sha256
        ).hexdigest()

        self._violations_since_attestation = 0
        return att

    def get_violations(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return recent violations."""
        return [
            {
                "timestamp": v.timestamp,
                "subsystem": v.subsystem,
                "detail": v.detail,
                "action_taken": v.action_taken,
            }
            for v in self._violations[-limit:]
        ]

    def enable(self):
        """Enable air-gap enforcement at runtime."""
        if self._enabled:
            return
        self._enabled = True
        self._mode = EnforcementMode.STRICT
        self._blocked_subsystems = self._blocked_subsystems or list(self._DEFAULT_BLOCKED)
        self._activate()

    def disable(self):
        """Disable air-gap enforcement at runtime."""
        if not self._enabled:
            return
        self._enabled = False
        os.environ.pop("AITHER_CLOUD_MODE", None)
        os.environ.pop("AITHER_LLM_OFFLINE_MODE", None)
        os.environ.pop("AITHER_PHONEHOME_DISABLED", None)
        logger.info("[AirGapEnforcer] DEACTIVATED")
        self._write_audit_event("air_gap_disabled", {})

    def reload_config(self):
        """Hot-reload configuration."""
        was_enabled = self._enabled
        self._load_config()
        if was_enabled and not self._enabled:
            self.disable()

    # ---- Internal ----

    def _record_violation(self, subsystem: str, detail: str):
        action = "blocked" if self._mode == EnforcementMode.STRICT else "logged"
        record = ViolationRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            subsystem=subsystem,
            detail=detail,
            action_taken=action,
        )
        self._violations.append(record)
        self._violations_since_attestation += 1

        level = logging.WARNING if self._mode == EnforcementMode.STRICT else logging.INFO
        logger.log(level, "[AirGapEnforcer] Violation: %s -- %s [%s]", subsystem, detail, action)

        self._write_audit_event("air_gap_violation", {
            "subsystem": subsystem,
            "detail": detail,
            "action_taken": action,
        })

    def _write_audit_event(self, action: str, metadata: Dict[str, Any]):
        """Append HMAC-signed audit event to JSONL file."""
        try:
            self._audit_log_path.parent.mkdir(parents=True, exist_ok=True)
            event = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": action,
                "actor": "air_gap_enforcer",
                "metadata": metadata,
            }
            event_json = json.dumps(event, separators=(",", ":"))
            sig = hmac_mod.new(
                self._signing_key, event_json.encode(), hashlib.sha256
            ).hexdigest()[:32]
            signed = json.dumps({"event": event, "sig": sig}, separators=(",", ":"))
            with open(self._audit_log_path, "a", encoding="utf-8") as f:
                f.write(signed + "\n")
        except Exception as e:
            logger.debug("[AirGapEnforcer] Audit log write failed: %s", e)


# ---- Singleton ----

_enforcer: Optional[AirGapEnforcer] = None


def get_air_gap_enforcer(
    config_path: Optional[Path] = None,
    *,
    enabled: Optional[bool] = None,
    mode: Optional[str] = None,
) -> AirGapEnforcer:
    """Get or create the singleton AirGapEnforcer."""
    global _enforcer
    if _enforcer is None:
        _enforcer = AirGapEnforcer(config_path, enabled=enabled, mode=mode)
    return _enforcer


def reset_enforcer():
    """Reset the singleton (for testing)."""
    global _enforcer
    _enforcer = None


def is_air_gap_enforced() -> bool:
    """Quick check: is air-gap enforcement active?"""
    if _enforcer is None:
        return False
    return _enforcer.is_enforced()
