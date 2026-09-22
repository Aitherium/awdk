"""The harness registry — what AitherShell knows how to drive.

A *harness* is any interactive coding agent that can be driven from outside:
Claude Code, Gemini CLI, an ACP-speaking editor agent, a raw shell inside a dev
sandbox container, or AitherOS's own Genesis chat. Each is described by a
:class:`HarnessSpec` declaring how to launch it, how to feed it a turn, and
which adapter normalizes its output.

Adding a harness is meant to be a data change, not a code change — a spec plus
(if its output is novel) one adapter function.

Detection is honest by construction: :func:`detect` reports ``installed=False``
with an install hint rather than pretending a missing binary will work. A shell
that offers a harness it cannot start is worse than one that says so.
"""

from __future__ import annotations

import os
import io
import re
import sys
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from adk.harnesses.adapters import ADAPTERS
from adk.harnesses.events import HarnessEvent


class Transport(str, Enum):
    """How a session exchanges turns with its harness."""

    #: One long-lived process; JSON lines in on stdin, JSON lines out on stdout.
    #: The only transport that keeps model context warm across turns for free.
    STRUCTURED_BIDI = "structured-bidi"
    #: One process PER TURN; the prompt goes in argv/stdin, output streams back.
    #: Continuity depends on the harness's own --resume.
    ONESHOT_PER_TURN = "oneshot-per-turn"
    #: No local process — turns are relayed to an HTTP service (Genesis/adk).
    HTTP_STREAM = "http-stream"
    #: A real pseudo-terminal: raw bytes both ways, ANSI intact, resizable.
    #: This is what makes a terminal a TERMINAL rather than a shell-shaped chat
    #: box — job control, curses apps and colours all depend on it.
    PTY_STREAM = "pty-stream"


@dataclass
class LaunchSpec:
    """Everything a session needs to start one harness process."""

    cwd: str = ""
    model: str = ""
    #: Resolved per-session model binding (Claude Code only, today).
    setting_sources: str = ""
    permission_mode: str = ""
    resume_session_id: str = ""
    system_prompt_append: str = ""
    add_dirs: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    mcp_config: str = ""
    #: For ONESHOT_PER_TURN: the prompt for THIS turn.
    prompt: str = ""
    #: For RAW_STREAM/exec: target container or host.
    target: str = ""
    extra_args: list[str] = field(default_factory=list)
    #: For a PTY program harness (claude-tty): the id the daemon MINTED for this
    #: session, handed to the program so the daemon row and the program's own state
    #: files share one identity. Empty when resuming (resume_session_id wins).
    session_id: str = ""
    #: The session's display title, for harnesses that can carry one (`--name`).
    title: str = ""


@dataclass
class HarnessSpec:
    """A harness AitherShell knows how to drive."""

    id: str
    label: str
    description: str
    transport: Transport
    #: Executable name looked up on PATH. Empty for HTTP_STREAM harnesses.
    binary: str = ""
    version_argv: list[str] = field(default_factory=list)
    install_hint: str = ""
    #: Key into :data:`adk.harnesses.adapters.ADAPTERS`.
    adapter: str = "text"
    #: Per-session model profiles (AitherOS claude_profiles.yaml) are supported.
    supports_model_binding: bool = False
    #: The harness can resume its own prior session by id.
    supports_resume: bool = False
    #: Output is line-delimited JSON (vs. plain text).
    json_lines: bool = False
    #: Builds argv for a launch. Receives the spec itself for defaults.
    build_argv: Optional[Callable[["HarnessSpec", LaunchSpec], list[str]]] = None
    #: Encodes a user turn for STRUCTURED_BIDI stdin. Returns a line WITHOUT "\n".
    encode_input: Optional[Callable[[str], str]] = None
    #: Entitlement a caller must hold to SEE or START this harness. Empty = free
    #: to anyone the daemon authenticates, which is every harness shipped today.
    #:
    #: This is the missing half of licensing. `requires_plan:` has sat in every
    #: addon manifest unread because the daemon had one shared bearer and so no
    #: subject to check a plan against; per-client identity supplied the caller,
    #: and this supplies the thing being licensed. It lives on the spec rather
    #: than in a side table so a harness cannot be added without the question
    #: "who may drive this" having an answer -- even when the answer is "anyone".
    #:
    #: Deliberately an ENTITLEMENT, not a plan tier. A tier is a billing
    #: arrangement that gets renamed; "may this caller drive Atlas" is what the
    #: daemon actually has to decide, and it survives the renames.
    requires_entitlement: str = ""

    def translate(self, obj: Any) -> list[HarnessEvent]:
        return ADAPTERS[self.adapter](obj)

    def argv(self, launch: LaunchSpec) -> list[str]:
        if self.build_argv is None:
            raise RuntimeError(f"harness '{self.id}' has no build_argv")
        return self.build_argv(self, launch)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "transport": self.transport.value,
            "binary": self.binary,
            "install_hint": self.install_hint,
            "supports_model_binding": self.supports_model_binding,
            "supports_resume": self.supports_resume,
            # Surfaced so a client can explain WHY a harness it once saw is gone,
            # rather than showing a list that silently shrank.
            "requires_entitlement": self.requires_entitlement,
        }


# ─────────────────────────────────────────────────────────────────────────────
# argv builders
# ─────────────────────────────────────────────────────────────────────────────

def _claude_argv(spec: HarnessSpec, launch: LaunchSpec) -> list[str]:
    """Claude Code in bidirectional stream-json mode.

    ``--setting-sources`` is load-bearing: when a per-session model binding is
    active it must exclude ``user``, or the global profile in
    ``~/.claude/settings.json`` overrides our process env and the session
    silently runs the wrong model. See adk.harnesses.models.
    """
    argv = [
        spec.binary,
        "-p",
        "--output-format", "stream-json",
        "--input-format", "stream-json",
        "--verbose",
    ]
    if launch.setting_sources:
        argv += ["--setting-sources", launch.setting_sources]
    if launch.model:
        argv += ["--model", launch.model]
    if launch.permission_mode:
        argv += ["--permission-mode", launch.permission_mode]
    if launch.resume_session_id:
        argv += ["--resume", launch.resume_session_id]
    if launch.system_prompt_append:
        argv += ["--append-system-prompt", launch.system_prompt_append]
    for directory in launch.add_dirs:
        argv += ["--add-dir", directory]
    if launch.allowed_tools:
        argv += ["--allowed-tools", ",".join(launch.allowed_tools)]
    if launch.mcp_config:
        argv += ["--mcp-config", launch.mcp_config]
    argv += launch.extra_args
    return argv


def _claude_encode(text: str) -> str:
    import json as _json

    return _json.dumps(
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
    )


def _claude_tty_argv(spec: HarnessSpec, launch: LaunchSpec) -> list[str]:
    """Claude Code, the REAL interactive TUI, behind a daemon-owned pseudo-terminal.

    Deliberately NO ``-p`` / ``--output-format`` / ``--input-format``: those make the
    ``claude`` harness above headless. This one is the screen the owner already
    knows -- permission prompts, the input box, Ctrl-C -- with one difference: the
    daemon holds the keyboard, so a steer addressed to this session lands NOW
    (``steer_dispatch`` tier 1) instead of at the next turn boundary. Measured
    2026-09-19: every interactive Claude Code tab on this box was ``origin=discovered``
    and tier 1 never once fired; this harness is what makes a tab managed.

    ``--session-id`` is minted by the daemon (``PtyHarnessSession._resolve_argv``) so
    the row the daemon lists and the transcript Claude writes under
    ``~/.claude/projects`` carry ONE id and the session directory folds them.
    """
    argv = [spec.binary]
    if launch.resume_session_id:
        argv += ["--resume", launch.resume_session_id]
    elif launch.session_id:
        argv += ["--session-id", launch.session_id]
    if launch.title:
        argv += ["--name", launch.title]
    if launch.model:
        argv += ["--model", launch.model]
    if launch.permission_mode:
        argv += ["--permission-mode", launch.permission_mode]
    for directory in launch.add_dirs:
        argv += ["--add-dir", directory]
    if launch.mcp_config:
        argv += ["--mcp-config", launch.mcp_config]
    argv += launch.extra_args
    return argv


def _gemini_argv(spec: HarnessSpec, launch: LaunchSpec) -> list[str]:
    argv = [spec.binary, "-o", "stream-json", "-p", launch.prompt]
    if launch.model:
        argv += ["-m", launch.model]
    if launch.resume_session_id:
        argv += ["-r", launch.resume_session_id]
    if launch.allowed_tools:
        argv += ["--allowed-tools", *launch.allowed_tools]
    argv += launch.extra_args
    return argv


# ─────────────────────────────────────────────────────────────────────────────
# The registry
# ─────────────────────────────────────────────────────────────────────────────

SPECS: dict[str, HarnessSpec] = {}


def register(spec: HarnessSpec) -> HarnessSpec:
    SPECS[spec.id] = spec
    return spec


register(
    HarnessSpec(
        id="claude",
        label="Claude Code",
        description="Anthropic Claude Code — bidirectional stream-json, full tool use",
        transport=Transport.STRUCTURED_BIDI,
        binary="claude",
        version_argv=["--version"],
        install_hint="npm i -g @anthropic-ai/claude-code",
        adapter="claude",
        supports_model_binding=True,
        supports_resume=True,
        json_lines=True,
        build_argv=_claude_argv,
        encode_input=_claude_encode,
    )
)

register(
    HarnessSpec(
        id="claude-tty",
        label="Claude Code (terminal)",
        description=(
            "Anthropic Claude Code, the real interactive TUI behind a pseudo-terminal "
            "the daemon owns -- steerable immediately, resumable, one id"
        ),
        transport=Transport.PTY_STREAM,
        binary="claude",
        version_argv=["--version"],
        install_hint="npm i -g @anthropic-ai/claude-code",
        adapter="text",
        # The brain stays a frontier model: no per-session model binding, and
        # PtyHarnessSession scrubs MANAGED_VARS from the child env so a bridge
        # profile in the daemon's own environment can never become this tab's brain
        # (the ~26k-token always-on prompt floor does not fit a fleet window).
        supports_model_binding=False,
        supports_resume=True,
        build_argv=_claude_tty_argv,
    )
)

register(
    HarnessSpec(
        id="gemini",
        label="Gemini CLI",
        description="Google Gemini CLI — one process per turn, stream-json output",
        transport=Transport.ONESHOT_PER_TURN,
        binary="gemini",
        version_argv=["--version"],
        install_hint="npm i -g @google/gemini-cli",
        adapter="gemini",
        supports_resume=True,
        json_lines=True,
        build_argv=_gemini_argv,
    )
)

register(
    HarnessSpec(
        id="terminal",
        label="Terminal",
        description="A real shell on this host behind a pseudo-terminal (pwsh/bash)",
        transport=Transport.PTY_STREAM,
        binary="",
        adapter="text",
    )
)

register(
    HarnessSpec(
        id="sandbox",
        label="Dev Sandbox (Linux)",
        description="A real Linux TTY inside a dev-workspace container (docker exec -it)",
        transport=Transport.PTY_STREAM,
        binary="docker",
        version_argv=["--version"],
        install_hint="Install Docker Desktop",
        adapter="text",
    )
)

register(
    HarnessSpec(
        id="aither",
        label="Aither (sovereign agent)",
        description="An AitherOS agent (Aither/Atlas/Lyra/Aeon…) relayed over Genesis SSE",
        transport=Transport.HTTP_STREAM,
        binary="",
        adapter="text",
        supports_resume=True,
    )
)

register(
    HarnessSpec(
        id="awdk",
        label="awdk (local agent loop)",
        description=(
            "A sovereign agent run by THIS host's awdk loop (adk serve, default "
            "127.0.0.1:9001): local tools, the model you chose, no fleet needed. "
            "Same wire as `aither`, different brain -- the one the daily driver measures."
        ),
        transport=Transport.HTTP_STREAM,
        binary="",
        adapter="text",
        supports_resume=True,
    )
)

register(
    HarnessSpec(
        id="group",
        label="Group Chat",
        description="Several sovereign agents in one room, answering concurrently",
        transport=Transport.HTTP_STREAM,
        binary="",
        adapter="text",
    )
)


# Harnesses declared but not installed on every box. Declaring them means
# `harness list` tells you what AitherShell COULD drive and how to get it,
# instead of silently pretending the world is Claude-only.
for _late in (
    HarnessSpec(
        id="codex",
        label="OpenAI Codex CLI",
        description="OpenAI Codex CLI — one process per turn (codex exec --json)",
        transport=Transport.ONESHOT_PER_TURN,
        binary="codex",
        version_argv=["--version"],
        install_hint="npm i -g @openai/codex",
        adapter="text",
        json_lines=True,
        build_argv=lambda spec, launch: [spec.binary, "exec", "--json", launch.prompt],
    ),
    HarnessSpec(
        id="aider",
        label="Aider",
        description="Aider — pair-programming CLI (one process per turn)",
        transport=Transport.ONESHOT_PER_TURN,
        binary="aider",
        version_argv=["--version"],
        install_hint="pip install aider-install && aider-install",
        adapter="text",
        build_argv=lambda spec, launch: [
            spec.binary, "--no-pretty", "--yes", "--message", launch.prompt,
        ],
    ),
    HarnessSpec(
        id="opencode",
        label="OpenCode",
        description="OpenCode — open-source coding agent (one process per turn)",
        transport=Transport.ONESHOT_PER_TURN,
        binary="opencode",
        version_argv=["--version"],
        install_hint="npm i -g opencode-ai",
        adapter="text",
        build_argv=lambda spec, launch: [spec.binary, "run", launch.prompt],
    ),
):
    register(_late)


def get(harness_id: str) -> HarnessSpec:
    if harness_id not in SPECS:
        known = ", ".join(sorted(SPECS))
        raise KeyError(f"Unknown harness '{harness_id}'. Known: {known}")
    return SPECS[harness_id]


def exe_from_cmd_shim(cmd_path: str, text: Optional[str] = None) -> Optional[str]:
    """The .exe an npm ``.cmd`` shim launches, or None. Pure when ``text`` is given.

    npm's Windows shim is one line -- ``"%dp0%\\node_modules\\...\\bin\\claude.exe" %*``.
    Spawning the shim itself puts a ``cmd.exe`` between the pty and the program, so an
    interrupt or a kill reaches the wrapper, not Claude. Follow it to the executable.
    Same recipe the desk uses (``command-agent.cjs`` ``exeFromCmdShim``).
    """
    try:
        body = text if text is not None else io.open(cmd_path, encoding="utf-8").read()
    except OSError:
        return None
    m = re.search(r'"%dp0%\\([^"]+\.exe)"', body, re.IGNORECASE) or re.search(
        r'"%~dp0\\([^"]+\.exe)"', body, re.IGNORECASE
    )
    if not m:
        return None
    candidate = os.path.join(os.path.dirname(cmd_path), m.group(1))
    if text is not None or os.path.exists(candidate):
        return candidate
    return None


def resolve_binary(spec: HarnessSpec) -> Optional[str]:
    """Absolute path to the harness binary, or None when not installed.

    On Windows an npm-installed CLI resolves to its ``.cmd`` shim; when that shim
    names a real ``.exe`` next to it, answer the ``.exe`` (see ``exe_from_cmd_shim``).
    """
    if not spec.binary:
        return None
    override = os.environ.get(f"AITHER_HARNESS_{spec.id.upper()}_BIN")
    if override:
        return override if os.path.exists(override) else shutil.which(override)
    found = shutil.which(spec.binary)
    if found and sys.platform == "win32" and found.lower().endswith(".cmd"):
        return exe_from_cmd_shim(found) or found
    return found


def probe_version(spec: HarnessSpec, timeout: float = 12.0) -> str:
    """Best-effort version string. Empty when it cannot be determined."""
    path = resolve_binary(spec)
    if not path or not spec.version_argv:
        return ""
    try:
        proc = subprocess.run(
            [path, *spec.version_argv],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or proc.stderr or "").strip().splitlines()[0][:120] if (
        proc.stdout or proc.stderr
    ) else ""


def detect(*, with_version: bool = False) -> list[dict[str, Any]]:
    """Report every known harness and whether this box can actually run it."""
    # Imported lazily: pty_session imports session, which imports this module.
    from adk.harnesses.pty_session import pty_available

    pty_ok, pty_detail = pty_available()
    out: list[dict[str, Any]] = []
    for spec in SPECS.values():
        entry = spec.to_dict()
        if spec.transport == Transport.HTTP_STREAM:
            # No binary to find; availability is a service question answered by
            # the caller, so report it as available-by-configuration.
            entry["installed"] = True
            entry["path"] = ""
        elif spec.transport == Transport.PTY_STREAM:
            # A pty harness needs BOTH a pty backend and (for sandbox) docker.
            # Reporting it installed on the strength of one of those is how a
            # UI ends up offering a terminal that cannot open.
            path = resolve_binary(spec) if spec.binary else ""
            entry["installed"] = pty_ok and (bool(path) or not spec.binary)
            entry["path"] = path or ""
            entry["pty_backend"] = pty_detail
            if not pty_ok:
                entry["install_hint"] = pty_detail
        else:
            path = resolve_binary(spec)
            entry["installed"] = bool(path)
            entry["path"] = path or ""
        entry["version"] = probe_version(spec) if (with_version and entry["installed"]) else ""
        out.append(entry)
    return out
