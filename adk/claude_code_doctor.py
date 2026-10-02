"""``adk claude setup`` and ``adk claude doctor`` -- reproduce and audit a Claude Code setup.

setup
    Installs the ``awsh@awsh`` plugin that ships inside this package
    (``adk/harnesses/claude_mod``) through the ``claude plugin`` CLI, or -- when the
    CLI is absent -- by merging ``extraKnownMarketplaces`` + ``enabledPlugins`` into
    ``~/.claude/settings.json`` (atomically, after a timestamped backup). Then runs
    ``awsettings preset apply aitherium-claude`` when the installed awsettings has the
    ``preset`` verb and knows that preset (probed with the read-only ``preset list``);
    an older awsettings is skipped with a line saying why. ``--dry-run`` prints the
    plan and changes nothing.

doctor (a checker: exit 0 clean, 1 violation, 2 could-not-judge)
    CCD001  autoMode in a project/local settings file (Claude Code reads autoMode only
            from user and managed settings, so those rules are dead)
    CCD002  the same hook command registered on the same event in more than one scope
            (it runs once per scope)
    CCD003  enabledMcpjsonServers names a server absent from the nearest .mcp.json, or
            omits one present there (neither enabled nor disabled)
    CCD004  a command hook on PreToolUse/PostToolUse whose matcher hits Bash (or all
            tools) with a median runtime over the budget on a harmless payload. The
            hooks really run, so they run SANDBOXED: a throwaway HOME, and
            AWSETTINGS_HOOKS_DISABLED / AWRELAY_HOOKS_DISABLED set so those hooks
            return at once (no settings push, no relay inbox drain).
    CCD005  a settings key Claude Code does not know (typo or a dead key). Judged
            against the shipped key list alone; the installed binary is consulted
            only for a hint, never to excuse a key (every common word is in it).
    CCD006  voice enabled but no claude.ai login or no audio capture device (warn only)

    ``--self-test`` builds fixtures that must trip CCD001-CCD005 and a clean one that
    must not, proving the checker can still fail.

Nothing here is machine-specific: every path is derived from the home directory, the
project root and the installed package.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------- keys
# Top-level settings keys Claude Code declares in its settings schema (extracted from
# the 2.1.x settings reference). This list IS the judgement: a key outside it and
# outside INTENTIONAL_CUSTOM_KEYS is reported. The installed binary is not asked to
# excuse a key -- a grep for `<key>:` in a 200 MB bundle finds `mcpServers:` and
# `apiKey:` too, so every common dead key read as known.
KNOWN_SETTINGS_KEYS = frozenset(
    """
$schema apiKeyHelper proxyAuthHelper awsCredentialExport awsAuthRefresh gcpAuthRefresh
processWrapper policyHelper policyHelpers fileSuggestion respectGitignore breakReminder
quietHours cleanupPeriodDays desktopSessionCleanupPeriodDays syncClaudeAiSkills
syncClaudeAiPlugins skillListingMaxDescChars skillListingBudgetFraction
wslInheritsWindowsSettings env attribution includeCoAuthoredBy includeGitInstructions
permissions model fallbackModel availableModels enforceAvailableModels
availableModelsMatch deniedModels modelOverrides modelPicker modelPricing
enableAllProjectMcpServers enabledMcpjsonServers disabledMcpjsonServers
disableClaudeAiConnectors skillOverrides disableBundledSkills managedMcpServers
allowedMcpServers deniedMcpServers hooks worktree disableAllHooks disableAgentView
disableRemoteControl disableWorkflows disableArtifact enableArtifact enableWorkflows
workflowSizeGuideline workflowKeywordTriggerEnabled disableSkillShellExecution
defaultShell bashEditDiffEnabled bashOutputMaxChars taskOutputMaxChars
respondToBashCommands allowManagedHooksOnly allowedHttpHookUrls httpHookAllowedEnvVars
allowManagedPermissionRulesOnly allowManagedMcpServersOnly allowAllClaudeAiMcps
allowClaudeInChromeWithManagedMcp strictPluginOnlyCustomization statusLine prUrlTemplate
footerLinksRegexes subagentStatusLine enabledPlugins prependPlugins appendPlugins
extraKnownMarketplaces additionalMarketplaces strictKnownMarketplaces
allowedMarketplaces blockedMarketplaces disableCommandPluginSources
disableSideloadFlags pluginSuggestionMarketplaces forceLoginMethod forceLoginGatewayUrl
gatewayInternalNetworks parentSettingsBehavior managedSourcesBehavior forceLoginOrgUUID
forceRemoteSettingsRefresh otelHeadersHelper outputStyle viewMode language
skipWebFetchPreflight sandbox feedbackSurveyRate feedbackDrafts spinnerTipsEnabled
spinnerVerbs spinnerTipsOverride syntaxHighlightingDisabled maxProseWidth spellcheck
terminalTitleFromRename promptCacheTtl subagentPromptCacheTtl alwaysThinkingEnabled
effortLevel maxEffortLevel modelSettings ultracode autoCompactWindow advisorModel
fastMode fastModePerSessionOptIn promptSuggestionEnabled emojiCompletionEnabled
awaySummaryEnabled showClearContextOnPlanAccept askUserQuestionTimeout dialogExpiry
agent modelProposedGoals companyAnnouncements pluginConfigs remote autoUpdatesChannel
minimumVersion requiredMinimumVersion requiredMaximumVersion plansDirectory tui voice
channelsEnabled allowedChannelPlugins prefersReducedMotion timeFormat timeZone
doneMeansMerged totalTokensReminder totalTokensReminderBudget
totalTokensReminderAfterUserTurn autoMemoryEnabled autoMemoryDirectory autoDreamEnabled
showThinkingSummaries skipDangerousModePermissionPrompt skipWorkflowUsageWarning
disableAutoMode remoteTools sshConfigs claudeMd claudeMdExcludes pluginTrustMessage
theme editorMode keybindingFlavor vimInsertModeRemaps verbose preferredNotifChannel
autoCompactEnabled precomputeCompactionEnabled switchModelsOnFlag
autoContinueAtUsageLimit autoScrollEnabled wheelScrollAccelerationEnabled
fileCheckpointingEnabled showTurnDuration showMessageTimestamps
terminalProgressBarEnabled todoFeatureEnabled teammateMode remoteControlAtStartup
remoteControl isolatePeerMachines daemonColdStart crossSessionInbound
autoUploadSessions inputNeededNotifEnabled agentPushNotifEnabled autoMode
""".split()
)

# Keys that are ours on purpose. Claude Code ignores them; our tools read them.
# `adk claude doctor --allow-key K` adds more for one run.
INTENTIONAL_CUSTOM_KEYS = frozenset({"aitherLane"})

# Keys people commonly put in settings.json that Claude Code does not read there.
# Each carries the hint the report prints.
DEAD_KEYS: dict[str, str] = {
    "mcpServers": "MCP servers belong in .mcp.json or ~/.claude.json, not settings.json",
    "apiKey": "use apiKeyHelper or the ANTHROPIC_API_KEY env var",
    "allowedTools": "use permissions.allow",
    "disallowedTools": "use permissions.deny",
    "ignorePatterns": "use permissions.deny with Read(...) rules",
    "customApiKeyResponses": "lives in ~/.claude.json, not settings.json",
    "dangerouslySkipPermissions": "a CLI flag, not a setting (see permissions.defaultMode)",
    "autoApprove": "use permissions.allow",
    "systemPrompt": "use CLAUDE.md or --append-system-prompt",
    "maxTokens": "set CLAUDE_CODE_MAX_OUTPUT_TOKENS in env",
}

HOT_EVENTS = ("PreToolUse", "PostToolUse")
DEFAULT_BUDGET_MS = 300
TIMING_RUNS = 3
HOOK_TIMEOUT_S = 15

PLUGIN_ID = "awsh@awsh"
MARKETPLACE = "awsh"
AWSETTINGS_PRESET = "aitherium-claude"


# ------------------------------------------------------------------------ model
@dataclass
class Scope:
    name: str  # user | project | local | managed
    path: Path
    data: dict | None = None
    error: str = ""


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
        sev = {f.severity for f in self.findings}
        if "violation" in sev:
            return 1
        if "unjudged" in sev:
            return 2
        return 0


# ---------------------------------------------------------------------- locate
def managed_settings_path() -> Path:
    if sys.platform == "win32":
        return (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "ClaudeCode"
            / "managed-settings.json"
        )
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    return Path("/etc/claude-code/managed-settings.json")


def find_project_root(start: Path) -> Path:
    start = start.resolve()
    for d in (start, *start.parents):
        if (d / ".claude").is_dir() or (d / ".git").exists():
            return d
    return start


def find_nearest_mcp_json(start: Path, stop: Path | None = None) -> Path | None:
    start = start.resolve()
    for d in (start, *start.parents):
        p = d / ".mcp.json"
        if p.is_file():
            return p
        if stop is not None and d == stop.resolve().parent:
            break
    return None


def load_scopes(home: Path, project: Path, managed: Path | None = None) -> list[Scope]:
    candidates = [
        ("user", home / ".claude" / "settings.json"),
        ("project", project / ".claude" / "settings.json"),
        ("local", project / ".claude" / "settings.local.json"),
        ("managed", managed if managed is not None else managed_settings_path()),
    ]
    scopes: list[Scope] = []
    seen: set[str] = set()
    for name, path in candidates:
        key = os.path.normcase(str(path.resolve())) if path.exists() else str(path)
        if key in seen:  # project root == home: do not count one file twice
            continue
        seen.add(key)
        if not path.is_file():
            continue
        sc = Scope(name, path)
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
            if not isinstance(data, dict):
                raise ValueError("top level is not an object")
            sc.data = data
        except (OSError, ValueError) as exc:
            sc.error = str(exc)
        scopes.append(sc)
    return scopes


# ---------------------------------------------------------------------- checks
def check_automode(scopes: list[Scope], rep: Report) -> None:
    for sc in scopes:
        if sc.data and sc.name in ("project", "local") and "autoMode" in sc.data:
            rep.add(
                "CCD001",
                f"autoMode in {sc.name} settings {sc.path} is ignored by Claude Code "
                "(read only from user/managed settings) -- move it to ~/.claude/settings.json",
            )


def iter_command_hooks(data: dict) -> Iterable[tuple[str, str, str]]:
    """Yield (event, matcher, command) for every command hook in a settings object."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for g in groups:
            if not isinstance(g, dict):
                continue
            matcher = str(g.get("matcher", "") or "")
            for h in g.get("hooks", []) or []:
                if (
                    isinstance(h, dict)
                    and h.get("type", "command") == "command"
                    and h.get("command")
                ):
                    yield event, matcher, str(h["command"]).strip()


def check_duplicate_hooks(scopes: list[Scope], rep: Report) -> None:
    where: dict[tuple[str, str], set[str]] = {}
    for sc in scopes:
        if not sc.data:
            continue
        for event, _m, cmd in iter_command_hooks(sc.data):
            where.setdefault((event, " ".join(cmd.split())), set()).add(sc.name)
    for (event, cmd), names in sorted(where.items()):
        if len(names) > 1:
            rep.add(
                "CCD002",
                f"{event} hook registered in {len(names)} scopes "
                f"({', '.join(sorted(names))}) runs once per scope: {cmd[:120]}",
            )


def check_mcpjson(scopes: list[Scope], cwd: Path, project: Path, rep: Report) -> None:
    enabled: set[str] = set()
    disabled: set[str] = set()
    named = False
    enable_all = False
    for sc in scopes:
        if not sc.data:
            continue
        if isinstance(sc.data.get("enabledMcpjsonServers"), list):
            named = True
            enabled.update(str(x) for x in sc.data["enabledMcpjsonServers"])
        if isinstance(sc.data.get("disabledMcpjsonServers"), list):
            disabled.update(str(x) for x in sc.data["disabledMcpjsonServers"])
        if sc.data.get("enableAllProjectMcpServers") is True:
            enable_all = True
    if not named:
        rep.notes.append("CCD003: no enabledMcpjsonServers in any scope -- nothing to compare")
        return
    mcp = find_nearest_mcp_json(cwd)
    if mcp is None:
        rep.add(
            "CCD003",
            f"enabledMcpjsonServers names {sorted(enabled)} but no .mcp.json exists "
            f"at or above {cwd}",
        )
        return
    try:
        servers = set(
            (json.loads(mcp.read_text(encoding="utf-8-sig")).get("mcpServers") or {}).keys()
        )
    except (OSError, ValueError) as exc:
        rep.add("CCD003", f"cannot parse {mcp}: {exc}", "unjudged")
        return
    rep.notes.append(f"CCD003: compared against {mcp}")
    for name in sorted(enabled - servers):
        rep.add("CCD003", f"enabledMcpjsonServers names '{name}', absent from {mcp}")
    omitted = sorted(servers - enabled - disabled)
    if omitted and not enable_all:
        rep.add(
            "CCD003",
            f"{mcp} defines {omitted} but enabledMcpjsonServers omits them "
            "(and disabledMcpjsonServers does not list them) -- they never load",
        )
    elif omitted:
        rep.notes.append(
            f"CCD003: {omitted} omitted but enableAllProjectMcpServers=true loads them"
        )


def _matcher_hits_bash(matcher: str) -> bool:
    if matcher in ("", "*"):
        return True
    try:
        return re.fullmatch(matcher, "Bash") is not None or "Bash" in matcher.split("|")
    except re.error:
        return "Bash" in matcher


def _hook_shell(command: str) -> list[str] | str:
    """How Claude Code would run a hook command: bash on POSIX, Git Bash on Windows."""
    if sys.platform != "win32":
        return ["bash", "-c", command] if shutil.which("bash") else command
    candidates = [os.environ.get("CLAUDE_CODE_GIT_BASH_PATH", "")]
    git = shutil.which("git")
    if git:  # <git>/cmd/git.exe -> <git>/bin/bash.exe (never the bare `bash`: that is WSL)
        candidates.append(str(Path(git).resolve().parent.parent / "bin" / "bash.exe"))
    candidates.append(
        str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe")
    )
    for c in candidates:
        if c and Path(c).is_file():
            return [c, "-c", command]
    return command


def time_hook(
    command: str, event: str, cwd: Path, runs: int = TIMING_RUNS
) -> tuple[float | None, str]:
    """Median wall ms of `command` fed a harmless Bash payload; (None, why) if unrunnable."""
    payload: dict[str, Any] = {
        "session_id": "ccd-doctor",
        "transcript_path": "",
        "cwd": str(cwd),
        "hook_event_name": event,
        "tool_name": "Bash",
        "tool_input": {"command": "echo ccd-doctor", "description": "doctor timing probe"},
    }
    if event == "PostToolUse":
        payload["tool_response"] = {"stdout": "ccd-doctor\n", "stderr": "", "interrupted": False}
    data = json.dumps(payload).encode()
    argv = _hook_shell(command)
    samples: list[float] = []
    # ignore_cleanup_errors: a hook can leave a child holding a file in the sandbox
    # (Windows refuses the delete); that must not turn a measurement into a traceback.
    with tempfile.TemporaryDirectory(
        prefix="ccd-hook-home-", ignore_cleanup_errors=True
    ) as sandbox:
        env = probe_env(cwd, Path(sandbox))
        for _ in range(runs):
            ms = _run_once(argv, data, cwd, env)
            if isinstance(ms, str):
                return None, ms
            samples.append(ms)
    return statistics.median(samples), ""


#: Env vars the aw* hook entrypoints honour: set, the hook returns at once.
HOOK_DISABLE_ENV = ("AWSETTINGS_HOOKS_DISABLED", "AWRELAY_HOOKS_DISABLED")


def probe_env(cwd: Path, sandbox_home: Path) -> dict[str, str]:
    """The env a timed hook runs in: the host's PATH, a throwaway HOME, and every
    known sync/relay hook switched off -- timing a hook must not push settings
    off-machine or drain a relay inbox."""
    env = dict(os.environ)
    for k in ("HOME", "USERPROFILE"):
        env[k] = str(sandbox_home)
    if sys.platform == "win32":
        env["HOMEDRIVE"], env["HOMEPATH"] = os.path.splitdrive(str(sandbox_home))
    env["CLAUDE_PROJECT_DIR"] = str(cwd)
    for k in HOOK_DISABLE_ENV:
        env[k] = "1"
    return env


def _run_once(argv: list[str] | str, data: bytes, cwd: Path, env: dict) -> float | str:
    """Wall ms of one run, or the error text when it could not start."""
    t0 = time.perf_counter()
    try:
        subprocess.run(
            argv,
            input=data,
            cwd=str(cwd),
            env=env,
            shell=isinstance(argv, str),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=HOOK_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return HOOK_TIMEOUT_S * 1000.0
    except OSError as exc:
        return str(exc)
    return (time.perf_counter() - t0) * 1000.0


def check_hook_latency(scopes: list[Scope], cwd: Path, budget_ms: int, rep: Report) -> None:
    measured: dict[tuple[str, str], tuple[float | None, str]] = {}
    for sc in scopes:
        if not sc.data:
            continue
        for event, matcher, cmd in iter_command_hooks(sc.data):
            if event not in HOT_EVENTS or not _matcher_hits_bash(matcher):
                continue
            key = (event, cmd)
            if key not in measured:
                measured[key] = time_hook(cmd, event, cwd)
            ms, why = measured[key]
            label = f"{sc.name} {event}[{matcher or '*'}]: {cmd[:100]}"
            if ms is None:
                rep.add("CCD004", f"could not run {label}: {why}", "unjudged")
            elif ms > budget_ms:
                rep.add(
                    "CCD004",
                    f"{label} median {ms:.0f} ms > budget {budget_ms} ms (paid on every Bash call)",
                )
            else:
                rep.notes.append(f"CCD004: {ms:.0f} ms ok  {label}")


def _claude_binary() -> Path | None:
    exe = shutil.which("claude")
    if not exe:
        return None
    p = Path(exe).resolve()
    candidates = [p]
    # npm shim -> the real executable shipped in the package.
    pkg = p.parent / "node_modules" / "@anthropic-ai" / "claude-code"
    candidates += [pkg / "bin" / "claude.exe", pkg / "bin" / "claude", pkg / "cli.js"]
    for c in candidates:
        if c.is_file() and c.stat().st_size > 1_000_000:
            return c
    return None


def keys_known_by_binary(keys: set[str], binary: Path | None) -> set[str]:
    """Which of `keys` the installed Claude Code declares as `<key>:` (schema literal)."""
    if not keys or binary is None:
        return set()
    import mmap

    found: set[str] = set()
    try:
        with open(binary, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            for k in keys:
                if mm.find(f"{k}:".encode()) != -1:
                    found.add(k)
    except (OSError, ValueError):
        return set()
    return found


def check_unknown_keys(
    scopes: list[Scope],
    rep: Report,
    binary: Path | None,
    allow_keys: Iterable[str] = (),
) -> None:
    """Every key outside KNOWN_SETTINGS_KEYS + the allowlists is a finding. The binary
    only adds a hint ("it appears in the installed binary") -- it never excuses a key."""
    allowed = KNOWN_SETTINGS_KEYS | INTENTIONAL_CUSTOM_KEYS | frozenset(allow_keys)
    rep.notes.append(
        "CCD005: allowlisted custom keys: "
        + ", ".join(sorted(INTENTIONAL_CUSTOM_KEYS | frozenset(allow_keys)))
    )
    unknown_by_scope: dict[str, set[str]] = {}
    for sc in scopes:
        if sc.data:
            u = set(sc.data) - allowed
            if u:
                unknown_by_scope[sc.name + " " + str(sc.path)] = u
    all_unknown = set().union(*unknown_by_scope.values()) if unknown_by_scope else set()
    in_binary = keys_known_by_binary(all_unknown - set(DEAD_KEYS), binary)
    for where, u in unknown_by_scope.items():
        for k in sorted(u):
            if k in DEAD_KEYS:
                why = f"dead key: {DEAD_KEYS[k]}"
            elif k in in_binary:
                why = (
                    "the installed binary mentions it -- if it is a new upstream key, add "
                    "it to KNOWN_SETTINGS_KEYS"
                )
            else:
                why = "Claude Code ignores it; if intentional, allowlist it with --allow-key"
            rep.add("CCD005", f"unknown settings key '{k}' in {where} ({why})")


def _voice_enabled(scopes: list[Scope]) -> bool:
    on = False
    for sc in scopes:  # later scopes override earlier ones
        v = (sc.data or {}).get("voice")
        if isinstance(v, bool):
            on = v
        elif isinstance(v, dict) and "enabled" in v:
            on = bool(v["enabled"])
    return on


def _claude_ai_logged_in(home: Path) -> bool | None:
    creds = home / ".claude" / ".credentials.json"
    try:
        if creds.is_file() and "claudeAiOauth" in json.loads(creds.read_text(encoding="utf-8")):
            return True
    except (OSError, ValueError):
        creds_unreadable = True  # fall through to ~/.claude.json
    else:
        creds_unreadable = False
    cfg = home / ".claude.json"
    try:
        if cfg.is_file():
            return bool(json.loads(cfg.read_text(encoding="utf-8")).get("oauthAccount"))
    except (OSError, ValueError):
        return None
    if creds_unreadable or sys.platform == "darwin":  # macOS keeps it in the keychain
        return None
    return False


def _capture_device_count() -> int | None:
    try:
        if sys.platform == "win32":
            # Capture endpoints are SWD\MMDEVAPI\{0.0.1.*}; render ones are {0.0.0.*}.
            ps = (
                "@(Get-PnpDevice -Class AudioEndpoint -Status OK -ErrorAction SilentlyContinue | "
                "Where-Object { $_.InstanceId -like 'SWD\\MMDEVAPI\\{0.0.1.*' }).Count"
            )
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            return int((out.stdout or "0").strip() or 0)
        if sys.platform.startswith("linux"):
            if shutil.which("arecord"):
                out = subprocess.run(
                    ["arecord", "-l"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                )
                return out.stdout.count("card ")
            pcm = Path("/proc/asound/pcm")
            return pcm.read_text().count("capture") if pcm.is_file() else None
        if sys.platform == "darwin":
            out = subprocess.run(
                ["system_profiler", "SPAudioDataType"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            return out.stdout.count("Input Channels")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


def check_voice(scopes: list[Scope], home: Path, rep: Report, probe_devices: bool = True) -> None:
    if not _voice_enabled(scopes):
        rep.notes.append("CCD006: voice not enabled -- skipped")
        return
    login = _claude_ai_logged_in(home)
    if login is False:
        rep.add(
            "CCD006",
            "voice enabled but no claude.ai login found (voice needs a claude.ai "
            "account, not an API key)",
            "warning",
        )
    elif login is None:
        rep.notes.append("CCD006: claude.ai login could not be determined")
    if not probe_devices:
        return
    n = _capture_device_count()
    if n is None:
        rep.notes.append("CCD006: audio capture devices could not be enumerated on this platform")
    elif n == 0:
        rep.add("CCD006", "voice enabled but no audio capture device is present", "warning")
    else:
        rep.notes.append(f"CCD006: {n} audio capture device(s) present")


# ------------------------------------------------------------------------ run
def run_doctor(
    home: Path,
    cwd: Path,
    *,
    budget_ms: int = DEFAULT_BUDGET_MS,
    timing: bool = True,
    managed: Path | None = None,
    binary: Path | None = None,
    use_binary: bool = True,
    probe_devices: bool = True,
    allow_keys: Iterable[str] = (),
) -> Report:
    rep = Report()
    project = find_project_root(cwd)
    scopes = load_scopes(home, project, managed)
    rep.notes.append(
        "scopes: " + ", ".join(f"{s.name}={s.path}" for s in scopes)
        if scopes
        else "scopes: none found"
    )
    for sc in scopes:
        if sc.error:
            rep.add("CCD000", f"{sc.name} settings {sc.path} unreadable: {sc.error}", "unjudged")
    if not scopes:
        rep.add("CCD000", f"no Claude Code settings file under {home} or {project}", "unjudged")
        return rep
    check_automode(scopes, rep)
    check_duplicate_hooks(scopes, rep)
    check_mcpjson(scopes, cwd, project, rep)
    if timing:
        check_hook_latency(scopes, project, budget_ms, rep)
    else:
        rep.notes.append("CCD004: skipped (--no-timing)")
    check_unknown_keys(
        scopes,
        rep,
        binary if binary is not None else (_claude_binary() if use_binary else None),
        allow_keys,
    )
    check_voice(scopes, home, rep, probe_devices)
    return rep


def print_report(rep: Report, as_json: bool = False, verbose: bool = False) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "exit": rep.exit_code,
                    "findings": [f.__dict__ for f in rep.findings],
                    "notes": rep.notes,
                },
                indent=2,
            )
        )
        return
    for f in rep.findings:
        tag = {"violation": "FAIL", "warning": "WARN", "unjudged": "DEAD"}[f.severity]
        print(f"{tag} {f.code} {f.message}")
    if verbose or not rep.findings:
        for n in rep.notes:
            print(f"  .. {n}")
    word = {0: "clean", 1: "violation(s)", 2: "could not judge"}[rep.exit_code]
    print(f"claude doctor: {word} ({len(rep.findings)} finding(s))")


# ------------------------------------------------------------------ self-test
def _write(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def self_test() -> int:
    py = f'"{sys.executable}"' if " " in sys.executable else sys.executable
    py = py.replace("\\", "/")
    slow = f'{py} -c "import time; time.sleep(0.6)"'
    fast = f'{py} -c "pass"'
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        home, proj = root / "home", root / "proj"
        (proj / ".git").mkdir(parents=True)
        nomanaged = root / "no-managed.json"
        # --- dirty fixture: every CCD must fire
        _write(
            home / ".claude" / "settings.json",
            {
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": slow}]}
                    ]
                },
                "voice": {"enabled": True},
                "aitherLane": "x",
            },
        )
        _write(
            proj / ".claude" / "settings.json",
            {
                "autoMode": {"allow": []},
                "notARealSettingKeyZz": 1,
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": slow}]}
                    ]
                },
            },
        )
        _write(proj / ".claude" / "settings.local.json", {"enabledMcpjsonServers": ["ghost"]})
        _write(proj / ".mcp.json", {"mcpServers": {"real": {"command": "x"}}})
        rep = run_doctor(
            home, proj, budget_ms=300, managed=nomanaged, use_binary=False, probe_devices=False
        )
        codes = {f.code for f in rep.findings}
        wanted = ["CCD001", "CCD002", "CCD003", "CCD004", "CCD005"]
        if sys.platform != "darwin":  # macOS keeps the login in the keychain
            wanted.append("CCD006")
        for want in wanted:
            if want not in codes:
                failures.append(f"dirty fixture: {want} did not fire")
        if sum(1 for f in rep.findings if f.code == "CCD003") != 2:
            failures.append("dirty fixture: CCD003 must report both the ghost and the omission")
        if any(f.code == "CCD005" and "aitherLane" in f.message for f in rep.findings):
            failures.append("allowlisted custom key reported by CCD005")
        if rep.exit_code != 1:
            failures.append(f"dirty fixture exit {rep.exit_code}, want 1")
        # --- clean fixture: nothing but the warn-only voice check may appear
        _write(
            home / ".claude" / "settings.json",
            {
                "autoMode": {"allow": []},
                "aitherLane": "x",
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": fast}]}
                    ]
                },
            },
        )
        _write(proj / ".claude" / "settings.json", {"hooks": {}})
        _write(proj / ".claude" / "settings.local.json", {"enabledMcpjsonServers": ["real"]})
        rep = run_doctor(
            home, proj, budget_ms=5000, managed=nomanaged, use_binary=False, probe_devices=False
        )
        if rep.exit_code != 0:
            failures.append(
                "clean fixture not clean: "
                + "; ".join(f"{f.code} {f.message}" for f in rep.findings)
            )
        # --- a dead key is reported even when the installed binary mentions it
        fake_bin = root / "fake-claude-binary"
        fake_bin.write_bytes(b"x" * 64 + b"mcpServers:{}apiKey:''" + b"x" * 64)
        _write(home / ".claude" / "settings.json", {"mcpServers": {}, "aitherLane": "x"})
        rep = run_doctor(
            home, proj, timing=False, managed=nomanaged, binary=fake_bin, probe_devices=False
        )
        if not any(f.code == "CCD005" and "mcpServers" in f.message for f in rep.findings):
            failures.append("dead key excused because the binary mentions it")
        _write(home / ".claude" / "settings.json", {"aitherLane": "x"})
        # --- unreadable settings: could-not-judge, never clean
        (proj / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
        rep = run_doctor(
            home, proj, timing=False, managed=nomanaged, use_binary=False, probe_devices=False
        )
        if rep.exit_code != 2:
            failures.append(f"unparsable settings exit {rep.exit_code}, want 2")
    for f in failures:
        print(f"SELF-TEST FAIL: {f}")
    print(
        "claude doctor self-test: "
        + ("PASS (8 arms)" if not failures else f"FAIL ({len(failures)})")
    )
    return 0 if not failures else 1


# ----------------------------------------------------------------------- setup
def plugin_source_dir() -> Path:
    return Path(__file__).resolve().parent / "harnesses" / "claude_mod"


def _merge_user_settings(home: Path, src: Path) -> dict:
    path = home / ".claude" / "settings.json"
    data: dict = {}
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    data.setdefault("extraKnownMarketplaces", {})[MARKETPLACE] = {
        "source": {"source": "directory", "path": str(src)}
    }
    data.setdefault("enabledPlugins", {})[PLUGIN_ID] = True
    return data


def atomic_write_json(path: Path, obj: Any) -> Path | None:
    """Write `obj` to `path` atomically (temp file in the same dir + os.replace), after
    copying any existing file to `<name>.bak-adk-setup-<UTC timestamp>`. Returns the
    backup path (None when there was nothing to back up). A crash mid-write leaves the
    old file intact; a second run never overwrites the first run's backup."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if path.is_file():
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup = path.with_name(f"{path.name}.bak-adk-setup-{stamp}")
        n = 1
        while backup.exists():
            backup = path.with_name(f"{path.name}.bak-adk-setup-{stamp}-{n}")
            n += 1
        shutil.copy2(path, backup)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return backup


def awsettings_preset_support(aws: str, preset: str = AWSETTINGS_PRESET) -> str:
    """'' when `aws` has the `preset` verb AND knows `preset`; otherwise why not.
    Probed with `awsettings preset list`, which is read-only."""
    try:
        p = subprocess.run(
            [aws, "preset", "list"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"could not run `awsettings preset list`: {exc}"
    out = p.stdout + p.stderr
    if p.returncode != 0:
        return (
            "the installed awsettings has no `preset` verb (upgrade awsettings to get it)"
            if "invalid choice" in out
            else f"`awsettings preset list` exited {p.returncode}: {out.strip()[-200:]}"
        )
    if not any(line.split(" ", 1)[0] == preset for line in p.stdout.splitlines()):
        return f"the installed awsettings does not know the preset {preset!r}"
    return ""


def run_setup(home: Path, dry_run: bool = False, use_cli: bool = True) -> int:
    src = plugin_source_dir()
    actions: list[str] = []
    rc = 0
    if not (src / ".claude-plugin" / "marketplace.json").is_file():
        print(f"setup: plugin source missing at {src} (broken adk install)")
        return 2
    claude = shutil.which("claude") if use_cli else None
    if claude:
        steps = [
            [claude, "plugin", "marketplace", "add", str(src)],
            [claude, "plugin", "install", PLUGIN_ID],
        ]
        for argv in steps:
            shown = "claude " + " ".join(argv[1:])
            if dry_run:
                actions.append(f"would run: {shown}")
                continue
            p = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            out = (p.stdout + p.stderr).strip()
            if p.returncode != 0 and "already" not in out.lower():
                actions.append(f"FAILED ({p.returncode}): {shown}: {out[-300:]}")
                rc = 1
            else:
                actions.append(
                    f"ran: {shown}" + (" (already present)" if "already" in out.lower() else "")
                )
    else:
        path = home / ".claude" / "settings.json"
        if dry_run:
            actions.append(
                f"would merge extraKnownMarketplaces.{MARKETPLACE} + enabledPlugins.{PLUGIN_ID} "
                f"into {path} (claude CLI not on PATH)"
            )
        else:
            try:
                data = _merge_user_settings(home, src)
            except ValueError as exc:
                print(f"setup: {path} is not valid JSON ({exc}); refusing to rewrite it")
                return 2
            backup = atomic_write_json(path, data)
            actions.append(
                f"merged {PLUGIN_ID} into {path}"
                + (f" (backup {backup.name})" if backup else "")
            )
    aws = shutil.which("awsettings")
    why_not = awsettings_preset_support(aws) if aws else ""
    if aws and why_not:
        actions.append(f"skipped: awsettings preset apply {AWSETTINGS_PRESET} -- {why_not}")
    elif aws:
        argv = [aws, "preset", "apply", AWSETTINGS_PRESET]
        if dry_run:
            actions.append(f"would run: awsettings preset apply {AWSETTINGS_PRESET}")
        else:
            p = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            if p.returncode != 0:
                actions.append(
                    f"awsettings preset apply {AWSETTINGS_PRESET} exited {p.returncode}: "
                    f"{(p.stdout + p.stderr).strip()[-300:]}"
                )
                rc = rc or 1
            else:
                actions.append(f"ran: awsettings preset apply {AWSETTINGS_PRESET}")
    else:
        actions.append(
            "skipped: awsettings not installed (pip install awsettings to sync the preset)"
        )
    for a in actions:
        print(("[dry-run] " if dry_run else "") + a)
    if not dry_run:
        print("next: adk claude doctor")
    return rc


# ------------------------------------------------------------------------ CLI
def add_arguments_setup(p) -> None:
    p.add_argument("--dry-run", action="store_true", help="Print the plan; change nothing")
    p.add_argument(
        "--no-cli", action="store_true", help="Write settings.json directly, never call `claude`"
    )


def add_arguments_doctor(p) -> None:
    p.add_argument("--self-test", action="store_true", help="Prove every check can still fail")
    p.add_argument("--no-timing", action="store_true", help="Skip CCD004 hook timing")
    p.add_argument("--budget-ms", type=int, default=DEFAULT_BUDGET_MS, help="CCD004 median budget")
    p.add_argument("--cwd", default="", help="Project directory to judge (default: current)")
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    p.add_argument("-v", "--verbose", action="store_true", help="Also print notes")
    p.add_argument(
        "--allow-key",
        action="append",
        default=[],
        metavar="KEY",
        help="CCD005: a custom settings key that is intentional (repeatable)",
    )


def cmd_setup(args) -> int:
    return run_setup(
        Path.home(),
        dry_run=bool(getattr(args, "dry_run", False)),
        use_cli=not getattr(args, "no_cli", False),
    )


def cmd_doctor(args) -> int:
    if getattr(args, "self_test", False):
        return self_test()
    cwd = Path(getattr(args, "cwd", "") or os.getcwd())
    rep = run_doctor(
        Path.home(),
        cwd,
        budget_ms=int(getattr(args, "budget_ms", DEFAULT_BUDGET_MS)),
        timing=not getattr(args, "no_timing", False),
        allow_keys=list(getattr(args, "allow_key", None) or []),
    )
    print_report(
        rep,
        as_json=bool(getattr(args, "json", False)),
        verbose=bool(getattr(args, "verbose", False)),
    )
    return rep.exit_code


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="claude-code-doctor", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    add_arguments_setup(sub.add_parser("setup"))
    add_arguments_doctor(sub.add_parser("doctor"))
    args = ap.parse_args(argv)
    if args.cmd == "setup":
        return cmd_setup(args)
    if args.cmd == "doctor":
        return cmd_doctor(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
