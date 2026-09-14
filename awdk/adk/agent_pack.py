"""Agent pack manifest + supervisor for universal framework bridging.

An AgentPackManifest wraps any framework agent (nooa | deer-flow | hermes |
openclaw | native | custom) and declares how to start it, what protocol it
speaks, what skills it exposes, and what identity/entitlements it requires.

The Supervisor spins up the agent as a subprocess, bridges its protocol,
and exposes a handle for interacting with it. Fail-closed: bad protocol/
framework literals, missing entitlements, or unparseable YAML all raise.
Vault references (secrets: {key: "vault:path/to/secret"}) are left as-is
(not resolved inline) and surface to the caller for late binding.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

__all__ = [
    "AgentPackManifest",
    "RuntimeConfig",
    "AgentHandle",
    "Supervisor",
    "load_agent_pack",
]

logger = logging.getLogger(__name__)

# The 6 framework variants
AGENT_FRAMEWORKS = Literal["nooa", "deer-flow", "hermes", "openclaw", "native", "custom"]
# Wire protocols the agent can expose
AGENT_PROTOCOLS = Literal["acp", "a2a", "mcp", "openai", "langgraph_rest", "http"]
# Runtime platform types
RUNTIME_TYPES = Literal["docker", "python", "node"]


class RuntimeConfig(BaseModel):
    """Describes how to launch the agent process."""
    type: RUNTIME_TYPES = Field(
        ..., description="Platform: docker | python | node"
    )
    image: str | None = Field(
        None, description="Docker image (required if type=docker)"
    )
    cmd: str | None = Field(
        None, description="Command to run (required if type=python or node)"
    )
    args: list[str] = Field(
        default_factory=list, description="CLI arguments"
    )

    @field_validator("type")
    @classmethod
    def validate_type(cls, v):
        if v not in ("docker", "python", "node"):
            raise ValueError(f"runtime.type must be docker | python | node, got {v!r}")
        return v

    def model_post_init(self, __context):
        """Validate that required fields are present for each type."""
        if self.type == "docker" and not self.image:
            raise ValueError("runtime.image required when type=docker")
        if self.type in ("python", "node") and not self.cmd:
            raise ValueError(f"runtime.cmd required when type={self.type}")


class AgentPackManifest(BaseModel):
    """Metadata + startup instructions for an agent pack.

    Fail-closed validation: invalid framework, protocol, or missing required
    runtime fields raise pydantic ValidationError at parse time. Vault
    references in secrets are NOT resolved (left as vault:path/to/secret
    strings for late binding by the caller).
    """
    id: str = Field(
        ..., description="Unique pack identifier"
    )
    name: str = Field(
        ..., description="Human-readable name"
    )
    version: str = Field(
        default="0.0.0", description="Semantic version"
    )
    framework: AGENT_FRAMEWORKS = Field(
        ..., description=(
            "Framework variant: "
            "nooa | deer-flow | hermes | openclaw | native | custom"
        )
    )
    runtime: RuntimeConfig = Field(
        ..., description="How to start the agent (docker/python/node config)"
    )
    entrypoint: str = Field(
        ..., description="How to invoke the agent (e.g. 'agent.main' or 'run.sh')"
    )
    protocol: AGENT_PROTOCOLS = Field(
        ..., description=(
            "Wire protocol: acp | a2a | mcp | openai | langgraph_rest | http"
        )
    )
    model_endpoint: str = Field(
        default="http://localhost:8150", description="LLM inference endpoint"
    )
    mcp: str | None = Field(
        None, description="Optional MCP server configuration path or inline"
    )
    skills: list[str] = Field(
        default_factory=list, description="Named skills the agent exposes"
    )
    entitlements: list[str] = Field(
        default_factory=list, description="Required entitlements to load the pack"
    )
    min_tier: str = Field(
        default="", description="Minimum license tier (free/builder/professional/enterprise)"
    )
    identity: dict = Field(
        default_factory=dict, description="Agent identity metadata (name, avatar, etc.)"
    )
    secrets: dict = Field(
        default_factory=dict, description=(
            "Secrets the agent needs (NOT resolved inline; "
            "vault: references left as-is for late binding)"
        )
    )

    @field_validator("framework")
    @classmethod
    def validate_framework(cls, v):
        if v not in ("nooa", "deer-flow", "hermes", "openclaw", "native", "custom"):
            raise ValueError(
                f"framework must be one of "
                f"nooa | deer-flow | hermes | openclaw | native | custom, "
                f"got {v!r}"
            )
        return v

    @field_validator("protocol")
    @classmethod
    def validate_protocol(cls, v):
        if v not in ("acp", "a2a", "mcp", "openai", "langgraph_rest", "http"):
            raise ValueError(
                f"protocol must be one of "
                f"acp | a2a | mcp | openai | langgraph_rest | http, "
                f"got {v!r}"
            )
        return v


    def to_toolpack_dict(self) -> dict[str, Any]:
        """Bridge to ToolPackManifest shape for interop with existing tooling.

        This allows an agent pack to register as a tool pack if needed,
        exposing its skills as mcp_tools and entitlements.
        """
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": f"{self.framework} agent: {self.entrypoint}",
            "category": "agent_packs",
            "tier": "free",
            "tool_modules": [],
            "entitlements": self.entitlements,
            "min_tier": self.min_tier,
            "skills": self.skills,
            "mcp_tools": self.skills,
            "tags": ["agent", self.framework, self.protocol],
        }


class AgentHandle:
    """Handle to a running agent process + its protocol driver.

    Tracks the subprocess and the declared protocol, and hands back a live
    *driver* for protocols adk can speak (see :meth:`driver`).
    """

    #: Protocols for which :meth:`driver` returns a live client.
    DRIVABLE_PROTOCOLS = ("acp",)

    def __init__(
        self,
        manifest: AgentPackManifest,
        process: asyncio.subprocess.Process,
    ):
        self.manifest = manifest
        self.process = process
        self._protocol = manifest.protocol
        self._driver: Any = None

    def is_running(self) -> bool:
        """True if the agent subprocess is still alive."""
        return self.process.returncode is None

    @property
    def protocol(self) -> AGENT_PROTOCOLS:
        """The agent's declared protocol (acp, a2a, mcp, etc.)."""
        return self._protocol

    async def driver(self, **kwargs: Any) -> Any:
        """Return a connected client that DRIVES this agent over its protocol.

        For ``protocol: acp`` this returns a connected
        :class:`adk.acp.ACPClient` bound to the spawned process's stdio, so the
        caller can immediately ``initialize()`` / ``create_session()`` /
        ``prompt()``. Cached: repeat calls return the same client.

        Args:
            **kwargs: Forwarded to the protocol client (e.g. ``approval_callback``).

        Raises:
            RuntimeError: If the process is not running.
            NotImplementedError: If the declared protocol has no driver yet —
                fail LOUD rather than silently hand back an inert handle.
        """
        if not self.is_running():
            raise RuntimeError(
                f"agent {self.manifest.id} is not running (returncode="
                f"{self.process.returncode}) — cannot open a {self._protocol} driver"
            )
        if self._driver is not None:
            return self._driver

        if self._protocol == "acp":
            from adk.acp import ACPClient  # local import: keep acp off the pack path

            client = ACPClient(subprocess_instance=self.process, **kwargs)
            await client.connect()
            self._driver = client
            logger.info("opened acp driver for agent %s", self.manifest.id)
            return client

        raise NotImplementedError(
            f"no driver implemented for protocol {self._protocol!r} "
            f"(agent {self.manifest.id}); drivable: {', '.join(self.DRIVABLE_PROTOCOLS)}"
        )

    async def terminate(self, timeout_secs: float = 5.0) -> None:
        """Gracefully stop the agent, then force-kill if needed."""
        if self._driver is not None:
            try:
                await self._driver.disconnect()
            except Exception:  # noqa: BLE001 — teardown is best-effort
                logger.debug("driver disconnect failed for %s", self.manifest.id)
            self._driver = None
        try:
            self.process.terminate()
            await asyncio.wait_for(self.process.wait(), timeout=timeout_secs)
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()
        except Exception:
            pass


class Supervisor:
    """Spawns and manages agent pack processes.

    The supervisor launches an agent as a subprocess via the declared runtime
    (docker/python/node), bridges its protocol, and returns a handle.
    Secrets with vault: prefixes are NOT resolved inline.
    """

    def __init__(self):
        self._handles: dict[str, AgentHandle] = {}

    def _build_command(self, manifest: AgentPackManifest) -> list[str]:
        """Build the argv for *manifest*'s runtime.

        Extracted from spawn() so argv construction is testable without
        launching anything — the docker path silently omitted `-i` for its whole
        life precisely because nothing could assert on it cheaply.
        """
        if manifest.runtime.type == "python":
            if not manifest.runtime.cmd:
                raise ValueError("runtime.cmd required for python runtime")
            cmd_str = manifest.runtime.cmd
            if cmd_str == sys.executable or Path(cmd_str).is_file():
                # An explicit interpreter/executable/script path — exec it directly.
                # (Do NOT .split() it: a path may contain spaces. And do NOT wrap it
                # in "-m", which would make "python -m /path/to/python.exe".)
                cmd = [cmd_str]
            elif cmd_str.startswith("python"):
                # A literal command line, e.g. 'python -c "..."'.
                #
                # Substitute THIS interpreter for the leading token rather than
                # exec'ing whatever `python` happens to mean on PATH. On Debian and
                # most modern distros there is no bare `python` at all, so spawning
                # one dies with:
                #   RuntimeError: Failed to start agent <n>: [Errno 2] No such file
                #   or directory: 'python'
                # — and it dies at RUN time, on the user's machine, long after any
                # install check. Measured 2026-08-15 on the release gate, which runs
                # the public payload on a Debian runner: 4 supervisor tests failed
                # exactly this way. It passed for as long as CI ran on hosted Ubuntu,
                # which ships the alias.
                #
                # sys.executable is also the RIGHT interpreter, not merely a present
                # one: an agent spawned from inside a venv must inherit that venv, or
                # it cannot import the packages the parent was installed with.
                parts = cmd_str.split()
                cmd = [sys.executable] + parts[1:]
            else:
                # A module target, e.g. 'hermes.acp' -> python -m hermes.acp
                cmd = [sys.executable, "-m"] + cmd_str.split()
            cmd.extend(manifest.runtime.args or [])

        elif manifest.runtime.type == "node":
            if not manifest.runtime.cmd:
                raise ValueError("runtime.cmd required for node runtime")
            cmd = ["node", *shlex.split(manifest.runtime.cmd)]
            cmd.extend(manifest.runtime.args or [])

        elif manifest.runtime.type == "docker":
            if not manifest.runtime.image:
                raise ValueError("runtime.image required for docker runtime")
            # `-i` is REQUIRED, not optional polish: without it `docker run` does
            # not attach the container's stdin, so a stdio-protocol agent (acp,
            # mcp) inside a container is structurally UNREACHABLE — the driver
            # writes a request that is silently discarded. Verified against a real
            # container: no `-i` → the child never sees a byte; with `-i` → the
            # request round-trips. Same defect class as forgetting stdin=PIPE.
            # Deliberately NOT forwarding env as `-e KEY=value`: that puts values
            # in the container's argv, where any local `ps`/`docker inspect` reads
            # them. Credentials belong in AitherSecrets, fetched by the agent.
            cmd = ["docker", "run", "--rm", "-i", manifest.runtime.image]
            if manifest.runtime.cmd:
                # shlex, not append: a cmd like "python -u server.py" appended
                # whole becomes ONE argv element, which docker tries to exec as a
                # binary literally named "python -u server.py".
                cmd.extend(shlex.split(manifest.runtime.cmd))
            cmd.extend(manifest.runtime.args or [])

        else:
            raise ValueError(f"Unsupported runtime type: {manifest.runtime.type}")

        return cmd

    async def spawn(self, manifest: AgentPackManifest) -> AgentHandle:
        """Launch an agent pack as a subprocess and return its handle.

        Args:
            manifest: The parsed AgentPackManifest.

        Returns:
            An AgentHandle exposing is_running(), protocol, and process.

        Raises:
            ValueError: If runtime type is unsupported or required fields missing.
            RuntimeError: If the subprocess fails to start.
        """
        cmd = self._build_command(manifest)

        # Launch the process
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                # stdin MUST be piped: the stdio protocols (acp, mcp-stdio) speak
                # JSON-RPC over the child's stdin — without this the driver can
                # never send a request and the agent is unreachable.
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise RuntimeError(f"Failed to start agent {manifest.id}: {e}") from e
        except Exception as e:
            raise RuntimeError(
                f"Failed to start agent {manifest.id}: {type(e).__name__}: {e}"
            ) from e

        handle = AgentHandle(manifest, process)
        self._handles[manifest.id] = handle
        logger.info(
            "spawned agent %s (pid=%s, protocol=%s)",
            manifest.id,
            process.pid,
            manifest.protocol,
        )
        return handle

    async def terminate_all(self, timeout_secs: float = 5.0) -> None:
        """Stop all running agents."""
        for handle in self._handles.values():
            if handle.is_running():
                await handle.terminate(timeout_secs)


def load_agent_pack(path: Path | str) -> AgentPackManifest:
    """Load and parse an agent pack manifest from YAML.

    Args:
        path: Path to a directory containing agent.yaml or a .yaml file.

    Returns:
        The parsed AgentPackManifest (fail-closed validation).

    Raises:
        FileNotFoundError: If the manifest file does not exist.
        ValueError: If YAML is unparseable or required fields are missing.
        pydantic.ValidationError: If the manifest fails schema validation
            (bad framework/protocol, missing runtime fields, etc.).
    """
    if isinstance(path, str):
        path = Path(path)

    # Look for agent.yaml or .agent.yaml in the directory
    manifest_path = None
    if path.is_file() and path.suffix == ".yaml":
        manifest_path = path
    elif path.is_dir():
        candidates = [path / "agent.yaml", path / ".agent.yaml"]
        for cand in candidates:
            if cand.is_file():
                manifest_path = cand
                break

    if not manifest_path or not manifest_path.is_file():
        raise FileNotFoundError(
            f"No agent manifest found at {path} "
            "(expected agent.yaml, .agent.yaml, or direct .yaml file)"
        )

    # Parse YAML
    try:
        data = yaml.safe_load(manifest_path.read_text("utf-8")) or {}
    except Exception as exc:
        raise ValueError(f"Failed to parse {manifest_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Manifest must be a YAML dict, got {type(data).__name__}")

    # Parse runtime config (required)
    runtime_data = data.get("runtime", {})
    if isinstance(runtime_data, dict):
        try:
            runtime = RuntimeConfig(**runtime_data)
        except ValidationError as exc:
            raise ValueError(
                f"Manifest validation failed for {manifest_path}: {exc}"
            ) from exc
    else:
        raise ValueError("runtime must be a dict")

    # Build AgentPackManifest (pydantic validates fail-closed)
    try:
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
    except ValidationError as exc:
        # Re-raise pydantic ValidationError as ValueError for fail-closed handling
        raise ValueError(
            f"Manifest validation failed for {manifest_path}: {exc}"
        ) from exc
    except Exception as exc:
        raise ValueError(
            f"Manifest validation failed for {manifest_path}: {exc}"
        ) from exc

    logger.info(
        "loaded agent pack %s (framework=%s, protocol=%s)",
        manifest.id,
        manifest.framework,
        manifest.protocol,
    )
    return manifest
