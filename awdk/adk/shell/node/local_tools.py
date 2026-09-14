"""
Local MCP tools for standalone mode.

Provides basic filesystem, git, and shell tools that work without any
cloud connection. Each function returns a string result suitable for
wrapping in MCP TextContent.
"""

import asyncio
import fnmatch
import os
import re
import subprocess
from pathlib import Path
from typing import List, Optional


# ── Safety ────────────────────────────────────────────────────────────────

_DANGEROUS_PATTERNS = [
    r"\brm\s+(-\w*\s+)*-rf?\s+/\s*$",       # rm -rf /
    r"\brm\s+(-\w*\s+)*-rf?\s+/\*",          # rm -rf /*
    r"\bmkfs\b",                               # format filesystem
    r"\bdd\s+.*\bof=/dev/",                   # dd to raw device
    r":\(\)\s*\{\s*:\|\s*:\s*&\s*\}\s*;",    # fork bomb
    r"\bchmod\s+(-\w+\s+)*777\s+/\s*$",      # chmod 777 /
    r"\bshutdown\b",                           # shutdown
    r"\breboot\b",                             # reboot
    r"\bhalt\b",                               # halt
    r"\binit\s+0\b",                           # init 0
    r"\bformat\s+[a-zA-Z]:",                  # Windows format
    r"\bdel\s+/[sS]\s+/[qQ]\s+[A-Z]:\\",     # del /s /q C:\
]
_DANGEROUS_RE = [re.compile(p) for p in _DANGEROUS_PATTERNS]

# Maximum file size to read (10 MB)
_MAX_READ_SIZE = 10 * 1024 * 1024
# Maximum command output (512 KB)
_MAX_CMD_OUTPUT = 512 * 1024
# Command timeout (30 seconds)
_CMD_TIMEOUT = 30


def _check_command_safety(command: str) -> Optional[str]:
    """Return an error message if the command is dangerous, else None."""
    for pattern in _DANGEROUS_RE:
        if pattern.search(command):
            return f"Blocked: command matches dangerous pattern ({pattern.pattern})"
    return None


def _resolve_path(path: str) -> Path:
    """Resolve a path, expanding ~ and making absolute."""
    return Path(path).expanduser().resolve()


# ── Tool implementations ─────────────────────────────────────────────────

def read_file(path: str, offset: int = 0, limit: int = 0) -> str:
    """Read a file and return its contents.

    Args:
        path: Absolute or relative file path.
        offset: Line number to start from (1-based, 0 = start).
        limit: Maximum number of lines to return (0 = all).

    Returns:
        File contents as a string with line numbers.
    """
    resolved = _resolve_path(path)
    if not resolved.is_file():
        return f"Error: not a file: {resolved}"

    size = resolved.stat().st_size
    if size > _MAX_READ_SIZE:
        return f"Error: file too large ({size:,} bytes, max {_MAX_READ_SIZE:,})"

    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except PermissionError:
        return f"Error: permission denied: {resolved}"

    lines = text.splitlines(keepends=True)
    start = max(0, offset - 1) if offset > 0 else 0
    end = start + limit if limit > 0 else len(lines)
    selected = lines[start:end]

    numbered = []
    for i, line in enumerate(selected, start=start + 1):
        numbered.append(f"{i:>6}\t{line.rstrip()}")
    return "\n".join(numbered)


def write_file(path: str, content: str) -> str:
    """Write content to a file, creating parent directories as needed.

    Args:
        path: Absolute or relative file path.
        content: Content to write.

    Returns:
        Confirmation message.
    """
    resolved = _resolve_path(path)
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return f"Written {len(content)} bytes to {resolved}"
    except PermissionError:
        return f"Error: permission denied: {resolved}"


def list_dir(path: str = ".") -> str:
    """List directory contents.

    Args:
        path: Directory path (default: current directory).

    Returns:
        Directory listing with type indicators.
    """
    resolved = _resolve_path(path)
    if not resolved.is_dir():
        return f"Error: not a directory: {resolved}"

    try:
        entries = sorted(resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return f"Error: permission denied: {resolved}"

    lines = [f"Directory: {resolved}", ""]
    for entry in entries:
        if entry.is_dir():
            lines.append(f"  [dir]  {entry.name}/")
        elif entry.is_symlink():
            lines.append(f"  [lnk]  {entry.name} -> {entry.resolve()}")
        else:
            size = entry.stat().st_size
            lines.append(f"  [file] {entry.name}  ({size:,} bytes)")

    if len(entries) == 0:
        lines.append("  (empty)")

    return "\n".join(lines)


def find_files(pattern: str, path: str = ".") -> str:
    """Find files matching a glob pattern.

    Args:
        pattern: Glob pattern (e.g. '*.py', '**/*.ts').
        path: Root directory to search from.

    Returns:
        Newline-separated list of matching file paths.
    """
    resolved = _resolve_path(path)
    if not resolved.is_dir():
        return f"Error: not a directory: {resolved}"

    matches: List[str] = []
    try:
        for match in resolved.rglob(pattern) if "**" in pattern else resolved.glob(pattern):
            if match.is_file():
                matches.append(str(match.relative_to(resolved)))
                if len(matches) >= 500:
                    matches.append(f"... (truncated at 500 results)")
                    break
    except PermissionError:
        return f"Error: permission denied: {resolved}"

    if not matches:
        return f"No files matching '{pattern}' in {resolved}"
    return "\n".join(matches)


def search_content(pattern: str, path: str = ".", glob: str = "") -> str:
    """Search file contents for a regex pattern.

    Args:
        pattern: Regular expression pattern to search for.
        path: Root directory to search from.
        glob: Optional glob filter for filenames (e.g. '*.py').

    Returns:
        Matching lines with file paths and line numbers.
    """
    resolved = _resolve_path(path)
    if not resolved.is_dir():
        return f"Error: not a directory: {resolved}"

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Error: invalid regex: {e}"

    results: List[str] = []
    count = 0
    max_results = 200

    for root, _dirs, files in os.walk(resolved):
        # Skip hidden and common noise directories
        root_path = Path(root)
        if any(part.startswith(".") or part in ("node_modules", "__pycache__", ".git")
               for part in root_path.relative_to(resolved).parts):
            continue

        for fname in files:
            if glob and not fnmatch.fnmatch(fname, glob):
                continue
            fpath = root_path / fname
            try:
                text = fpath.read_text(encoding="utf-8", errors="ignore")
            except (PermissionError, OSError):
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    rel = fpath.relative_to(resolved)
                    results.append(f"{rel}:{lineno}: {line.rstrip()}")
                    count += 1
                    if count >= max_results:
                        results.append(f"... (truncated at {max_results} matches)")
                        return "\n".join(results)

    if not results:
        return f"No matches for '{pattern}' in {resolved}"
    return "\n".join(results)


def run_command(command: str, cwd: str = ".") -> str:
    """Run a shell command and return its output.

    Args:
        command: Shell command to execute.
        cwd: Working directory (default: current directory).

    Returns:
        Combined stdout and stderr.
    """
    safety_error = _check_command_safety(command)
    if safety_error:
        return safety_error

    resolved_cwd = _resolve_path(cwd)
    if not resolved_cwd.is_dir():
        return f"Error: not a directory: {resolved_cwd}"

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=str(resolved_cwd),
            timeout=_CMD_TIMEOUT,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        output_parts = []
        if result.stdout:
            output_parts.append(result.stdout[:_MAX_CMD_OUTPUT])
        if result.stderr:
            output_parts.append(f"[stderr]\n{result.stderr[:_MAX_CMD_OUTPUT]}")
        if result.returncode != 0:
            output_parts.append(f"\n[exit code: {result.returncode}]")
        return "\n".join(output_parts) if output_parts else "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {_CMD_TIMEOUT}s"
    except Exception as e:
        return f"Error: {e}"


def git_status(cwd: str = ".") -> str:
    """Show git status of the working directory.

    Args:
        cwd: Repository directory.

    Returns:
        git status output.
    """
    return run_command("git status --short --branch", cwd=cwd)


def git_diff(ref: str = "", cwd: str = ".") -> str:
    """Show git diff.

    Args:
        ref: Optional ref to diff against (e.g. 'HEAD~1', 'main').
        cwd: Repository directory.

    Returns:
        git diff output.
    """
    cmd = f"git diff {ref}" if ref else "git diff"
    return run_command(cmd, cwd=cwd)


def git_log(count: int = 10, cwd: str = ".") -> str:
    """Show recent git commits.

    Args:
        count: Number of commits to show (default: 10).
        cwd: Repository directory.

    Returns:
        git log output.
    """
    n = min(max(1, count), 100)
    return run_command(f"git log --oneline --no-decorate -n {n}", cwd=cwd)


# ── Tool registry ────────────────────────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "read_file",
        "description": "Read a file and return its contents with line numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read"},
                "offset": {"type": "integer", "description": "Start line (1-based, 0=start)", "default": 0},
                "limit": {"type": "integer", "description": "Max lines to return (0=all)", "default": 0},
            },
            "required": ["path"],
        },
        "fn": read_file,
    },
    {
        "name": "write_file",
        "description": "Write content to a file, creating parent directories as needed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to write"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
        "fn": write_file,
    },
    {
        "name": "list_dir",
        "description": "List directory contents with type indicators and sizes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path", "default": "."},
            },
        },
        "fn": list_dir,
    },
    {
        "name": "find_files",
        "description": "Find files matching a glob pattern recursively.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern (e.g. '*.py', '**/*.ts')"},
                "path": {"type": "string", "description": "Root directory", "default": "."},
            },
            "required": ["pattern"],
        },
        "fn": find_files,
    },
    {
        "name": "search_content",
        "description": "Search file contents for a regex pattern. Returns matching lines with paths and line numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path": {"type": "string", "description": "Root directory", "default": "."},
                "glob": {"type": "string", "description": "Filename filter (e.g. '*.py')", "default": ""},
            },
            "required": ["pattern"],
        },
        "fn": search_content,
    },
    {
        "name": "run_command",
        "description": "Run a shell command and return output. Dangerous commands (rm -rf /, format, etc.) are blocked.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "cwd": {"type": "string", "description": "Working directory", "default": "."},
            },
            "required": ["command"],
        },
        "fn": run_command,
    },
    {
        "name": "git_status",
        "description": "Show git status (short format with branch).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "description": "Repository directory", "default": "."},
            },
        },
        "fn": git_status,
    },
    {
        "name": "git_diff",
        "description": "Show git diff, optionally against a specific ref.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Ref to diff against (e.g. 'HEAD~1', 'main')", "default": ""},
                "cwd": {"type": "string", "description": "Repository directory", "default": "."},
            },
        },
        "fn": git_diff,
    },
    {
        "name": "git_log",
        "description": "Show recent git commits (oneline format).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "Number of commits (max 100)", "default": 10},
                "cwd": {"type": "string", "description": "Repository directory", "default": "."},
            },
        },
        "fn": git_log,
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Cross-cutting invariants are applied HERE, at registration, so every consumer
# inherits them — adk/node/server.py, adk/cli.py, and anything written later.
# Guarding a dispatcher would guard one caller; this guards all of them.
# Soft by construction: a guard that cannot run is skipped, so an ADK shipped to
# a machine with no awgit and no repo behaves exactly as before.
# See adk/tool_guards.py for why this is a path property and not a rule agents
# are asked to remember.
# ─────────────────────────────────────────────────────────────────────────────
try:
    from adk.tool_guards import guard_registry as _guard_registry

    TOOL_DEFINITIONS = _guard_registry(TOOL_DEFINITIONS)
except Exception:  # pragma: no cover - never break tool loading on our own helper
    pass
