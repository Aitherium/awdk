"""
AitherShell — Shell Integration
================================
Clipboard, last command, aliases, file/git context.
All sync, no AitherOS deps, subprocess-based.
"""

import os
import sys
import subprocess
import json
from pathlib import Path
from typing import Optional, Dict, List

ALIASES_FILE = Path.home() / ".aither" / "aliases.yaml"


# ─── Clipboard ──────────────────────────────────────────────────────

def copy_to_clipboard(text: str) -> bool:
    """Copy text to system clipboard."""
    try:
        if sys.platform == "win32":
            p = subprocess.Popen(["clip.exe"], stdin=subprocess.PIPE)
            p.communicate(text.encode("utf-16le"))
        elif sys.platform == "darwin":
            p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            p.communicate(text.encode("utf-8"))
        else:
            for cmd in [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]:
                try:
                    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
                    p.communicate(text.encode("utf-8"))
                    return True
                except FileNotFoundError:
                    continue
            return False
        return True
    except Exception:
        return False


def paste_from_clipboard() -> str:
    """Read text from system clipboard."""
    try:
        if sys.platform == "win32":
            r = subprocess.run(["powershell", "-command", "Get-Clipboard"], capture_output=True, text=True, timeout=3)
            return r.stdout.strip()
        elif sys.platform == "darwin":
            r = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=3)
            return r.stdout.strip()
        else:
            for cmd in [["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]]:
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
                    return r.stdout.strip()
                except FileNotFoundError:
                    continue
    except Exception:
        pass
    return ""


# ─── Last Command ───────────────────────────────────────────────────

def get_last_command() -> str:
    """Get last terminal command from shell history."""
    try:
        # PowerShell (Windows — check first)
        ps_hist = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "PowerShell" / "PSReadLine" / "ConsoleHost_history.txt"
        if ps_hist.exists():
            lines = ps_hist.read_text(encoding="utf-8", errors="replace").strip().split("\n")
            return lines[-1] if lines else ""
        # bash
        hist = Path.home() / ".bash_history"
        if hist.exists():
            lines = hist.read_text(encoding="utf-8", errors="replace").strip().split("\n")
            return lines[-1] if lines else ""
        # zsh
        hist = Path.home() / ".zsh_history"
        if hist.exists():
            lines = hist.read_text(encoding="utf-8", errors="replace").strip().split("\n")
            return lines[-1].split(";", 1)[-1] if lines else ""
    except Exception:
        pass
    return ""


# ─── Aliases ────────────────────────────────────────────────────────

def load_aliases() -> Dict[str, str]:
    """Load aliases from ~/.aither/aliases.yaml."""
    if not ALIASES_FILE.exists():
        return {}
    try:
        import yaml
        with open(ALIASES_FILE, "r") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def save_alias(name: str, value: str):
    """Save an alias."""
    aliases = load_aliases()
    aliases[name] = value
    ALIASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        import yaml
        with open(ALIASES_FILE, "w") as f:
            yaml.dump(aliases, f)
    except ImportError:
        with open(ALIASES_FILE, "w") as f:
            for k, v in aliases.items():
                f.write(f"{k}: {v}\n")


# ─── File Context ───────────────────────────────────────────────────

def read_file_for_context(path: str, max_chars: int = 16000) -> str:
    """Read a file and return truncated content for prompt injection."""
    try:
        p = Path(path).expanduser()
        if not p.exists():
            return ""
        text = p.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n... (truncated, {len(text)} total chars)"
        return text
    except Exception:
        return ""


# ─── Git Context ────────────────────────────────────────────────────

def _git(*args, cwd=None) -> str:
    """Run a git command and return stdout."""
    try:
        r = subprocess.run(
            ["git"] + list(args),
            capture_output=True, text=True, timeout=10,
            cwd=cwd or os.getcwd(),
        )
        return r.stdout.strip()
    except Exception:
        return ""


def get_git_context() -> Dict[str, str]:
    """Get current git context: branch, status, diffs, recent commits."""
    return {
        "branch": _git("branch", "--show-current"),
        "status": _git("status", "--short"),
        "staged_diff": _git("diff", "--cached"),
        "unstaged_diff": _git("diff"),
        "recent_commits": _git("log", "--oneline", "-10"),
    }


def generate_commit_context() -> Dict[str, str]:
    """Get context for generating a commit message."""
    return {
        "staged_diff": _git("diff", "--cached"),
        "staged_files": _git("diff", "--cached", "--name-only"),
        "branch": _git("branch", "--show-current"),
    }


def generate_pr_context(base: str = "main") -> Dict[str, str]:
    """Get context for generating a PR title + body."""
    branch = _git("branch", "--show-current")
    return {
        "branch": branch,
        "base": base,
        "commits": _git("log", f"{base}..HEAD", "--oneline"),
        "diff": _git("diff", f"{base}...HEAD"),
        "files_changed": _git("diff", f"{base}...HEAD", "--name-only"),
    }


# ─── Web Search ─────────────────────────────────────────────────────

async def web_search(query: str, limit: int = 5) -> List[Dict[str, str]]:
    """Search the web via DuckDuckGo instant answer API."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": 1},
            )
            if r.status_code == 200:
                data = r.json()
                results = []
                # Abstract
                if data.get("Abstract"):
                    results.append({
                        "title": data.get("Heading", ""),
                        "snippet": data["Abstract"],
                        "url": data.get("AbstractURL", ""),
                    })
                # Related topics
                for topic in data.get("RelatedTopics", [])[:limit]:
                    if isinstance(topic, dict) and topic.get("Text"):
                        results.append({
                            "title": topic.get("Text", "")[:80],
                            "snippet": topic.get("Text", ""),
                            "url": topic.get("FirstURL", ""),
                        })
                return results
    except Exception:
        pass
    return []
