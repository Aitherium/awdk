"""Identity loader — load agent personas from YAML files.

Enhanced with:
  - Skill manifests: structured capability declarations per identity
  - A2A protocol support: /.well-known/agent.json generation
  - Capability requirements for sandbox enforcement
  - GitHub device flow authentication for CLI login
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

__all__ = [
    "Identity",
    "SkillManifest",
    "load_identity",
    "list_identities",
    "load_soul_md",
    "export_soul_md",
    "github_device_flow",
    "AuthError",
]

logger = logging.getLogger("adk.identity")

# Bundled identities ship with the package
_IDENTITIES_DIR = Path(__file__).parent / "identities"


# ─────────────────────────────────────────────────────────────────────────────
# Skill Manifest
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SkillManifest:
    """Structured skill declaration for an agent identity.

    Structured skill declaration for sandbox and A2A integration.
    Declares what the agent CAN do, what it REQUIRES, and constraints.
    """
    name: str = ""
    description: str = ""
    capabilities_required: list[str] = field(default_factory=list)
    input_schema: Dict[str, Any] = field(default_factory=dict)
    output_schema: Dict[str, Any] = field(default_factory=dict)
    examples: list[Dict[str, str]] = field(default_factory=list)
    max_tokens: int = 0
    timeout_seconds: float = 60.0
    tags: list[str] = field(default_factory=list)


@dataclass
class Identity:
    """An agent identity loaded from YAML.

    Enhanced with:
      - skills_manifest: structured skill declarations
      - a2a_card: A2A protocol agent card data
      - capabilities: sandbox capabilities this identity needs
      - version: identity schema version for forward compat
    """
    name: str
    role: str = "assistant"
    description: str = ""
    skills: list[str] = field(default_factory=list)
    effort_cap: int = 10
    system_prompt: str = ""

    # Spirit/personality
    core_trait: str = ""
    drive: str = ""
    temperament: str = ""

    # Will/autonomy
    priority: str = ""
    autonomy: str = "moderate"

    # Skill manifests
    skills_manifest: list[SkillManifest] = field(default_factory=list)

    # A2A protocol support
    a2a_card: Dict[str, Any] = field(default_factory=dict)

    # Sandbox capabilities this identity requires
    capabilities: list[str] = field(default_factory=list)

    # Schema version
    version: str = "1.0"

    # Raw YAML data for extensibility
    raw: dict = field(default_factory=dict)

    def build_system_prompt(self) -> str:
        """Build a system prompt from this identity."""
        if self.system_prompt:
            return self.system_prompt

        parts = [f"You are {self.name}, an AI agent."]
        if self.description:
            parts.append(f"Role: {self.description}")
        if self.core_trait:
            parts.append(f"Core trait: {self.core_trait}")
        if self.drive:
            parts.append(f"Drive: {self.drive}")
        if self.temperament:
            parts.append(f"Temperament: {self.temperament}")
        if self.skills:
            parts.append(f"Skills: {', '.join(self.skills)}")
        return "\n".join(parts)

    def to_a2a_card(self, base_url: str = "http://localhost:8000") -> Dict[str, Any]:
        """Generate an A2A protocol agent card (/.well-known/agent.json).

        Follows Google A2A protocol spec with AitherOS extensions.
        """
        if self.a2a_card:
            return self.a2a_card

        skills_section = []
        for sm in self.skills_manifest:
            skill_entry = {
                "id": sm.name.lower().replace(" ", "_"),
                "name": sm.name or self.name,
                "description": sm.description or self.description,
                "tags": sm.tags or self.skills,
            }
            if sm.input_schema:
                skill_entry["inputModes"] = ["text/plain", "application/json"]
            if sm.output_schema:
                skill_entry["outputModes"] = ["text/plain", "application/json"]
            if sm.examples:
                skill_entry["examples"] = sm.examples
            skills_section.append(skill_entry)

        # Fallback: create skills from basic identity info
        if not skills_section and self.skills:
            for skill_name in self.skills[:5]:
                skills_section.append({
                    "id": skill_name.lower().replace(" ", "_"),
                    "name": skill_name,
                    "description": f"{self.name} skill: {skill_name}",
                    "tags": [skill_name],
                })

        return {
            "name": self.name,
            "description": self.description or f"AI agent: {self.name}",
            "url": base_url,
            "version": self.version,
            "provider": {
                "organization": "Aitherium",
                "url": "https://aitherium.com",
            },
            "capabilities": {
                "streaming": False,
                "pushNotifications": False,
                "stateTransitionHistory": False,
            },
            "authentication": {
                "schemes": ["bearer"],
            },
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": skills_section,
        }

    def to_skill_manifest_yaml(self) -> str:
        """Export skills as YAML manifest."""
        manifests = []
        for sm in self.skills_manifest:
            entry = {
                "name": sm.name,
                "description": sm.description,
                "capabilities_required": sm.capabilities_required,
                "tags": sm.tags,
            }
            if sm.input_schema:
                entry["input_schema"] = sm.input_schema
            if sm.output_schema:
                entry["output_schema"] = sm.output_schema
            if sm.max_tokens:
                entry["max_tokens"] = sm.max_tokens
            if sm.timeout_seconds != 60.0:
                entry["timeout_seconds"] = sm.timeout_seconds
            manifests.append(entry)
        return yaml.dump({"skills": manifests}, default_flow_style=False)


def load_soul_md(path: Path) -> dict:
    """Read an OpenClaw-format SOUL.md and convert to ADK identity config.

    Sections recognised (case-insensitive):
        # Personality  -> system_prompt
        # Instructions -> rules (joined into system_prompt)
        # Knowledge    -> injected as context block
        # Name         -> name
        # Role / Description -> description

    Returns a dict suitable for ``Identity(**result)``.
    """
    text = path.read_text(encoding="utf-8")
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = stripped[2:].strip().lower()
            buf = []
        else:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()

    # Build identity dict
    result: dict[str, Any] = {}
    result["name"] = sections.get("name", path.stem)

    desc = sections.get("role", sections.get("description", ""))
    if desc:
        result["description"] = desc

    # System prompt = personality + instructions
    prompt_parts: list[str] = []
    if "personality" in sections:
        prompt_parts.append(sections["personality"])
    if "instructions" in sections:
        prompt_parts.append(sections["instructions"])
    if prompt_parts:
        result["system_prompt"] = "\n\n".join(prompt_parts)

    if "knowledge" in sections:
        result["knowledge"] = sections["knowledge"]

    # Extract skills from instructions if present
    skills: list[str] = []
    if "skills" in sections:
        for line in sections["skills"].splitlines():
            line = line.strip().lstrip("-*").strip()
            if line:
                skills.append(line)
        result["skills"] = skills

    return result


def export_soul_md(identity: Identity) -> str:
    """Export an ADK Identity as OpenClaw-compatible SOUL.md format."""
    parts: list[str] = []
    parts.append(f"# Name\n{identity.name}")

    if identity.description:
        parts.append(f"# Description\n{identity.description}")

    if identity.role and identity.role != "assistant":
        parts.append(f"# Role\n{identity.role}")

    # Personality from spirit fields
    personality_lines: list[str] = []
    if identity.core_trait:
        personality_lines.append(f"Core trait: {identity.core_trait}")
    if identity.drive:
        personality_lines.append(f"Drive: {identity.drive}")
    if identity.temperament:
        personality_lines.append(f"Temperament: {identity.temperament}")
    if personality_lines:
        parts.append("# Personality\n" + "\n".join(personality_lines))

    # Instructions from system prompt
    if identity.system_prompt:
        parts.append(f"# Instructions\n{identity.system_prompt}")

    # Skills
    if identity.skills:
        skill_lines = [f"- {s}" for s in identity.skills]
        parts.append("# Skills\n" + "\n".join(skill_lines))

    return "\n\n".join(parts) + "\n"


def load_identity(name: str, search_paths: list[Path] | None = None) -> Identity:
    """Load an identity by name from YAML files.

    Searches in order:
    1. Provided search_paths
    2. User-installed identities (~/.aitheros/identities/)
    3. Current directory ./identities/
    4. Bundled package identities
    """
    paths_to_try = []
    if search_paths:
        for p in search_paths:
            paths_to_try.append(p / f"{name}.yaml")
            paths_to_try.append(p / f"{name}.yml")
    # User-installed identities
    user_dir = Path.home() / ".aitheros" / "identities"
    paths_to_try.append(user_dir / f"{name}.yaml")
    paths_to_try.append(user_dir / f"{name}.yml")
    paths_to_try.append(Path("identities") / f"{name}.yaml")
    paths_to_try.append(_IDENTITIES_DIR / f"{name}.yaml")

    for path in paths_to_try:
        if path.exists():
            return _parse_identity(path)

    logger.warning(f"Identity '{name}' not found, using defaults")
    return Identity(name=name)


def list_identities(search_paths: list[Path] | None = None) -> list[str]:
    """List all available identity names."""
    names = set()
    dirs = [_IDENTITIES_DIR, Path("identities"), Path.home() / ".aitheros" / "identities"]
    if search_paths:
        dirs.extend(search_paths)

    for d in dirs:
        if d.exists():
            for f in d.glob("*.yaml"):
                names.add(f.stem)
            for f in d.glob("*.yml"):
                names.add(f.stem)
    return sorted(names)


def _parse_identity(path: Path) -> Identity:
    """Parse a YAML identity file into an Identity object.

    Supports new fields:
      - skills_manifest: list of structured skill declarations
      - a2a_card: pre-built A2A agent card
      - capabilities: sandbox capabilities
      - version: identity schema version
    """
    with open(path) as f:
        data = yaml.safe_load(f) or {}

    spirit = data.get("spirit_snapshot", {})
    will = data.get("will_config", {})

    # Parse skill manifests
    skills_manifest = []
    for sm_data in data.get("skills_manifest", []):
        skills_manifest.append(SkillManifest(
            name=sm_data.get("name", ""),
            description=sm_data.get("description", ""),
            capabilities_required=sm_data.get("capabilities_required", []),
            input_schema=sm_data.get("input_schema", {}),
            output_schema=sm_data.get("output_schema", {}),
            examples=sm_data.get("examples", []),
            max_tokens=sm_data.get("max_tokens", 0),
            timeout_seconds=sm_data.get("timeout_seconds", 60.0),
            tags=sm_data.get("tags", []),
        ))

    return Identity(
        name=data.get("name", path.stem),
        role=data.get("role", "assistant"),
        description=data.get("description", ""),
        skills=data.get("skills", []),
        effort_cap=data.get("effort_cap", 10),
        system_prompt=data.get("system_prompt", ""),
        core_trait=spirit.get("core_trait", ""),
        drive=spirit.get("drive", ""),
        temperament=spirit.get("temperament", ""),
        priority=will.get("priority", ""),
        autonomy=will.get("autonomy", "moderate"),
        skills_manifest=skills_manifest,
        a2a_card=data.get("a2a_card", {}),
        capabilities=data.get("capabilities", []),
        version=data.get("version", "1.0"),
        raw=data,
    )


# ─────────────────────────────────────────────────────────────────────────────
# GitHub Device Flow Authentication
# ─────────────────────────────────────────────────────────────────────────────


class AuthError(RuntimeError):
    """Raised on credential lookup / device flow authentication failures."""


async def github_device_flow(
    no_browser: bool = False,
    base: str | None = None,
    *,
    poll_timeout: float = 300.0,
    poll_interval: float = 1.0,
) -> Dict[str, Any]:
    """Authenticate via GitHub device flow against AitherIdentity.

    Initiates RFC 8628 OAuth device flow by POST-ing /auth/github/device/start,
    prints the verification_uri + user_code, and polls
    /auth/github/device/poll(handle) until success/timeout/denial. Returns
    {user_id, workspace_id, bearer_token} on success (fail-closed).

    Args:
        no_browser: Ignored (reserved for future browser-launch support).
        base: AitherIdentity base URL (default: env AITHER_IDENTITY_URL or
              https://aitheros-security-core:8115). Must support TLS + internal
              CA verification.
        poll_timeout: Seconds to wait for user to authorize (default: 300s).
        poll_interval: Seconds between poll attempts (default: 1s).

    Returns:
        {
            "user_id": str,
            "workspace_id": str,
            "bearer_token": str,
            "username": str,
        }

    Raises:
        AuthError: timeout/denied/error from GitHub or service. Never raises
                   HTTPException or returns partial dict.
    """
    try:
        import httpx
    except ImportError as e:
        raise AuthError(f"httpx not installed: {e}") from e

    # Resolve base URL
    if not base:
        base = os.getenv(
            "AITHER_IDENTITY_URL",
            "https://aitheros-security-core:8115",
        ).strip()
    base = base.rstrip("/")

    # Get TLS verify setting (prefer internal CA bundle, fall back to system CAs)
    try:
        from adk._tls import tls_verify
        verify = tls_verify()
    except (ImportError, Exception):
        verify = True  # Fallback: system CAs only

    # Step 1: POST /auth/github/device/start
    try:
        async with httpx.AsyncClient(verify=verify, timeout=15.0) as client:
            start_resp = await client.post(f"{base}/auth/github/device/start")
    except asyncio.TimeoutError as e:
        raise AuthError(f"Device start timeout: {e}") from e
    except httpx.HTTPError as e:
        raise AuthError(f"Device start failed: {e}") from e

    if start_resp.status_code != 200:
        detail = start_resp.text[:200] if start_resp.text else ""
        raise AuthError(
            f"Device start HTTP {start_resp.status_code}: {detail}"
        )

    try:
        data = start_resp.json()
    except ValueError as e:
        raise AuthError(f"Device start response invalid JSON: {e}") from e

    handle = data.get("handle", "").strip()
    user_code = data.get("user_code", "").strip()
    verification_uri = data.get("verification_uri", "").strip()
    expires_in = int(data.get("expires_in", 900))
    interval = int(data.get("interval", 5))

    if not handle or not user_code or not verification_uri:
        raise AuthError(
            "Device start response incomplete: missing handle/user_code/"
            "verification_uri"
        )

    # Step 2: Display verification URI + user code (never print token)
    print(
        f"\nGitHub Device Authorization\n"
        f"Visit: {verification_uri}\n"
        f"Enter code: {user_code}\n",
        file=sys.stderr,
    )

    # Step 3: Poll /auth/github/device/poll until complete/timeout
    start_time = asyncio.get_event_loop().time()
    poll_wait = min(interval, 2)  # User-friendly polling interval

    while True:
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed > poll_timeout:
            raise AuthError(
                f"Device authorization timeout after {poll_timeout}s "
                "(check GitHub app at {verification_uri})"
            )

        await asyncio.sleep(poll_wait)

        try:
            async with httpx.AsyncClient(
                verify=verify, timeout=15.0
            ) as client:
                poll_resp = await client.post(
                    f"{base}/auth/github/device/poll",
                    json={"handle": handle},
                )
        except asyncio.TimeoutError as e:
            # Timeout on this poll attempt — retry
            logger.debug("Poll attempt timeout: %s", e)
            continue
        except httpx.HTTPError as e:
            raise AuthError(f"Poll request failed: {e}") from e

        if poll_resp.status_code != 200:
            detail = poll_resp.text[:200] if poll_resp.text else ""
            raise AuthError(f"Poll HTTP {poll_resp.status_code}: {detail}")

        try:
            poll_data = poll_resp.json()
        except ValueError as e:
            raise AuthError(f"Poll response invalid JSON: {e}") from e

        status = poll_data.get("status", "").strip().lower()

        if status == "complete":
            # Success: extract fields (fail-closed if any required field missing)
            bearer_token = poll_data.get("access_token", "").strip()
            user_id = poll_data.get("user_id", "").strip()
            workspace_id = poll_data.get("tenant_id", "").strip()

            if not bearer_token or not user_id:
                raise AuthError(
                    "Poll complete but missing access_token or user_id"
                )

            # Never print/log the bearer token
            logger.debug(
                "Device auth complete: user_id=%s workspace_id=%s",
                user_id, workspace_id,
            )
            return {
                "user_id": user_id,
                "workspace_id": workspace_id,
                "bearer_token": bearer_token,
                "username": poll_data.get("username", ""),
            }

        elif status == "pending":
            # Keep polling
            new_interval = int(poll_data.get("interval", interval))
            if new_interval != interval:
                interval = new_interval
                poll_wait = min(interval, 2)
            continue

        elif status == "error":
            # Denied or error
            error = poll_data.get("error", "unknown_error").strip()
            raise AuthError(f"GitHub authorization denied: {error}")

        else:
            # Unknown status
            raise AuthError(f"Unexpected poll status: {status}")
