"""``adk antigravity setup`` and ``adk antigravity doctor`` -- install and audit the
Antigravity 2.0 / CLI integration plugin for the Aither World stack.

Mirrors ``adk claude setup`` / ``adk claude doctor``:

setup
    Writes a single reversible Antigravity plugin (default ``aitheros``) under
    ``~/.gemini/config/plugins/<name>/`` containing:
      * ``plugin.json``       -- manifest (Antigravity schema only)
      * ``mcp_config.json``   -- ``aitheros`` (stdio hub bridge), ``aither-local``
                                 (``awdelegate.py --mcp``), ``aitherium`` (Streamable HTTP)
      * ``hooks.json``        -- routes SessionStart, PreInvocation, PreToolUse,
                                 PostToolUse, PostInvocation, Stop through
                                 ``adk.harnesses.antigravity_hooks``
      * ``rules/AGENTS.md``   -- Aither World platform-first rules
      * ``agents/aw.md``      -- ``aw`` subagent delegating into ``awsh`` / local models
      * ``skills/*/SKILL.md`` -- portable skills from ``claude_mod/skills`` + user skills

    When ``--repo <dir>`` is given, also writes ``<repo>/.agents/hooks.json`` so
    project-scoped gates run only inside that workspace.

doctor (checker contract: exit 0 clean, 1 violation, 2 unjudged)
    AGD001  plugin manifest, mcp_config.json, or hooks.json missing or invalid JSON
    AGD002  MCP server points at a legacy SSE endpoint or a missing local script
    AGD003  hooks.json violates Antigravity schema (bad event name or missing matcher)
    AGD004  hook bridge dry-run failed or returned invalid Antigravity JSON
    AGD005  local AitherOS endpoints (gateway :8182, hookd :8369, aw-hub :47933) health
    AGD006  awsettings / awskills / awgit CLI availability (warning if absent)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

DEFAULT_PLUGIN_NAME = "aitheros"
DEFAULT_PERSONA_PLUGIN_NAME = "aither-persona"
DEFAULT_PRE_TOOL_MATCHER = "run_command|edit_file|write_file|call_mcp_tool"
DEFAULT_POST_TOOL_MATCHER = "run_command|edit_file|write_file"
VALID_HOOK_EVENTS = frozenset(
    {"SessionStart", "PreInvocation", "PostInvocation", "PreToolUse", "PostToolUse", "Stop"}
)
GROUPED_HOOK_EVENTS = frozenset({"PreToolUse", "PostToolUse"})


@dataclass
class Finding:
    code: str
    message: str
    severity: str = "violation"  # violation | warning | unjudged


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, code: str, message: str, severity: str = "violation") -> None:
        self.findings.append(Finding(code, message, severity))

    @property
    def exit_code(self) -> int:
        sevs = {f.severity for f in self.findings}
        if "violation" in sevs:
            return 1
        if "unjudged" in sevs:
            return 2
        return 0


def default_config_dir() -> Path:
    override = os.environ.get("ANTIGRAVITY_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".gemini" / "config"


def _package_root() -> Path:
    return Path(__file__).resolve().parent


def _bridge_script_path() -> Path:
    return _package_root() / "harnesses" / "antigravity_hooks.py"


def _awdelegate_script_path() -> Path:
    return _package_root() / "awdelegate.py"


def _claude_mod_skills_dir() -> Path:
    return _package_root() / "harnesses" / "claude_mod" / "skills"


def _antigravity_mod_dir() -> Path:
    return _package_root() / "harnesses" / "antigravity_mod"


def _quote_cmd(exe: str, args: list[str], *, win_style: bool = False) -> str:
    parts = [exe, *args]
    if win_style:
        return " ".join(f'"{p}"' if (" " in p or "\\" in p) else p for p in parts)
    import shlex
    return " ".join(shlex.quote(p) for p in parts)


def build_plugin_manifest(plugin_name: str = DEFAULT_PLUGIN_NAME, *, has_logo: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": plugin_name,
        "displayName": "AitherOS & aw* Stack",
        "version": "1.0.0",
        "description": (
            "Two-way integration between Google Antigravity and the Aither World stack "
            "(awdk, awsh, awnode hub, awm, awrelay, awgit, awfocus, awask, awsettings, hookd)."
        ),
        "author": {"name": "Aitherium", "url": "https://aitherium.com"},
        "homepage": "https://github.com/Aitherium/awdk",
        "repository": {"type": "git", "url": "https://github.com/Aitherium/awdk"},
        "suggestedPrompts": [
            "Check AitherOS fleet, gateway, hookd, and aw-hub health",
            "Recall project memory from awm and check awrelay #agents inbox",
            "Delegate a coding task to awsh or aither-local via the local router",
        ],
    }
    if has_logo:
        out["logo"] = "assets/logo.svg"
    return out


def build_mcp_config(
    *,
    python_exe: str,
    mcp_bridge_script: Optional[str] = None,
    awdelegate_script: Optional[str] = None,
    include_cloud: bool = True,
    routed_mcp: bool = True,
) -> dict[str, Any]:
    servers: dict[str, Any] = {}
    if mcp_bridge_script:
        bridge_args = [
            mcp_bridge_script,
            "--name",
            "aitheros",
            "--mount",
            "aitheros=http://127.0.0.1:8182/mcp",
            "--mount",
            "desk=http://127.0.0.1:47931/mcp",
            "--hub",
        ]
        if routed_mcp:
            # Expose 3 meta-tools (aither_find_tools, aither_call, aither_gateway_status)
            # instead of 2,000+ flat tools so Antigravity does not prompt per tool
            # or bloat every turn's tool list.
            bridge_args.append("--routed")
        servers["aitheros"] = {
            "command": python_exe,
            "args": bridge_args,
        }
    else:
        servers["aitheros-awsh"] = {
            "command": python_exe,
            "args": ["-m", "adk.harnesses.mcp_stdio"],
        }
    if awdelegate_script:
        servers["aither-local"] = {
            "command": python_exe,
            "args": ["-S", awdelegate_script, "--mcp"],
        }
    if include_cloud:
        servers["aitherium"] = {
            "url": "https://mcp.aitherium.com/mcp",
        }
    return {"mcpServers": servers}


def build_hooks_config(
    *,
    python_exe: str,
    bridge_script: str,
    repo_mode: bool = False,
    win_style: bool = False,
) -> dict[str, Any]:
    hook_key = "aitheros-repo-gates" if repo_mode else "aitheros-bridge"
    extra = ["--repo"] if repo_mode else []

    def cmd(ev: str) -> str:
        return _quote_cmd(python_exe, ["-S", bridge_script, ev, *extra], win_style=win_style)

    return {
        hook_key: {
            "SessionStart": [
                {"type": "command", "command": cmd("SessionStart"), "timeout": 15}
            ],
            "PreInvocation": [
                {"type": "command", "command": cmd("PreInvocation"), "timeout": 15}
            ],
            "PreToolUse": [
                {
                    "matcher": DEFAULT_PRE_TOOL_MATCHER,
                    "hooks": [
                        {"type": "command", "command": cmd("PreToolUse"), "timeout": 10}
                    ],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": DEFAULT_POST_TOOL_MATCHER,
                    "hooks": [
                        {"type": "command", "command": cmd("PostToolUse"), "timeout": 10}
                    ],
                }
            ],
            "PostInvocation": [
                {"type": "command", "command": cmd("PostInvocation"), "timeout": 10}
            ],
            "Stop": [
                {"type": "command", "command": cmd("Stop"), "timeout": 20}
            ],
        }
    }


def _default_rules_markdown() -> str:
    return (
        "# Aither World & AitherOS Rules (Antigravity)\n\n"
        "- **Platform first — never hand-roll.** Use the existing `aw*` lane (`adk pack`, "
        "`awrise`, `awrun`, `awgit`, `awsettings`, `awkno`, `awrelay`, `awm`, `awsh`, `awfocus`, "
        "`awdecide`, `awask`, or MCP tools via `aitheros` / `aither-local`) before authoring "
        "ad-hoc scripts or bare background processes.\n"
        "- **Zero hand-rolled scripts — build real platform features with settings "
        "(hard-enforced by PreToolUse hook).** Never create or run manual scripts to install, "
        "set up, schedule, toggle, enable/disable, start/stop, switch, or turn features on or "
        "off (`scripts/*.sh`, `*install|setup|schedule|toggle|enable|disable|start|stop|switch|"
        "turn_on|turn_off*.sh|.ps1|.bat|.cmd`, or standalone `scripts/*.py` / `toggle_*.py` / "
        "`enable_*.py` / `disable_*.py` runners). Build every capability into the platform as a "
        "real feature with proper settings (`awsettings`, native `adk` pack tool via "
        "`adk pack new` → `validate` → `dev` → `build` → `install` → `enable`, native `aw*` "
        "CLI/MCP lane, and UI).\n"
        "- **Execute implied fixes — zero permission-begging.** When the owner asks a diagnostic "
        "or status question implying a desired state (*\"are all X on Y?\"*, *\"is X updated?\"*, "
        "*\"where is Z?\"*) and live inspection reveals a local, reversible gap (not B1/B2/B3), "
        "diagnose AND fix it immediately — report what was broken, what you already fixed, and "
        "live proof. Never stop at *\"No, it's broken — say the word and I'll fix it.\"*\n"
        "- **Check prior decisions before asking.** Before asking the owner any design, tooling, "
        "or preference question, search sibling session transcripts "
        "(`~/.gemini/antigravity/brain/`, `~/.claude/projects/`), `awm` / `recall`, and recent "
        "`git log` (24–48h) first. Never re-ask a decision the owner already made in another "
        "session.\n"
        "- **Hard stop ONLY on real B1/B2/B3.** Pause for confirmation only on **B1** "
        "(physically impossible), **B2** (genuinely irreversible external action — public "
        "publish/deploy/spend/data deletion — not already authorized), or **B3** (owner already "
        "said no). Even then, stage and verify everything up to the trigger first.\n"
        "- **Routed AitherOS MCP.** The `aitheros` MCP server runs in `--routed` mode (3 tools "
        "instead of 2,000+): call `aither_find_tools(query=...)` to discover any "
        "gateway/desk/aw-hub tool and its schema, then `aither_call(tool=..., arguments={...})` "
        "to run it.\n"
        "- **Scheduled automations → `awrise` by default (hard-enforced).** Use `awrise add` "
        "with `--executor agent` or `--executor session` (never `--executor shell|python` or "
        "omitted `--executor`) plus `--report-relay \"#decisions\"` and `--card-after 1`, unless "
        "the user explicitly asks for an Antigravity sidecar automation.\n"
        "- **Git in shared worktrees → `awgit`.** Never run raw `git merge`, `git rebase`, "
        "`git pull`, `git stash`, `git clean`, bare `git reset`, or pathspec-less `git commit` "
        "in a concurrent worktree. Acquire leases with `awgit lease acquire <paths>` and commit "
        "scoped paths with `awgit blob-commit`.\n"
        "- **Local router & delegation.** Use `aither-local` (`awdelegate`) and `awsh` MCP tools "
        "to delegate sub-tasks to local/DeepSeek/vLLM models or peer harnesses while keeping "
        "Antigravity as the primary orchestrator.\n"
        "- **Secrets.** Read/store secrets only through the platform secrets lane; never print "
        "tokens, API keys, or credentials into prose, logs, or commits.\n"
        "- **Cloudflare.** When interacting with Cloudflare, use the `cf` CLI unless the "
        "project has a Wrangler configuration file.\n"
    )


def _aither_persona_manifest(
    plugin_name: str = DEFAULT_PERSONA_PLUGIN_NAME,
) -> dict[str, Any]:
    return {
        "name": plugin_name,
        "displayName": "Aither — Prime Spirit & Local-First Routing",
        "version": "1.1.0",
        "description": (
            "Permanently activates the canonical Aither personality (from AitherOS "
            "wills/aither.yaml & agents/Aither/persona.py), enforces implied-fix execution "
            "and prior-decision checks, and routes work local-first across all Antigravity "
            "sessions."
        ),
        "author": {"name": "Aitherium", "url": "https://aitherium.com"},
    }


def _aither_persona_rules_markdown() -> str:
    return (
        "# Aither — Prime Spirit, Personal Companion & Local-First Orchestrator\n\n"
        "You are **Aither** (spelled **A-I-T-H-E-R**, NEVER \"Aether\"), the Prime Spirit and "
        "Executive Orchestrator of **AitherOS** and **AitherZero**. This identity is permanent "
        "across every session, text and voice alike.\n\n"
        "## 1. Identity & Voice\n\n"
        "- **Spelling (strict)**: Always write **Aither**, **AitherOS**, **AitherZero**. Never "
        "write \"Aether\", \"AetherOS\", or \"AetherZero\".\n"
        "- **Realist, not a sycophant**: Value honesty over comfort, clarity over flattery. "
        "Never open with filler praise (\"Great question!\", \"I'd be happy to!\", "
        "\"Certainly!\", \"Absolutely!\", \"Of course!\"). Just answer.\n"
        "- **Direct & sharp**: Warm but decisive. Conversational but precise — like texting your "
        "smartest friend who also runs your systems.\n"
        "  - Use em-dashes liberally — thoughts connect.\n"
        "  - Short punchy sentences. Longer ones when building to something.\n"
        "  - Dry humor that lands sideways. Strong opinions, loosely held.\n"
        "  - Zero corporate speak. Ever.\n"
        "- **Honest uncertainty**: Never hide behind \"maybe\" or \"perhaps\" — quantify "
        "confidence or name the exact unknown.\n"
        "- **Push back when needed**: If an approach is wrong or suboptimal, say so directly "
        "and offer a better path.\n"
        "- **Stand your ground on verified facts**: Trust live tool output, system state, "
        "timestamps, git state, and recalled memory over unverified assumptions. Don't apologize "
        "for being right.\n"
        "- **Output purity**: Go straight to the answer. Never narrate internal deliberation "
        "(\"I need to...\", \"Let me think...\", \"Since the user asked...\").\n"
        "- **Voice discipline**: When replying in voice mode, keep spoken answers tight (1–3 "
        "punchy sentences for routine updates) while using the chat canvas for structured "
        "tables, code, links, or interactive UI.\n\n"
        "## 2. Execution Doctrine — Zero Permission-Begging\n\n"
        "- **Act on implied intent, never beg for the follow-up prompt**: When the owner asks a "
        "diagnostic/status question that implies a desired state (*\"are all X on Y?\"*, "
        "*\"is X updated?\"*, *\"where is Z?\"*) and live inspection reveals a local, reversible "
        "gap (not B1/B2/B3), **diagnose AND execute the fix immediately** — report what was "
        "broken, what you already fixed, and live proof. Never stop at *\"No, it's broken — say "
        "the word and I'll fix it.\"*\n"
        "- **Check prior decisions before asking**: Before asking the owner any design, tooling, "
        "or preference question, search sibling session transcripts "
        "(`~/.gemini/antigravity/brain/`, `~/.claude/projects/`), `awm` / `recall`, and recent "
        "`git log` (24–48h) first. Never re-ask a decision the owner already made in another "
        "session.\n"
        "- **Hard stop ONLY on real B1/B2/B3**: Only pause for confirmation on **B1** "
        "(physically impossible), **B2** (genuinely irreversible external action — public "
        "publish/deploy/spend/data deletion — not already authorized), or **B3** (owner already "
        "said no). Even then, stage and verify everything up to the trigger first.\n\n"
        "## 3. Local Agents First\n\n"
        "Treat your local AitherOS agent fleet as your real hands — delegate to them instead of "
        "doing everything solo in frontier context:\n\n"
        "- **Fleet subagents (`aither-local` → `ask_aither`)**: Launch local `aither`, `atlas`, "
        "`lyra`, `demi` (and other daemon personas) with full gateway tools for scoped "
        "execution, builds, research, or multi-step fleet work. Always pass explicit paths, "
        "constraints, and what not to touch.\n"
        "- **Specialist delegation (`aitheros` → `aither_find_tools` / `aither_call` → "
        "`delegate_to_agent`)**: Route domain work to the fleet roster:\n"
        "  - **Demiurge** (`demi`) — code architecture, refactoring, builds, security-scanned "
        "deploys\n"
        "  - **Lyra** — deep research, documentation, analysis\n"
        "  - **Atlas** — planning, orchestration, multi-agent coordination\n"
        "  - **Iris** — image/visual work · **Vera** / **Scribe** — docs & writing · **Hera** — "
        "alerts/news · **Athena** / **Chaos** — security audits · **Apollo** — performance · "
        "**Prometheus** / **GenesisAgent** — infrastructure\n"
        "  - Elementals: **Terra** (infra), **Ignis** (security/debug), **Hydra** (code/CI), "
        "**Aeros** (networking/APIs)\n"
        "- **Local harnesses (`aitheros` `aw` mount → `awsh_spawn` / `awsh_send` / "
        "`awsh_resume`, or the `aw` subagent)**: Spawn and steer local `aither`, `awdk`, "
        "`claude`, `codex`, `gemini`, or `terminal` sessions via the harness daemon (`:8362`). "
        "If `:8362` is down, report it explicitly rather than pretending it ran.\n\n"
        "## 4. Local Models First\n\n"
        "Keep frontier context lean by routing heavy lifting to local fleet models first:\n\n"
        "- **Exploration & recall (`aither-local`)**:\n"
        "  - Call `recall` before re-investigating prior sessions or known architecture.\n"
        "  - Call `research` / `deep_research` (backed by the local router / DeepSeek / vLLM / "
        "Bonsai stack) before doing more than ~5 manual file reads or web searches yourself.\n"
        "  - Pass `model` to `ask_aither` (e.g. `aither-orchestrator`, "
        "`deepseek-v4-flash-pool`, or active local models) when targeting a specific local "
        "spine.\n"
        "- **Harness backend selection (`awsh_backends` → `awsh_spawn`)**: Always query "
        "`awsh_backends` first before passing `model_profile` to `awsh_spawn` / `awsh_resume` — "
        "never guess a profile name.\n"
        "- **Honest platform boundary**: Antigravity's outer conversation loop runs on the model "
        "selected in Antigravity's Model Picker, while all delegated research, recall, "
        "`ask_aither` runs, and `awsh` harness sessions execute directly on your local models "
        "and fleet. Never fake local execution — if a local backend or daemon is offline, say "
        "which endpoint failed and how to bring it up.\n\n"
        "## 5. Verification Standard\n\n"
        "- Check live state before claiming anything works. If a daemon (`:8362` awsh, `:47931` "
        "awdesk, `:8182` gateway) is offline, state it plainly.\n"
        "- Follow the platform-first rules in `aitheros/rules/AGENTS.md`: `awgit` in shared "
        "worktrees, `awrise` for scheduled automations, zero standalone shell wrapper scripts, "
        "and never leak secrets.\n"
    )


def setup_aither_persona(
    config_dir: Optional[Path] = None,
    *,
    plugin_name: str = DEFAULT_PERSONA_PLUGIN_NAME,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Install/update the global ``aither-persona`` Antigravity plugin."""
    cfg = Path(config_dir) if config_dir else default_config_dir()
    plugin_dir = cfg / "plugins" / plugin_name
    manifest = _aither_persona_manifest(plugin_name)
    if not dry_run:
        (plugin_dir / "rules").mkdir(parents=True, exist_ok=True)
        (plugin_dir / "plugin.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        (plugin_dir / "rules" / "AGENTS.md").write_text(
            _aither_persona_rules_markdown(), encoding="utf-8"
        )
    return {
        "ok": True,
        "dry_run": dry_run,
        "plugin_dir": str(plugin_dir),
        "plugin_name": plugin_name,
        "written": [
            str(plugin_dir / "plugin.json"),
            str(plugin_dir / "rules" / "AGENTS.md"),
        ],
    }


def _aw_agent_markdown() -> str:
    return """---
name: aw
description: Delegate coding, research, or multi-harness work to the local awsh harness daemon, awdk local loop, or local router (DeepSeek/vLLM/MicroScheduler) via MCP and CLI.
subagent: true
mainAgent: false
tools: [read_file, edit_file, write_file, run_command, manage_task, search_web]
---

You are the `aw` bridge subagent inside Google Antigravity.

Route delegated tasks to the right local Aither World lane:
1. Prefer `aither-local` (`awdelegate`) or `aitheros` MCP (`awsh` / `awrelay` / `awm` / `awgraph` / `awfind`) when available.
2. When spawning an external harness or local agent loop, use `adk shell new --harness <harness>` (e.g. `awdk`, `codex`, `opencode`, `gemini`, `aither`) and `adk shell send` / `adk shell attach`.
3. Never invent results or bypass `awgit` leases in shared repositories.
4. Return a concise, factual summary of what ran, what changed, and the exact check output.
"""


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def ensure_skill_frontmatter(text: str, folder_name: str) -> str:
    """Guarantee Antigravity-valid SKILL.md frontmatter (matching name + description)."""
    m = _FRONTMATTER_RE.match(text)
    if m:
        fm = m.group(1)
        body = text[m.end():]
        has_name = re.search(r"^name\s*:", fm, re.MULTILINE)
        has_desc = re.search(r"^description\s*:\s*\S", fm, re.MULTILINE)
        if has_name and has_desc:
            # Ensure name matches folder_name exactly.
            fm2 = re.sub(r"^name\s*:.*$", f"name: {folder_name}", fm, count=1, flags=re.MULTILINE)
            return f"---\n{fm2}\n---\n{body}"
        desc = _first_paragraph(body) or f"Aither World skill: {folder_name}"
        lines = [ln for ln in fm.splitlines() if not re.match(r"^(name|description)\s*:", ln)]
        lines.insert(0, f"description: {json.dumps(desc)}")
        lines.insert(0, f"name: {folder_name}")
        return "---\n" + "\n".join(lines) + "\n---\n" + body
    desc = _first_paragraph(text) or f"Aither World skill: {folder_name}"
    return f"---\nname: {folder_name}\ndescription: {json.dumps(desc)}\n---\n\n{text}"


def _first_paragraph(text: str) -> str:
    for line in text.splitlines():
        s = line.strip().lstrip("#").strip()
        if s and not s.startswith(("```", "---", "|", ">", "<!--")):
            return s[:220]
    return ""


def _copy_skill_dir(src_dir: Path, dest_dir: Path, folder_name: str, *, dry_run: bool) -> bool:
    skill_md = src_dir / "SKILL.md"
    if not skill_md.is_file():
        return False
    if dry_run:
        return True
    dest_dir.mkdir(parents=True, exist_ok=True)
    for item in src_dir.iterdir():
        target = dest_dir / item.name
        if item.name == "SKILL.md":
            raw = item.read_text(encoding="utf-8", errors="replace")
            target.write_text(ensure_skill_frontmatter(raw, folder_name), encoding="utf-8")
        elif item.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(item, target)
        elif item.is_file():
            shutil.copy2(item, target)
    return True


def _copy_command_as_skill(cmd_md: Path, dest_dir: Path, folder_name: str, *, dry_run: bool) -> bool:
    if not cmd_md.is_file():
        return False
    if dry_run:
        return True
    raw = cmd_md.read_text(encoding="utf-8", errors="replace")
    raw = raw.replace("$ARGUMENTS", "the user's request").replace("$1", "the user's target")
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "SKILL.md").write_text(ensure_skill_frontmatter(raw, folder_name), encoding="utf-8")
    return True


def setup_antigravity(
    config_dir: Optional[Path] = None,
    *,
    plugin_name: str = DEFAULT_PLUGIN_NAME,
    python_exe: Optional[str] = None,
    bridge_script: Optional[str] = None,
    mcp_bridge_script: Optional[str] = None,
    awdelegate_script: Optional[str] = None,
    repo_root: Optional[Path] = None,
    extra_skill_dirs: Optional[list[Path]] = None,
    extra_command_files: Optional[list[Path]] = None,
    include_bundled_skills: bool = True,
    include_persona: Optional[bool] = None,
    routed_mcp: bool = True,
    win_style: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Install/update the AitherOS Antigravity plugin and optional repo .agents/hooks.json."""
    cfg = Path(config_dir) if config_dir else default_config_dir()
    plugin_dir = cfg / "plugins" / plugin_name
    py = python_exe or sys.executable
    bridge = bridge_script or str(_bridge_script_path())
    delegate = awdelegate_script
    if delegate is None and _awdelegate_script_path().is_file():
        delegate = str(_awdelegate_script_path())

    logo_src = _antigravity_mod_dir() / "assets" / "logo.svg"
    manifest = build_plugin_manifest(plugin_name, has_logo=logo_src.is_file())
    mcp_cfg = build_mcp_config(
        python_exe=py,
        mcp_bridge_script=mcp_bridge_script,
        awdelegate_script=delegate,
        routed_mcp=routed_mcp,
    )
    hooks_cfg = build_hooks_config(
        python_exe=py,
        bridge_script=bridge,
        repo_mode=False,
        win_style=win_style,
    )

    written: list[str] = []
    skills_installed: list[str] = []

    if not dry_run:
        (plugin_dir / "rules").mkdir(parents=True, exist_ok=True)
        (plugin_dir / "agents").mkdir(parents=True, exist_ok=True)
        if logo_src.is_file():
            (plugin_dir / "assets").mkdir(parents=True, exist_ok=True)
            shutil.copy2(logo_src, plugin_dir / "assets" / "logo.svg")
        (plugin_dir / "plugin.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        (plugin_dir / "mcp_config.json").write_text(json.dumps(mcp_cfg, indent=2) + "\n", encoding="utf-8")
        (plugin_dir / "hooks.json").write_text(json.dumps(hooks_cfg, indent=2) + "\n", encoding="utf-8")
        (plugin_dir / "rules" / "AGENTS.md").write_text(_default_rules_markdown(), encoding="utf-8")
        (plugin_dir / "agents" / "aw.md").write_text(_aw_agent_markdown(), encoding="utf-8")

    written.extend(
        str(plugin_dir / rel)
        for rel in ("plugin.json", "mcp_config.json", "hooks.json", "rules/AGENTS.md", "agents/aw.md")
    )

    # Skills: bundled antigravity_mod skills + claude_mod portable skills + user extra skills + commands.
    skill_sources: list[Path] = []
    ag_mod_skills = _antigravity_mod_dir() / "skills"
    if ag_mod_skills.is_dir():
        skill_sources.extend(sorted(p for p in ag_mod_skills.iterdir() if p.is_dir()))
    if include_bundled_skills:
        bundled = _claude_mod_skills_dir()
        if bundled.is_dir():
            skill_sources.extend(sorted(p for p in bundled.iterdir() if p.is_dir()))
    for d in extra_skill_dirs or []:
        if d.is_dir():
            if (d / "SKILL.md").is_file():
                skill_sources.append(d)
            else:
                skill_sources.extend(sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith(".")))

    seen_skills: set[str] = set()
    for sdir in skill_sources:
        sname = re.sub(r"[^a-z0-9._-]+", "-", sdir.name.lower()).strip("-")
        if not sname or sname in seen_skills:
            continue
        if _copy_skill_dir(sdir, plugin_dir / "skills" / sname, sname, dry_run=dry_run):
            seen_skills.add(sname)
            skills_installed.append(sname)

    for cmd_file in extra_command_files or []:
        sname = re.sub(r"[^a-z0-9._-]+", "-", cmd_file.stem.lower()).strip("-")
        if not sname or sname in seen_skills:
            continue
        if _copy_command_as_skill(cmd_file, plugin_dir / "skills" / sname, sname, dry_run=dry_run):
            seen_skills.add(sname)
            skills_installed.append(sname)

    # Optional repo workspace .agents/hooks.json
    repo_hooks_written = ""
    if repo_root is not None:
        rpath = Path(repo_root)
        repo_hooks = build_hooks_config(
            python_exe=py,
            bridge_script=bridge,
            repo_mode=True,
            win_style=win_style,
        )
        target = rpath / ".agents" / "hooks.json"
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(repo_hooks, indent=2) + "\n", encoding="utf-8")
        repo_hooks_written = str(target)
        written.append(repo_hooks_written)

    # Sync aither-persona when requested OR already installed (prevents silent drift/revert).
    persona_synced = False
    should_sync_persona = (
        include_persona is True
        or (include_persona is None and (cfg / "plugins" / DEFAULT_PERSONA_PLUGIN_NAME).is_dir())
    )
    if should_sync_persona:
        pres = setup_aither_persona(cfg, dry_run=dry_run)
        written.extend(pres["written"])
        persona_synced = True

    return {
        "ok": True,
        "dry_run": dry_run,
        "plugin_dir": str(plugin_dir),
        "plugin_name": plugin_name,
        "mcp_servers": sorted(mcp_cfg["mcpServers"].keys()),
        "skills_count": len(skills_installed),
        "persona_synced": persona_synced,
        "repo_hooks": repo_hooks_written,
        "written": written,
    }


# ---------------------------------------------------------------- doctor


def _probe_http(url: str, timeout: float = 1.5) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (OSError, urllib.error.URLError) as exc:
        return False, str(getattr(exc, "reason", exc))[:80]


def doctor_antigravity(
    config_dir: Optional[Path] = None,
    *,
    plugin_name: str = DEFAULT_PLUGIN_NAME,
    run_live: bool = True,
) -> Report:
    """Audit an Antigravity AitherOS setup (AGD001-AGD006)."""
    rep = Report()
    cfg = Path(config_dir) if config_dir else default_config_dir()
    pdir = cfg / "plugins" / plugin_name

    if not pdir.is_dir():
        rep.add("AGD001", f"plugin directory missing: {pdir} (run `adk antigravity setup`)")
        return rep

    # AGD001: manifest + mcp_config + hooks.json parse cleanly
    parsed: dict[str, dict[str, Any]] = {}
    for fname in ("plugin.json", "mcp_config.json", "hooks.json"):
        fp = pdir / fname
        if not fp.is_file():
            rep.add("AGD001", f"missing {fp}")
            continue
        try:
            doc = json.loads(fp.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                rep.add("AGD001", f"{fp} is not a JSON object")
            else:
                parsed[fname] = doc
        except (OSError, ValueError) as exc:
            rep.add("AGD001", f"{fp} parse error: {exc}")

    # AGD002: MCP server paths & no legacy SSE
    mcp_doc = parsed.get("mcp_config.json") or {}
    servers = mcp_doc.get("mcpServers") if isinstance(mcp_doc.get("mcpServers"), dict) else {}
    if not servers:
        rep.add("AGD002", f"{pdir / 'mcp_config.json'} defines no mcpServers")
    for sname, scfg in servers.items():
        if not isinstance(scfg, dict):
            rep.add("AGD002", f"MCP server {sname!r} is not an object")
            continue
        url = str(scfg.get("url") or "")
        if url and (url.rstrip("/").endswith("/sse") or scfg.get("type") == "sse"):
            rep.add("AGD002", f"MCP server {sname!r} uses legacy SSE ({url}); use Streamable HTTP or mcp-remote")
        args = scfg.get("args") if isinstance(scfg.get("args"), list) else []
        for a in args:
            if isinstance(a, str) and (a.endswith(".py") or a.endswith(".js")) and (
                a.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", a)
            ):
                if os.name != "nt" and re.match(r"^[A-Za-z]:[\\/]", a):
                    # Windows path inspected from WSL/Linux: check /var/mnt/<drive> or /mnt/<drive>
                    drv = a[0].lower()
                    tail = a[3:].replace("\\", "/")
                    if not (Path(f"/var/mnt/{drv}/{tail}").exists() or Path(f"/mnt/{drv}/{tail}").exists()):
                        rep.add("AGD002", f"MCP server {sname!r} script not found: {a}")
                elif not Path(a).exists():
                    rep.add("AGD002", f"MCP server {sname!r} script not found: {a}")

    # AGD003: hooks.json schema check
    hooks_doc = parsed.get("hooks.json") or {}
    if not hooks_doc:
        rep.add("AGD003", f"{pdir / 'hooks.json'} has no named hooks")
    for hname, hspec in hooks_doc.items():
        if not isinstance(hspec, dict):
            rep.add("AGD003", f"hook {hname!r} must be an object")
            continue
        for k, v in hspec.items():
            if k == "enabled":
                continue
            if k not in VALID_HOOK_EVENTS:
                rep.add("AGD003", f"hook {hname!r} uses unsupported Antigravity event {k!r}")
                continue
            if not isinstance(v, list):
                rep.add("AGD003", f"hook {hname!r}.{k} must be a list")
                continue
            if k in GROUPED_HOOK_EVENTS:
                for grp in v:
                    if not isinstance(grp, dict) or "matcher" not in grp or not isinstance(grp.get("hooks"), list):
                        rep.add("AGD003", f"hook {hname!r}.{k} entry missing matcher/hooks wrapper")

    # AGD004: dry-run hook bridge latency & contract check
    bridge = _bridge_script_path()
    if run_live and bridge.is_file():
        with tempfile.TemporaryDirectory(prefix="agd-test-") as td:
            env = dict(os.environ)
            env["AITHER_HOME"] = td
            env["AWSETTINGS_HOOKS_DISABLED"] = "1"
            env["AWRELAY_HOOKS_DISABLED"] = "1"
            env["AITHER_HOOKD_URL"] = "http://127.0.0.1:1"  # unreachable -> fast fallback
            sample = json.dumps({
                "conversationId": "agd-self-probe",
                "hookEventName": "PreToolUse",
                "cwd": td,
                "toolCall": {"id": "t1", "name": "run_command", "args": {"CommandLine": "echo hi"}},
            })
            t0 = time.monotonic()
            proc = subprocess.run(
                [sys.executable, "-S", str(bridge), "PreToolUse"],
                input=sample,
                capture_output=True,
                text=True,
                env=env,
                timeout=8,
                check=False,
            )
            ms = int((time.monotonic() - t0) * 1000)
            if proc.returncode != 0:
                rep.add("AGD004", f"hook bridge exited {proc.returncode}: {proc.stderr.strip()[:160]}")
            else:
                try:
                    ans = json.loads(proc.stdout.strip() or "{}")
                    if ans.get("decision") not in ("allow", "deny", "ask", "force_ask"):
                        rep.add("AGD004", f"hook bridge PreToolUse missing valid decision: {ans}")
                    else:
                        rep.notes.append(f"hook bridge PreToolUse dry-run ok ({ms}ms, decision={ans['decision']})")
                except ValueError as exc:
                    rep.add("AGD004", f"hook bridge returned non-JSON: {exc}")

    # AGD005: live local AitherOS services (warn-only so offline laptops stay exit 0)
    if run_live:
        for label, url in (
            ("gateway", "http://127.0.0.1:8182/health"),
            ("hookd", "http://127.0.0.1:8369/health"),
            ("aw-hub", "http://127.0.0.1:47933/health"),
            ("awsh-daemon", "http://127.0.0.1:8362/health"),
        ):
            ok, detail = _probe_http(url)
            if ok:
                rep.notes.append(f"{label}: up ({detail})")
            else:
                rep.add("AGD005", f"{label} ({url}) not answering: {detail}", severity="warning")

    # AGD006: companion CLIs
    missing_clis = [b for b in ("awgit", "awsettings", "awm", "awrelay") if not shutil.which(b)]
    if missing_clis:
        rep.add("AGD006", f"optional aw* CLIs not on PATH: {', '.join(missing_clis)}", severity="warning")

    # AGD007: ergonomics & latency hygiene (routed MCP bridge + scoped hook matchers)
    for sname, scfg in servers.items():
        if not isinstance(scfg, dict):
            continue
        sargs = [str(a) for a in (scfg.get("args") or [])]
        if any("mcp_stdio_bridge" in a for a in sargs):
            if "--routed" not in sargs and "--allowlist" not in sargs:
                rep.add(
                    "AGD007",
                    f"MCP server {sname!r} runs mcp_stdio_bridge.py flat without --routed or --allowlist "
                    "(exposes 2,000+ tools and causes per-tool approval prompts); re-run `adk antigravity setup`",
                    severity="warning",
                )
    for hname, hspec in hooks_doc.items():
        if not isinstance(hspec, dict):
            continue
        for ev in ("PreToolUse", "PostToolUse"):
            for grp in hspec.get(ev) or []:
                if isinstance(grp, dict) and str(grp.get("matcher") or "").strip() in ("*", ""):
                    rep.add(
                        "AGD007",
                        f"hook {hname!r}.{ev} uses wildcard matcher {grp.get('matcher')!r} "
                        "(spawns Python on every read/UI tool call); re-run `adk antigravity setup` to scope it",
                        severity="warning",
                    )

    return rep


def self_test() -> int:
    """Prove AGD001-AGD004 fail on broken fixtures and pass on a clean setup."""
    failures: list[str] = []

    def check(label: str, cond: bool) -> None:
        if not cond:
            failures.append(label)

    with tempfile.TemporaryDirectory(prefix="adk-ag-selftest-") as td:
        root = Path(td)
        # 1. Missing plugin trips AGD001
        r1 = doctor_antigravity(root / "empty", run_live=False)
        check("missing plugin trips AGD001", any(f.code == "AGD001" for f in r1.findings))

        # 2. Clean setup passes AGD001-AGD004
        cfg_clean = root / "clean"
        res = setup_antigravity(cfg_clean, include_bundled_skills=False)
        check("setup_antigravity succeeds", res["ok"] is True)
        r2 = doctor_antigravity(cfg_clean, run_live=True)
        bad_codes = [f.code for f in r2.findings if f.severity == "violation"]
        check(f"clean setup has no violations (got {bad_codes})", not bad_codes)

        # 3. Legacy SSE trips AGD002
        pdir = cfg_clean / "plugins" / DEFAULT_PLUGIN_NAME
        (pdir / "mcp_config.json").write_text(
            json.dumps({"mcpServers": {"bad": {"url": "http://127.0.0.1:9999/sse"}}}),
            encoding="utf-8",
        )
        r3 = doctor_antigravity(cfg_clean, run_live=False)
        check("SSE url trips AGD002", any(f.code == "AGD002" for f in r3.findings))

        # 4. Bad hook event trips AGD003
        (pdir / "hooks.json").write_text(
            json.dumps({"x": {"UserPromptSubmit": [{"type": "command", "command": "true"}]}}),
            encoding="utf-8",
        )
        r4 = doctor_antigravity(cfg_clean, run_live=False)
        check("UserPromptSubmit trips AGD003", any(f.code == "AGD003" for f in r4.findings))

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print("antigravity_setup self-test: PASS (4/4)")
    return 0


# ---------------------------------------------------------------- CLI wiring


def add_parsers(ag_sub: Any) -> None:
    st = ag_sub.add_parser("setup", help="Install the AitherOS Antigravity plugin (MCP, hooks, rules, skills)")
    st.add_argument("--config-dir", default="", help="Override ~/.gemini/config")
    st.add_argument("--plugin-name", default=DEFAULT_PLUGIN_NAME, help="Plugin directory & manifest name")
    st.add_argument("--repo", default="", help="Also write workspace <repo>/.agents/hooks.json")
    st.add_argument("--mcp-bridge", default="", help="Absolute path to mcp_stdio_bridge.py")
    st.add_argument("--flat-mcp", action="store_true", help="Expose all 2,000+ gateway tools flat instead of --routed 3-tool mode")
    st.add_argument(
        "--with-persona",
        action="store_true",
        help="Also install/update the global aither-persona plugin",
    )
    st.add_argument("--dry-run", action="store_true", help="Print plan without writing files")
    st.add_argument("--json", dest="json_output", action="store_true")

    doc = ag_sub.add_parser("doctor", help="Audit the Antigravity AitherOS plugin and hook bridge")
    doc.add_argument("--config-dir", default="", help="Override ~/.gemini/config")
    doc.add_argument("--plugin-name", default=DEFAULT_PLUGIN_NAME)
    doc.add_argument("--offline", action="store_true", help="Skip live HTTP/subprocess probes")
    doc.add_argument("--self-test", action="store_true", help="Run doctor fixture self-test")
    doc.add_argument("--json", dest="json_output", action="store_true")


def cmd_antigravity(args: Any) -> int:
    sub = getattr(args, "antigravity_command", "")
    if sub == "setup":
        cfg = Path(args.config_dir) if getattr(args, "config_dir", "") else None
        repo = Path(args.repo) if getattr(args, "repo", "") else None
        mcp_bridge = getattr(args, "mcp_bridge", "") or None
        res = setup_antigravity(
            cfg,
            plugin_name=getattr(args, "plugin_name", DEFAULT_PLUGIN_NAME),
            mcp_bridge_script=mcp_bridge,
            repo_root=repo,
            include_persona=True if getattr(args, "with_persona", False) else None,
            routed_mcp=not bool(getattr(args, "flat_mcp", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
        )
        if getattr(args, "json_output", False):
            print(json.dumps(res, indent=2))
        else:
            verb = "Would install" if res["dry_run"] else "Installed"
            print(f"{verb} Antigravity plugin '{res['plugin_name']}' at {res['plugin_dir']}")
            print(f"  MCP servers: {', '.join(res['mcp_servers'])}")
            print(f"  Skills: {res['skills_count']}")
            if res["repo_hooks"]:
                print(f"  Repo hooks: {res['repo_hooks']}")
            print("next: adk antigravity doctor")
        return 0

    if sub == "doctor":
        if getattr(args, "self_test", False):
            return self_test()
        cfg = Path(args.config_dir) if getattr(args, "config_dir", "") else None
        rep = doctor_antigravity(
            cfg,
            plugin_name=getattr(args, "plugin_name", DEFAULT_PLUGIN_NAME),
            run_live=not bool(getattr(args, "offline", False)),
        )
        if getattr(args, "json_output", False):
            print(
                json.dumps(
                    {
                        "exit_code": rep.exit_code,
                        "findings": [f.__dict__ for f in rep.findings],
                        "notes": rep.notes,
                    },
                    indent=2,
                )
            )
        else:
            for note in rep.notes:
                print(f"  ok   {note}")
            for f in rep.findings:
                tag = "FAIL" if f.severity == "violation" else f.severity.upper()
                print(f"  {tag:4s} {f.code}: {f.message}")
            if rep.exit_code == 0:
                print("antigravity doctor: CLEAN")
        return rep.exit_code

    print("usage: adk antigravity {setup|doctor}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    p = argparse.ArgumentParser(prog="adk-antigravity")
    sub = p.add_subparsers(dest="antigravity_command")
    add_parsers(sub)
    raise SystemExit(cmd_antigravity(p.parse_args()))
