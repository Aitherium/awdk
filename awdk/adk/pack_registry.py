"""Pack registry for awdk — local-first storage and discovery of agent/tool packs.

A PackRegistry stores published packs under a root directory, organized as:
  root/<pack_id>/<version>/
    manifest.json       - The AgentPackManifest or ToolPackManifest
    digest.txt          - sha256 of manifest.json (computed once, used for validation)
    .yanked             - Optional: marks the version as withdrawn (skipped by browse/get)

Publishing is local-first: manifests are validated (fail-closed), written to disk,
then best-effort publisher() callbacks notify a remote registry. Local success
does not depend on remote availability.

Validation enforces: id/name/version present; version is semver-ish; framework/protocol
are in the known literals; entrypoint is non-empty; entitlements is a list; min_tier
is a known tier if specified.

Version resolution uses semantic versioning: 0.10.0 > 0.2.0 (not string order).
Yanked versions are hidden by default from browse() and get(version=None).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from pydantic import ValidationError

from adk.agent_pack import AgentPackManifest

__all__ = [
    "PackRegistry",
    "PublishReceipt",
    "PackSummary",
    "validate_pack",
]

logger = logging.getLogger(__name__)

# Known license tiers (same as in tool_pack_loader.py)
_KNOWN_TIERS = {"community", "free", "builder", "professional", "enterprise", "internal"}

# Known agent frameworks
_AGENT_FRAMEWORKS = {"nooa", "deer-flow", "hermes", "openclaw", "native", "custom"}

# Known wire protocols
_AGENT_PROTOCOLS = {"acp", "a2a", "mcp", "openai", "langgraph_rest", "http"}


def _parse_semver(version_str: str) -> tuple[int, int, int]:
    """Parse a semver string (e.g., '1.2.3') into (major, minor, patch).

    Tolerates leading 'v' and trailing build/prerelease metadata.
    Falls back to (0, 0, 0) if parsing fails (for non-strict versions).

    Returns:
        (major, minor, patch) tuple for sorting.

    Raises:
        ValueError: If the version string is completely unparseable.
    """
    s = str(version_str).strip()
    # Remove leading 'v'
    if s.startswith("v") or s.startswith("V"):
        s = s[1:]
    # Remove prerelease/build (+... or -...)
    s = re.split(r"[+-]", s)[0]
    # Parse major.minor.patch
    parts = s.split(".")
    try:
        major = int(parts[0]) if len(parts) > 0 else 0
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0
        return (major, minor, patch)
    except (ValueError, IndexError):
        raise ValueError(f"Cannot parse version '{version_str}'")


def validate_pack(manifest: AgentPackManifest) -> list[str]:
    """Validate an AgentPackManifest and return a list of human-readable errors.

    Fail-closed: any error prevents publish. Checks:
    - id: must be non-empty string
    - name: must be non-empty string
    - version: must parse as semver-ish (e.g., '1.0.0')
    - framework: must be in known set (nooa, deer-flow, hermes, openclaw, native, custom)
    - protocol: must be in known set (acp, a2a, mcp, openai, langgraph_rest, http)
    - entrypoint: must be non-empty string
    - entitlements: must be a list
    - min_tier: if non-empty, must be in known set

    Args:
        manifest: The AgentPackManifest to validate.

    Returns:
        A list of error strings (empty = valid).
    """
    errors: list[str] = []

    # id
    if not manifest.id or not isinstance(manifest.id, str):
        errors.append("id must be a non-empty string")
    elif not re.match(r"^[a-z0-9][a-z0-9._-]*$", manifest.id):
        errors.append(
            f"id '{manifest.id}' must be lowercase alphanumeric "
            f"(with . _ - allowed, not at start)"
        )

    # name
    if not manifest.name or not isinstance(manifest.name, str):
        errors.append("name must be a non-empty string")

    # version
    if not manifest.version or not isinstance(manifest.version, str):
        errors.append("version must be a non-empty string")
    else:
        try:
            _parse_semver(manifest.version)
        except ValueError as exc:
            errors.append(f"version '{manifest.version}' is not semver-ish: {exc}")

    # framework
    if manifest.framework not in _AGENT_FRAMEWORKS:
        errors.append(
            f"framework '{manifest.framework}' not in "
            f"{sorted(_AGENT_FRAMEWORKS)}"
        )

    # protocol
    if manifest.protocol not in _AGENT_PROTOCOLS:
        errors.append(
            f"protocol '{manifest.protocol}' not in "
            f"{sorted(_AGENT_PROTOCOLS)}"
        )

    # entrypoint
    if not manifest.entrypoint or not isinstance(manifest.entrypoint, str):
        errors.append("entrypoint must be a non-empty string")

    # entitlements must be a list
    if not isinstance(manifest.entitlements, list):
        errors.append(f"entitlements must be a list, got {type(manifest.entitlements)}")

    # min_tier (if specified, must be known)
    if manifest.min_tier and manifest.min_tier not in _KNOWN_TIERS:
        errors.append(
            f"min_tier '{manifest.min_tier}' not in "
            f"{sorted(_KNOWN_TIERS)}"
        )

    return errors


@dataclass
class PublishReceipt:
    """Outcome of a pack publish operation."""

    #: The pack id
    id: str

    #: The published version
    version: str

    #: SHA256 of the manifest.json content (hex string)
    digest: str

    #: When the pack was published (ISO 8601 UTC)
    published_at: str

    #: True if the remote publisher succeeded (or was not called)
    remote_ok: bool = True

    #: Optional remote error message
    remote_error: str = ""


@dataclass
class PackSummary:
    """Lightweight summary of a published pack (used by browse)."""

    #: The pack id
    id: str

    #: Human-readable name
    name: str

    #: Published version
    version: str

    #: Framework (nooa, deer-flow, etc.)
    framework: str

    #: Protocol (acp, a2a, mcp, etc.)
    protocol: str

    #: Optional description
    description: str = ""

    #: When published (ISO 8601 UTC)
    published_at: str = ""

    #: Skills exposed
    skills: list[str] = field(default_factory=list)

    #: Required entitlements
    entitlements: list[str] = field(default_factory=list)


class PackRegistry:
    """A local-first registry for agent and tool packs.

    Stores packs under root/<pack_id>/<version>/ with manifest.json,
    digest.txt, and optional .yanked markers.

    Operations:
    - publish(manifest, publisher=None, force=False): Write and optionally notify remote.
    - browse(query=None, framework=None): List published packs, excluding yanked.
    - versions(pack_id): List all versions of a pack, excluding yanked.
    - get(pack_id, version=None): Fetch a specific or latest pack manifest.
    - yank(pack_id, version): Mark a version as withdrawn.
    """

    def __init__(self, root: Path | str):
        """Initialize the registry at the given root directory.

        Args:
            root: Root directory for pack storage. Will be created if missing.
        """
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        logger.info("PackRegistry initialized at %s", self.root)

    # ── Private helpers ──────────────────────────────────────────────────

    def _pack_dir(self, pack_id: str, version: str) -> Path:
        """Return the directory for a pack's version."""
        return self.root / pack_id / version

    def _manifest_path(self, pack_id: str, version: str) -> Path:
        """Return the manifest.json path for a pack version."""
        return self._pack_dir(pack_id, version) / "manifest.json"

    def _digest_path(self, pack_id: str, version: str) -> Path:
        """Return the digest.txt path for a pack version."""
        return self._pack_dir(pack_id, version) / "digest.txt"

    def _yanked_marker_path(self, pack_id: str, version: str) -> Path:
        """Return the .yanked marker path for a pack version."""
        return self._pack_dir(pack_id, version) / ".yanked"

    def _is_yanked(self, pack_id: str, version: str) -> bool:
        """Check if a pack version is marked as yanked."""
        return self._yanked_marker_path(pack_id, version).is_file()

    # ── Publishing ───────────────────────────────────────────────────────

    def publish(
        self,
        manifest_or_path: AgentPackManifest | Path | str,
        publisher: Callable[[AgentPackManifest], None] | None = None,
        force: bool = False,
    ) -> PublishReceipt:
        """Publish a pack locally, optionally notifying a remote registry.

        Publishing is fail-closed on validation errors: if the manifest is invalid,
        nothing is written. If the (id, version) already exists, publish fails
        unless force=True.

        The remote publisher (if provided) is called best-effort; a remote failure
        does NOT undo the local publish. The receipt's remote_ok flag indicates
        whether the remote succeeded.

        Args:
            manifest_or_path: An AgentPackManifest or path to a .yaml/.json file.
            publisher: Optional callable(manifest) for remote registry notification.
                       Failures are logged but do not fail the local publish.
            force: If True, overwrite an existing (id, version).

        Returns:
            A PublishReceipt describing the outcome.

        Raises:
            ValueError: If the manifest is invalid or (id, version) exists
                        and force=False.
            FileNotFoundError: If manifest_or_path is a path that does not exist.
        """
        # Load manifest if needed
        if isinstance(manifest_or_path, (Path, str)):
            path = Path(manifest_or_path)
            if not path.is_file():
                raise FileNotFoundError(f"Manifest file not found: {path}")
            # Try to load as YAML first (agent.yaml), then JSON
            try:
                import yaml

                data = yaml.safe_load(path.read_text("utf-8")) or {}
                if not isinstance(data, dict):
                    raise ValueError("Manifest must be a YAML/JSON dict")
            except Exception as exc:
                raise ValueError(f"Failed to parse {path}: {exc}") from exc

            try:
                from adk.agent_pack import RuntimeConfig

                runtime_data = data.get("runtime", {})
                if isinstance(runtime_data, dict):
                    runtime = RuntimeConfig(**runtime_data)
                else:
                    raise ValueError("runtime must be a dict")

                manifest = AgentPackManifest(
                    id=data.get("id") or path.parent.name,
                    name=data.get("name", ""),
                    version=data.get("version", "0.0.0"),
                    framework=data.get("framework"),
                    runtime=runtime,
                    entrypoint=data.get("entrypoint", ""),
                    protocol=data.get("protocol"),
                    model_endpoint=data.get("model_endpoint", "http://localhost:8150"),
                    mcp=data.get("mcp"),
                    skills=data.get("skills") or [],
                    entitlements=data.get("entitlements") or [],
                    min_tier=data.get("min_tier", ""),
                    identity=data.get("identity") or {},
                    secrets=data.get("secrets") or {},
                )
            except (ValueError, ValidationError) as exc:
                raise ValueError(f"Failed to parse manifest at {path}: {exc}") from exc
        else:
            manifest = manifest_or_path

        # Validate (fail-closed)
        validation_errors = validate_pack(manifest)
        if validation_errors:
            msg = "Pack validation failed:\n" + "\n".join(
                f"  - {err}" for err in validation_errors
            )
            raise ValueError(msg)

        # Check for existing version
        pack_dir = self._pack_dir(manifest.id, manifest.version)
        if pack_dir.exists() and not force:
            raise ValueError(
                f"Pack {manifest.id}@{manifest.version} already published. "
                f"Set force=True to overwrite."
            )

        # Compute digest
        manifest_json = manifest.model_dump_json(indent=2, exclude_none=False)
        digest = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()

        # Write to disk (fail-closed if mkdir/write fails)
        try:
            pack_dir.mkdir(parents=True, exist_ok=True)

            manifest_path = self._manifest_path(manifest.id, manifest.version)
            manifest_path.write_text(manifest_json, encoding="utf-8")

            digest_path = self._digest_path(manifest.id, manifest.version)
            digest_path.write_text(digest + "\n", encoding="utf-8")

            # Remove .yanked if it exists (re-publishing an unpublished pack)
            yanked_marker = self._yanked_marker_path(manifest.id, manifest.version)
            if yanked_marker.exists():
                yanked_marker.unlink()

            logger.info(
                "Published pack %s@%s (digest=%s)",
                manifest.id,
                manifest.version,
                digest[:16],
            )
        except Exception as exc:
            logger.error(
                "Failed to write pack %s@%s to disk: %s",
                manifest.id,
                manifest.version,
                exc,
            )
            raise ValueError(
                f"Failed to write pack {manifest.id}@{manifest.version}: {exc}"
            ) from exc

        # Best-effort remote publish
        published_at = datetime.now(timezone.utc).isoformat()
        remote_ok = True
        remote_error = ""
        if publisher is not None:
            try:
                publisher(manifest)
                logger.info(
                    "Remote publish succeeded for %s@%s",
                    manifest.id,
                    manifest.version,
                )
            except Exception as exc:
                remote_ok = False
                remote_error = str(exc)
                logger.warning(
                    "Remote publish failed for %s@%s: %s",
                    manifest.id,
                    manifest.version,
                    exc,
                )

        return PublishReceipt(
            id=manifest.id,
            version=manifest.version,
            digest=digest,
            published_at=published_at,
            remote_ok=remote_ok,
            remote_error=remote_error,
        )

    # ── Browsing ─────────────────────────────────────────────────────────

    def browse(
        self, query: str | None = None, framework: str | None = None
    ) -> list[PackSummary]:
        """List published packs, optionally filtered by query and framework.

        Excludes yanked versions. For each pack, returns only the latest
        non-yanked version.

        Args:
            query: Optional substring (case-insensitive) to match against
                   pack id or name.
            framework: Optional framework name to filter by (e.g., 'nooa').

        Returns:
            A list of PackSummary objects, sorted by published_at descending.
        """
        summaries: dict[str, PackSummary] = {}  # pack_id -> latest PackSummary

        if not self.root.is_dir():
            return []

        # Scan all pack_id directories
        for pack_id_dir in sorted(self.root.iterdir()):
            if not pack_id_dir.is_dir():
                continue

            pack_id = pack_id_dir.name

            # Scan all version directories
            for version_dir in sorted(pack_id_dir.iterdir()):
                if not version_dir.is_dir():
                    continue

                version = version_dir.name

                # Skip yanked versions
                if self._is_yanked(pack_id, version):
                    continue

                # Try to load the manifest
                manifest_path = self._manifest_path(pack_id, version)
                if not manifest_path.is_file():
                    continue

                try:
                    manifest_json = manifest_path.read_text("utf-8")
                    data = json.loads(manifest_json)
                    manifest = AgentPackManifest(**data)
                except Exception as exc:
                    logger.warning(
                        "Failed to load manifest for %s@%s: %s",
                        pack_id,
                        version,
                        exc,
                    )
                    continue

                # Apply filters
                if query:
                    q_lower = query.lower()
                    if (
                        q_lower not in pack_id.lower()
                        and q_lower not in manifest.name.lower()
                    ):
                        continue

                if framework and manifest.framework != framework:
                    continue

                # Get published_at from the manifest file mtime
                try:
                    mtime = version_dir.stat().st_mtime
                    published_at = datetime.fromtimestamp(
                        mtime, tz=timezone.utc
                    ).isoformat()
                except Exception:
                    published_at = datetime.now(timezone.utc).isoformat()

                summary = PackSummary(
                    id=manifest.id,
                    name=manifest.name,
                    version=manifest.version,
                    framework=manifest.framework,
                    protocol=manifest.protocol,
                    description=manifest.to_toolpack_dict().get("description", ""),
                    published_at=published_at,
                    skills=manifest.skills,
                    entitlements=manifest.entitlements,
                )

                # Keep only the latest version per pack
                if pack_id not in summaries:
                    summaries[pack_id] = summary
                else:
                    try:
                        new_semver = _parse_semver(summary.version)
                        old_semver = _parse_semver(summaries[pack_id].version)
                        if new_semver > old_semver:
                            summaries[pack_id] = summary
                    except ValueError:
                        # If version parsing fails, keep the first one
                        pass

        # Sort by published_at descending
        return sorted(
            summaries.values(),
            key=lambda s: s.published_at,
            reverse=True,
        )

    def versions(self, pack_id: str) -> list[str]:
        """List all non-yanked versions of a pack, sorted by semver.

        Args:
            pack_id: The pack identifier.

        Returns:
            A list of version strings, sorted by semantic version (descending).
        """
        pack_dir = self.root / pack_id
        if not pack_dir.is_dir():
            return []

        versions: list[str] = []
        for version_dir in pack_dir.iterdir():
            if not version_dir.is_dir():
                continue

            version = version_dir.name

            # Skip yanked
            if self._is_yanked(pack_id, version):
                continue

            # Verify manifest exists
            if self._manifest_path(pack_id, version).is_file():
                versions.append(version)

        # Sort by semver descending
        try:
            versions.sort(key=lambda v: _parse_semver(v), reverse=True)
        except ValueError:
            # Fall back to string sort if any version is unparseable
            versions.sort(reverse=True)

        return versions

    def get(self, pack_id: str, version: str | None = None) -> AgentPackManifest:
        """Fetch a pack manifest by id and optional version.

        If version is None, returns the latest non-yanked version.
        Yanked versions are NOT returned unless explicitly requested.

        Args:
            pack_id: The pack identifier.
            version: Optional specific version. If None, fetches latest.

        Returns:
            The loaded AgentPackManifest.

        Raises:
            KeyError: If the pack or version does not exist.
            ValueError: If the manifest cannot be parsed.
        """
        if version is None:
            # Get latest non-yanked version
            versions = self.versions(pack_id)
            if not versions:
                raise KeyError(f"Pack {pack_id} not found or all versions are yanked")
            version = versions[0]

        manifest_path = self._manifest_path(pack_id, version)
        if not manifest_path.is_file():
            raise KeyError(f"Pack {pack_id}@{version} not found")

        if self._is_yanked(pack_id, version):
            raise KeyError(f"Pack {pack_id}@{version} is yanked")

        try:
            manifest_json = manifest_path.read_text("utf-8")
            data = json.loads(manifest_json)
            return AgentPackManifest(**data)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Failed to parse manifest for {pack_id}@{version}: {exc}"
            ) from exc
        except Exception as exc:
            raise ValueError(
                f"Failed to load manifest for {pack_id}@{version}: {exc}"
            ) from exc

    def yank(self, pack_id: str, version: str) -> None:
        """Mark a pack version as yanked (withdrawn).

        Yanked versions are hidden from browse(), versions(), and get()
        (unless explicitly fetched by exact version).

        Args:
            pack_id: The pack identifier.
            version: The version to yank.

        Raises:
            KeyError: If the pack version does not exist.
        """
        pack_dir = self._pack_dir(pack_id, version)
        if not pack_dir.is_dir():
            raise KeyError(f"Pack {pack_id}@{version} not found")

        # Create .yanked marker
        yanked_path = self._yanked_marker_path(pack_id, version)
        yanked_path.touch()
        logger.info("Yanked pack %s@%s", pack_id, version)

    def verify_digest(self, pack_id: str, version: str) -> bool:
        """Verify the integrity of a published pack by checking its digest.

        Recomputes the SHA256 of the manifest and compares it against the
        stored digest.txt.

        Args:
            pack_id: The pack identifier.
            version: The version.

        Returns:
            True if the digest matches, False otherwise.

        Raises:
            KeyError: If the pack version or digest file does not exist.
        """
        manifest_path = self._manifest_path(pack_id, version)
        digest_path = self._digest_path(pack_id, version)

        if not manifest_path.is_file():
            raise KeyError(f"Manifest not found for {pack_id}@{version}")

        if not digest_path.is_file():
            raise KeyError(f"Digest not found for {pack_id}@{version}")

        # Recompute digest
        manifest_json = manifest_path.read_text("utf-8")
        computed = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()

        # Read stored digest
        stored = digest_path.read_text("utf-8").strip()

        return computed == stored
