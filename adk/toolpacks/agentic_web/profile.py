"""SiteProfile: the one description of a site every agentic_web generator reads.

A profile says who the site is (domain, name, origins), what agents and skills it
exposes, how it authenticates, which DNS-AID records it wants and which commerce
protocols it advertises. Every generator, deployer and gate is driven from it, so a
customer onboarding is "write one profile, run plan, review, apply".

Credentials are NEVER fields here: ``web_bot_auth.key_secret_name`` names a vault
secret; the caller resolves it.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import yaml


class ProfileError(ValueError):
    """The profile is missing a field a generator needs, or a field is malformed."""


def _is_absolute_url(url: str) -> bool:
    p = urlparse(str(url or ""))
    return p.scheme in ("https", "http") and bool(p.netloc)


def _origin(domain: str, sub: str = "") -> str:
    return f"https://{sub + '.' if sub else ''}{domain}"


@dataclass
class SiteProfile:
    domain: str
    name: str
    version: str = "1.0.0"
    description: str = ""
    origins: Dict[str, str] = field(default_factory=dict)
    agents: List[Dict[str, Any]] = field(default_factory=list)
    skills: List[Dict[str, Any]] = field(default_factory=list)
    endpoints: Dict[str, Any] = field(default_factory=dict)
    auth: Dict[str, Any] = field(default_factory=dict)
    mcp: Dict[str, Any] = field(default_factory=dict)
    webmcp_tools: List[Dict[str, Any]] = field(default_factory=list)
    web_bot_auth: Dict[str, Any] = field(default_factory=dict)
    dns_aid: List[Dict[str, Any]] = field(default_factory=list)
    commerce: Dict[str, Any] = field(default_factory=dict)
    #: Optional: true when the domain belongs to a customer, not to us. The gate
    #: also judges the domain itself, so a False here cannot hide a customer domain.
    customer: bool = False
    #: Directory skill ``source`` paths resolve against (the profile file's dir).
    base_dir: str = ""

    # ------------------------------------------------------------------ loading
    @classmethod
    def from_dict(cls, d: Dict[str, Any], base_dir: str = "") -> "SiteProfile":
        if not isinstance(d, dict):
            raise ProfileError("profile must be a mapping")
        d = copy.deepcopy(d)
        domain = str(d.get("domain") or "").strip().lower().rstrip(".")
        if not domain or "/" in domain or ":" in domain:
            raise ProfileError(f"domain must be a bare host name, got {d.get('domain')!r}")
        name = str(d.get("name") or "").strip()
        if not name:
            raise ProfileError("name is required")
        origins = dict(d.get("origins") or {})
        origins.setdefault("apex", _origin(domain))
        for k, v in origins.items():
            if v and not _is_absolute_url(v):
                raise ProfileError(f"origins.{k} must be an absolute URL, got {v!r}")
        commerce = dict(d.get("commerce") or {})
        ap2 = dict(commerce.get("ap2") or {})
        if ap2:
            ap2.setdefault("required", False)
            ap2.setdefault("roles", [])
            commerce["ap2"] = ap2
        x402 = dict(commerce.get("x402") or {})
        if x402:
            x402.setdefault("test_mode", True)
            commerce["x402"] = x402
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = sorted(set(d) - known)
        if unknown:
            raise ProfileError(f"unknown profile fields: {unknown}")
        return cls(
            domain=domain, name=name,
            version=str(d.get("version") or "1.0.0"),
            description=str(d.get("description") or ""),
            origins=origins,
            agents=list(d.get("agents") or []),
            skills=list(d.get("skills") or []),
            endpoints=dict(d.get("endpoints") or {}),
            auth=dict(d.get("auth") or {}),
            mcp=dict(d.get("mcp") or {}),
            webmcp_tools=list(d.get("webmcp_tools") or []),
            web_bot_auth=dict(d.get("web_bot_auth") or {}),
            dns_aid=list(d.get("dns_aid") or []),
            commerce=commerce,
            customer=bool(d.get("customer", False)),
            base_dir=str(d.get("base_dir") or base_dir or ""),
        )

    @classmethod
    def load(cls, path: str) -> "SiteProfile":
        p = Path(path)
        text = p.read_text(encoding="utf-8")
        data = json.loads(text) if p.suffix.lower() == ".json" else yaml.safe_load(text)
        return cls.from_dict(data or {}, base_dir=str(p.resolve().parent))

    def to_dict(self) -> Dict[str, Any]:
        return {f: copy.deepcopy(getattr(self, f))
                for f in self.__dataclass_fields__}  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ helpers
    @property
    def apex(self) -> str:
        return self.origins.get("apex") or _origin(self.domain)

    def resolve_source(self, source: str) -> Path:
        p = Path(source)
        if not p.is_absolute() and self.base_dir:
            p = Path(self.base_dir) / p
        return p


def coerce_profile(profile: Any, base_dir: str = "") -> SiteProfile:
    """Accept a SiteProfile, a dict, a JSON string or a path to YAML/JSON."""
    if isinstance(profile, SiteProfile):
        return profile
    if isinstance(profile, dict):
        return SiteProfile.from_dict(profile, base_dir=base_dir)
    if isinstance(profile, str):
        s = profile.strip()
        if s.startswith("{"):
            return SiteProfile.from_dict(json.loads(s), base_dir=base_dir)
        return SiteProfile.load(s)
    raise ProfileError(f"cannot read a profile from {type(profile).__name__}")


def require_absolute(url: Optional[str], what: str) -> str:
    if not _is_absolute_url(url or ""):
        raise ProfileError(f"{what} must be an absolute URL, got {url!r}")
    return str(url)
