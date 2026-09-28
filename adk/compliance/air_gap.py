"""AirGapEnforcer — Proactive air-gap enforcement for regulated deployments.

Self-contained port of AitherOS lib/compliance/AirGapEnforcer.py.
No FluxEmitter, no CustomAuditEvents — uses file-based JSONL audit log instead.

Two modes:
  - strict: raises AirGapViolation on any outbound attempt
  - audit:  logs violations but allows (for migration testing)

Destination guard: ``enforce_destination(url)`` refuses a request whose
host is outside ``allowed_subnets`` before a byte is sent. Loopback is always
allowed (it never leaves the host). Enabled with no ``allowed_subnets`` means
loopback only — a strict air gap. ``adk.compliance.egress_guard`` wires this
into every process-wide egress choke point (httpx, urllib, requests, sockets,
UDP sendto/sendmsg, and the resolver calls).

DNS: a hostname is only resolved when ``resolve_hostnames`` is true (default:
true when ``allowed_subnets`` is non-empty, false for loopback-only). With it
false, a non-local hostname is refused BEFORE any DNS query is sent; names in
the local hosts file and ``localhost`` still resolve. With it true, the DNS
query itself is an egress event to the configured resolver -- say so in any
claim.

Config resolution order (the primary layer):
  1. explicit ``config_path`` argument
  2. ``$AITHER_AIR_GAP_CONFIG``
  3. ``$AITHER_DATA_DIR/air_gap.yaml`` (default ``~/.aither/air_gap.yaml``)
  4. ``/etc/aither/air_gap.yaml`` (shipped by an air-gapped OS image profile)
``$AITHER_AIR_GAP=strict|audit|1|0`` overrides ``enabled`` and ``enforcement``
of the primary layer; empty means unset.

System floor: when ``/etc/aither/air_gap.yaml`` enables enforcement, no user
config, env var, constructor argument or ``disable()`` call can switch it off,
downgrade strict to audit, or widen its ``allowed_subnets``.

Fail closed: a config file that exists but cannot be read or parsed enforces
strict, loopback-only, and reports ``config_error``.

Usage:
    from adk.compliance.air_gap import get_air_gap_enforcer, AirGapViolation

    enforcer = get_air_gap_enforcer()
    enforcer.check_allowed("cloud_providers", detail="anthropic chat()")
    enforcer.enforce_destination("https://api.anthropic.com/v1/messages")
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import ipaddress
import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlsplit

logger = logging.getLogger("adk.compliance.air_gap")

SYSTEM_CONFIG_PATH = Path("/etc/aither/air_gap.yaml")

_ENV_TRUE = {"1", "true", "yes", "on"}
_ENV_FALSE = {"0", "false", "no", "off", "disabled"}

# A config file that exists but cannot be read/parsed enforces this.
_FAIL_CLOSED_CFG: Dict[str, Any] = {"enabled": True, "enforcement": "strict"}


def _hosts_file_path() -> Path:
    if os.name == "nt":
        root = os.environ.get("SystemRoot", "C:/Windows")
        return Path(root) / "System32" / "drivers" / "etc" / "hosts"
    return Path("/etc/hosts")


HOSTS_FILE_PATH: Optional[Path] = None  # None = the platform default
_hosts_cache: Dict[str, Any] = {"key": None, "names": frozenset()}


def local_hosts_names() -> frozenset:
    """Names in the local hosts file (resolved without a DNS query). Cached by mtime."""
    path = HOSTS_FILE_PATH or _hosts_file_path()
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return frozenset()
    if _hosts_cache["key"] == key:
        return _hosts_cache["names"]
    names = set()
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split("#", 1)[0].split()
            names.update(n.lower().rstrip(".") for n in parts[1:])
    except OSError:
        pass
    _hosts_cache["key"], _hosts_cache["names"] = key, frozenset(names)
    return _hosts_cache["names"]

IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]
IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


class AirGapViolation(Exception):
    """Raised when an air-gap-violating operation is attempted in strict mode.

    Deliberately NOT an httpx/requests error subclass, so a caller's
    ``except httpx.HTTPError`` never swallows it.
    """

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


def _parse_env_override(raw: Optional[str]) -> Tuple[Optional[bool], Optional[str]]:
    """``$AITHER_AIR_GAP`` -> (enabled, mode). (None, None) when unset/empty/unknown.

    Empty is UNSET: container templating renders ``AITHER_AIR_GAP=${X}`` as ''.
    """
    if raw is None or not raw.strip():
        return None, None
    val = raw.strip().lower()
    if val in ("strict", "audit"):
        return True, val
    if val in _ENV_TRUE:
        return True, None
    if val in _ENV_FALSE:
        return False, None
    logger.warning("[AirGapEnforcer] ignoring unknown AITHER_AIR_GAP=%r", raw)
    return None, None


def _minimal_yaml(raw: str) -> Dict[str, Any]:
    """Tiny fallback parser (PyYAML absent): flat ``key: value`` + ``- item`` lists."""
    out: Dict[str, Any] = {}
    current: Optional[str] = None
    for line in raw.splitlines():
        stripped = line.split("#", 1)[0].rstrip()
        if not stripped.strip():
            continue
        if stripped.lstrip().startswith("- ") and current is not None:
            item = stripped.lstrip()[2:].strip().strip("'\"")
            if not isinstance(out.get(current), list):
                out[current] = []
            out[current].append(item)
            continue
        if ":" in stripped and not stripped.startswith(" "):
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip().strip("'\"")
            current = key
            if value == "":
                out[key] = []
            elif value.startswith("[") and value.endswith("]"):
                out[key] = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
            elif value.lower() in ("true", "false"):
                out[key] = value.lower() == "true"
            else:
                try:
                    out[key] = int(value)
                except ValueError:
                    out[key] = value
    return out


class AirGapEnforcer:
    """Singleton air-gap enforcement engine (see module docstring for config order)."""

    # Default blocked subsystems for regulated deployments
    _DEFAULT_BLOCKED = [
        "cloud_providers", "cloud_llm", "external_api",
        "phonehome", "telemetry", "mesh_network", "network_egress",
    ]

    def __init__(
        self,
        config_path: Optional[Path] = None,
        *,
        enabled: Optional[bool] = None,
        mode: Optional[str] = None,
        system_floor: bool = True,
    ):
        self._explicit_path: Optional[Path] = Path(config_path) if config_path else None
        self._kw_enabled = enabled
        self._kw_mode = mode
        self._use_system_floor = system_floor
        self._config_path: Optional[Path] = None
        self._enabled: bool = False
        self._mode: EnforcementMode = EnforcementMode.STRICT
        self._activated_at: Optional[str] = None
        self._blocked_subsystems: List[str] = []
        self._allowed_subsystems: List[str] = []
        self._allowed_networks: List[IPNetwork] = []
        self._allowed_subnets_raw: List[str] = []
        self._resolve_hostnames_cfg: Optional[bool] = None
        self._attestation_interval: int = 300
        self._config_error: Optional[str] = None
        self._floor_path: Optional[Path] = None
        self._violations: List[ViolationRecord] = []
        self._violations_since_attestation: int = 0
        self._config_hash: str = ""
        self._resolve_cache: Dict[str, Tuple[float, List[str]]] = {}
        self._lock = threading.Lock()
        # No key => no signature. Never sign with a public default key (a
        # signature anyone can forge is worse than an honest empty one).
        key = os.environ.get("AITHER_AUDIT_SIGNING_KEY", "")
        self._signing_key: Optional[bytes] = key.encode() if key else None

        data_dir = Path(os.environ.get("AITHER_DATA_DIR", os.path.expanduser("~/.aither")))
        self._audit_log_path = data_dir / "compliance" / "audit.jsonl"

        self._resolve_state()
        if self._enabled:
            self._activate()

    def _resolve_state(self) -> None:
        """Primary layer -> env override -> constructor args -> system floor."""
        self._enabled = False
        self._mode = EnforcementMode.STRICT
        self._blocked_subsystems = []
        self._allowed_subsystems = []
        self._allowed_networks = []
        self._allowed_subnets_raw = []
        self._resolve_hostnames_cfg = None
        self._config_error = None
        self._floor_path = None
        self._config_hash = ""
        self._resolve_cache.clear()
        self._config_path = self._explicit_path or self._default_config_path()
        self._load_config()

        # Environment override: AITHER_AIR_GAP=strict|audit|1|0 (empty = unset)
        env_enabled, env_mode = _parse_env_override(os.environ.get("AITHER_AIR_GAP"))
        if env_mode is not None:
            self._mode = EnforcementMode(env_mode)
        if env_enabled is not None:
            self._enabled = env_enabled

        # Explicit overrides (--air-gap flag or config.yaml air_gap: true)
        if self._kw_enabled is not None:
            self._enabled = bool(self._kw_enabled)
        if self._kw_mode is not None:
            self._mode = EnforcementMode(self._kw_mode)

        self._apply_system_floor()

        if self._enabled:
            if not self._blocked_subsystems:
                self._blocked_subsystems = list(self._DEFAULT_BLOCKED)
            if "network_egress" not in self._blocked_subsystems:
                self._blocked_subsystems.append("network_egress")

    @staticmethod
    def _read_layer(path: Path) -> Tuple[Optional[Dict[str, Any]], str, Optional[str]]:
        """(cfg, hash, error). cfg None = file absent. Unreadable -> fail closed."""
        if not path.exists():
            return None, "", None
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as e:
            return dict(_FAIL_CLOSED_CFG), "", f"cannot read {path}: {e}"
        digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
        try:
            import yaml
            cfg = yaml.safe_load(raw) or {}
        except ImportError:
            cfg = _minimal_yaml(raw)
        except Exception as e:  # malformed YAML
            return dict(_FAIL_CLOSED_CFG), digest, f"malformed {path}: {e}"
        if not isinstance(cfg, dict):
            return dict(_FAIL_CLOSED_CFG), digest, f"{path} is not a mapping"
        return cfg, digest, None

    @staticmethod
    def _parse_mode(value: Any) -> EnforcementMode:
        try:
            return EnforcementMode(str(value if value is not None else "strict").lower())
        except ValueError:
            logger.warning("[AirGapEnforcer] unknown enforcement %r -- using strict", value)
            return EnforcementMode.STRICT

    @staticmethod
    def _parse_networks(raw: List[str]) -> List[IPNetwork]:
        nets: List[IPNetwork] = []
        for subnet in raw:
            try:
                nets.append(ipaddress.ip_network(subnet, strict=False))
            except ValueError:
                logger.warning("[AirGapEnforcer] Invalid subnet (ignored): %s", subnet)
        return nets

    def _apply_system_floor(self) -> None:
        """/etc/aither/air_gap.yaml, when it enforces, is a floor nothing lowers."""
        if not self._use_system_floor:
            return
        sys_path = SYSTEM_CONFIG_PATH
        cfg, digest, err = self._read_layer(sys_path)
        if cfg is None or not bool(cfg.get("enabled", False)):
            return
        self._floor_path = sys_path
        if err:
            logger.error("[AirGapEnforcer] %s -- failing CLOSED (strict, loopback only)", err)
            self._config_error = err
        if not self._enabled:
            logger.warning("[AirGapEnforcer] system policy %s enforces the air gap; "
                           "a user config/env/argument cannot disable it", sys_path)
        self._enabled = True
        if self._parse_mode(cfg.get("enforcement")) == EnforcementMode.STRICT:
            self._mode = EnforcementMode.STRICT
        floor_raw = [str(x) for x in (cfg.get("allowed_subnets") or [])]
        if self._allowed_subnets_raw and sorted(floor_raw) != sorted(self._allowed_subnets_raw):
            logger.warning("[AirGapEnforcer] allowed_subnets from %s ignored; the system "
                           "policy's %s apply", self._config_path, floor_raw)
        self._allowed_subnets_raw = floor_raw
        self._allowed_networks = self._parse_networks(floor_raw)
        floor_blocked = list(cfg.get("blocked_subsystems") or self._DEFAULT_BLOCKED)
        self._blocked_subsystems = floor_blocked + [
            b for b in self._blocked_subsystems if b not in floor_blocked]
        rh = cfg.get("resolve_hostnames")
        self._resolve_hostnames_cfg = None if rh is None else bool(rh)
        if self._config_path is not None and self._config_path != sys_path and digest:
            self._config_hash = hashlib.sha256(
                (digest + ":" + self._config_hash).encode()).hexdigest()[:16]
        elif digest:
            self._config_hash = digest

    @property
    def system_floor_enforced(self) -> bool:
        return self._floor_path is not None

    @property
    def config_error(self) -> Optional[str]:
        return self._config_error

    @property
    def resolves_hostnames(self) -> bool:
        """May a non-local hostname be sent to DNS? (loopback-only default: no)."""
        if self._resolve_hostnames_cfg is not None:
            return self._resolve_hostnames_cfg
        return bool(self._allowed_networks)

    @staticmethod
    def _default_config_path() -> Optional[Path]:
        explicit = os.environ.get("AITHER_AIR_GAP_CONFIG")
        if explicit and explicit.strip():
            return Path(explicit)
        user = Path(os.environ.get(
            "AITHER_DATA_DIR", os.path.expanduser("~/.aither")
        )) / "air_gap.yaml"
        if user.exists():
            return user
        if SYSTEM_CONFIG_PATH.exists():
            return SYSTEM_CONFIG_PATH
        return user

    @property
    def config_path(self) -> Optional[Path]:
        return self._config_path

    def _load_config(self):
        """Load the primary layer. A file that exists but is unreadable or
        malformed fails CLOSED (strict, loopback only) and sets config_error."""
        if self._config_path is None:
            return
        cfg, digest, err = self._read_layer(self._config_path)
        if cfg is None:
            logger.debug("[AirGapEnforcer] No config at %s", self._config_path)
            return
        if err:
            logger.error("[AirGapEnforcer] %s -- failing CLOSED (strict, loopback only)", err)
            self._config_error = err
        self._config_hash = digest
        self._enabled = bool(cfg.get("enabled", False))
        self._mode = self._parse_mode(cfg.get("enforcement", "strict"))
        self._blocked_subsystems = list(cfg.get("blocked_subsystems") or self._DEFAULT_BLOCKED)
        self._allowed_subsystems = list(cfg.get("allowed_subsystems") or [])
        self._allowed_subnets_raw = [str(x) for x in (cfg.get("allowed_subnets") or [])]
        self._allowed_networks = self._parse_networks(self._allowed_subnets_raw)
        rh = cfg.get("resolve_hostnames")
        self._resolve_hostnames_cfg = None if rh is None else bool(rh)
        try:
            self._attestation_interval = int(cfg.get("attestation_interval", 300) or 300)
        except (TypeError, ValueError):
            self._attestation_interval = 300

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
            "allowed_subnets": self._allowed_subnets_raw,
            "config_hash": self._config_hash,
        })

    # ---- Public API ----

    def is_enforced(self) -> bool:
        return self._enabled

    def get_mode(self) -> str:
        return self._mode.value if self._enabled else "disabled"

    @property
    def allowed_subnets(self) -> List[str]:
        return list(self._allowed_subnets_raw)

    def check_allowed(self, subsystem: str, detail: str = ""):
        """Check if a subsystem operation is allowed. Raises or logs."""
        if not self._enabled:
            return
        if subsystem in self._blocked_subsystems:
            self._record_violation(subsystem, detail)
            if self._mode == EnforcementMode.STRICT:
                raise AirGapViolation(subsystem, detail)

    def check_destination_allowed(self, address: str) -> bool:
        """Is this IP address inside an allowed subnet (loopback always is)?

        Answers the question only; strict-vs-audit is the caller's business
        (see ``enforce_destination``). Anything that does not parse as an IP is
        NOT allowed in either mode — "I could not read it" is never "allowed".
        """
        if not self._enabled:
            return True
        try:
            addr = ipaddress.ip_address(str(address).strip("[]").split("%")[0])
        except ValueError:
            return False
        return self._ip_allowed(addr)

    def _ip_allowed(self, addr: IPAddress) -> bool:
        if addr.is_loopback:
            return True  # never leaves the host
        mapped = getattr(addr, "ipv4_mapped", None)
        if mapped is not None:
            if mapped.is_loopback:
                return True
            if any(mapped in net for net in self._allowed_networks):
                return True
        return any(addr in net for net in self._allowed_networks)

    def _resolve(self, host: str) -> List[str]:
        """Resolve ``host`` to its addresses, cached for 60 s. [] = unresolvable."""
        now = time.monotonic()
        hit = self._resolve_cache.get(host)
        if hit and now - hit[0] < 60:
            return hit[1]
        try:
            infos = socket.getaddrinfo(host, None)
            addrs = sorted({str(info[4][0]) for info in infos})
        except (OSError, UnicodeError):
            addrs = []
        self._resolve_cache[host] = (now, addrs)
        return addrs

    def is_host_allowed(self, host: str) -> bool:
        """Is every address ``host`` resolves to inside an allowed subnet?

        Fail closed: an empty host, or one that does not resolve, is denied.
        """
        if not self._enabled:
            return True
        host = (host or "").strip().strip("[]")
        if not host:
            return False
        try:
            literal: Optional[IPAddress] = ipaddress.ip_address(host.split("%")[0])
        except ValueError:
            literal = None  # a hostname: resolve it below
        if literal is not None:
            return self._ip_allowed(literal)
        if host.lower() == "localhost" or host.lower().endswith(".localhost"):
            return True  # RFC 6761: always loopback
        if not self.dns_permitted(host):
            return False  # refused before any DNS query leaves the host
        addrs = self._resolve(host)
        if not addrs:
            return False
        for a in addrs:
            try:
                if not self._ip_allowed(ipaddress.ip_address(a.split("%")[0])):
                    return False
            except ValueError:
                return False
        return True

    def dns_permitted(self, host: str) -> bool:
        """May ``host`` be looked up? No lookup is ever made to answer this.

        IP literals and ``localhost`` need no DNS; names in the local hosts file
        resolve locally; any other name needs ``resolves_hostnames``.
        """
        if not self._enabled:
            return True
        h = (host or "").strip().strip("[]").rstrip(".").lower()
        if not h:
            return False
        try:
            ipaddress.ip_address(h.split("%")[0])
            return True
        except ValueError:
            pass
        if h == "localhost" or h.endswith(".localhost"):
            return True
        if h in local_hosts_names():
            return True
        return self.resolves_hostnames

    def enforce_destination(self, url: str, detail: str = "") -> None:
        """Guard one outbound request. Strict raises AirGapViolation; audit logs.

        ``url`` may be a full URL or a bare host / IP. Disabled is a no-op.
        """
        if not self._enabled:
            return
        text = str(url)
        try:
            host = urlsplit(text).hostname if "://" in text else text
        except ValueError:
            host = None
        if host and self.is_host_allowed(host):
            return
        what = detail or f"outbound request to {host or text!r}"
        if host and not self.dns_permitted(host):
            what += " (refused before DNS)"
        self._record_violation("network_egress", what)
        if self._mode == EnforcementMode.STRICT:
            raise AirGapViolation("network_egress", what)

    def get_attestation_state(self) -> AirGapAttestation:
        """Generate a signed attestation of current air-gap state.

        ``signature`` is '' when ``AITHER_AUDIT_SIGNING_KEY`` is unset.
        """
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
        att.signature = self._sign(payload.encode())

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

    def enable(self, mode: Optional[str] = None):
        """Enable air-gap enforcement at runtime and install the egress guard."""
        if mode is not None:
            self._mode = EnforcementMode(mode)
        if not self._enabled:
            self._enabled = True
            if mode is None:
                self._mode = EnforcementMode.STRICT
            self._blocked_subsystems = self._blocked_subsystems or list(self._DEFAULT_BLOCKED)
            if "network_egress" not in self._blocked_subsystems:
                self._blocked_subsystems.append("network_egress")
            self._activate()
        try:
            from adk.compliance._egress_guard import install_egress_guard
            install_egress_guard()
        except Exception as e:  # the guard must never break enabling
            logger.warning("[AirGapEnforcer] egress guard install failed: %s", e)

    def disable(self):
        """Disable air-gap enforcement at runtime (the guard's wrappers go inert).

        Refused while the system floor (/etc/aither/air_gap.yaml) enforces.
        """
        if not self._enabled:
            return
        if self.system_floor_enforced:
            logger.warning("[AirGapEnforcer] disable() refused: %s enforces the air gap",
                           self._floor_path)
            self._write_audit_event("air_gap_disable_refused",
                                    {"system_config": str(self._floor_path)})
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
        self._resolve_state()
        if self._enabled and not was_enabled:
            self._activate()
        elif was_enabled and not self._enabled:
            self._enabled = True
            self.disable()

    # ---- Internal ----

    def _sign(self, data: bytes, length: int = 64) -> str:
        if not self._signing_key:
            return ""
        return hmac_mod.new(self._signing_key, data, hashlib.sha256).hexdigest()[:length]

    def _record_violation(self, subsystem: str, detail: str):
        action = "blocked" if self._mode == EnforcementMode.STRICT else "logged"
        record = ViolationRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            subsystem=subsystem,
            detail=detail,
            action_taken=action,
        )
        with self._lock:
            self._violations.append(record)
            self._violations_since_attestation += 1

        level = logging.WARNING if self._mode == EnforcementMode.STRICT else logging.INFO
        logger.log(level, "[AirGapEnforcer] Violation: %s -- %s [%s]", subsystem, detail, action)

        self._write_audit_event("air_gap_violation", {
            "subsystem": subsystem,
            "detail": detail,
            "action_taken": action,
            "mode": self._mode.value,
            "pid": os.getpid(),
        })

    def _write_audit_event(self, action: str, metadata: Dict[str, Any]):
        """Append an audit event (HMAC-signed when a key is set) to the JSONL file."""
        try:
            self._audit_log_path.parent.mkdir(parents=True, exist_ok=True)
            event = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": action,
                "actor": "air_gap_enforcer",
                "metadata": metadata,
            }
            event_json = json.dumps(event, separators=(",", ":"))
            sig = self._sign(event_json.encode(), 32)
            signed = json.dumps({"event": event, "sig": sig}, separators=(",", ":"))
            with self._lock:
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


def current_enforcer() -> Optional[AirGapEnforcer]:
    """The singleton if one exists, without creating it (cheap, for hot paths)."""
    return _enforcer


def set_enforcer(enforcer: Optional[AirGapEnforcer]) -> Optional[AirGapEnforcer]:
    """Swap the singleton (tests / self-test). Returns the previous one."""
    global _enforcer
    prev, _enforcer = _enforcer, enforcer
    return prev


def reset_enforcer():
    """Reset the singleton (for testing)."""
    global _enforcer
    _enforcer = None


def is_air_gap_enforced() -> bool:
    """Quick check: is air-gap enforcement active?"""
    if _enforcer is None:
        return False
    return _enforcer.is_enforced()


def enforce_outbound_request(url: str) -> None:
    """Module-level guard, same name as the AitherOS lib function."""
    get_air_gap_enforcer().enforce_destination(url)
