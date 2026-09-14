"""
AitherZero Plugin for AitherShell
==================================

Exposes AitherZero's 170+ PowerShell 7 automation scripts as slash commands.

Usage:
    /zero                      — List all script categories
    /zero list <category>      — List scripts in a category
    /zero search <keyword>     — Search scripts by name
    /zero run <number|name>    — Run a script by number or partial name
    /zero info <number|name>   — Show script synopsis
    /zero path                 — Show AitherZero scripts root

Aliases: /az, /pwsh
"""

import os
import subprocess
import re
from pathlib import Path
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field

from adk.shell.plugins import SlashCommand


def _find_aitherzero_root() -> Optional[Path]:
    """Locate the AitherZero automation-scripts directory."""
    candidates = []

    # Check env var
    root = os.environ.get("AITHERZERO_ROOT")
    if root:
        candidates.append(Path(root) / "library" / "automation-scripts")
        candidates.append(Path(root) / "AitherZero" / "library" / "automation-scripts")

    # Check common locations relative to AITHEROS_ROOT
    aitheros = os.environ.get("AITHEROS_ROOT")
    if aitheros:
        candidates.append(Path(aitheros) / ".." / "AitherZero" / "library" / "automation-scripts")

    # Check relative to CWD
    cwd = Path.cwd()
    candidates.extend([
        cwd / "AitherZero" / "library" / "automation-scripts",
        cwd / ".." / "AitherZero" / "library" / "automation-scripts",
    ])

    # Home- and drive-root checkouts, DISCOVERED rather than hardcoded. This used to
    # name an absolute drive path explicitly — one developer's drive layout, shipped in a
    # public package.
    from adk.shell._repo_roots import candidate_repo_roots

    home = Path.home()
    candidates.append(home / "AitherZero" / "library" / "automation-scripts")
    for root in candidate_repo_roots(include_cwd=False):
        candidates.append(root / "AitherZero" / "library" / "automation-scripts")

    for c in candidates:
        resolved = c.resolve()
        if resolved.is_dir():
            return resolved
    return None


def _find_pwsh() -> str:
    """Find PowerShell 7 binary."""
    import shutil
    # Prefer pwsh (PowerShell 7) over powershell.exe (Windows PowerShell 5.1)
    pwsh = shutil.which("pwsh")
    if pwsh:
        return pwsh
    ps = shutil.which("powershell")
    if ps:
        return ps
    return "pwsh"


@dataclass
class ScriptInfo:
    number: str
    name: str
    filename: str
    category: str
    category_dir: str
    full_path: str


def _scan_scripts(root: Path) -> List[ScriptInfo]:
    """Scan all .ps1 scripts and parse their number/name."""
    scripts = []
    for category_dir in sorted(root.iterdir()):
        if not category_dir.is_dir() or category_dir.name.startswith("_"):
            continue
        category = category_dir.name
        for f in sorted(category_dir.glob("*.ps1")):
            # Parse "0123_Script-Name.ps1" pattern
            match = re.match(r"^(\d+)[_-](.+)\.ps1$", f.name, re.IGNORECASE)
            if match:
                scripts.append(ScriptInfo(
                    number=match.group(1),
                    name=match.group(2),
                    filename=f.name,
                    category=category,
                    category_dir=category_dir.name,
                    full_path=str(f),
                ))
    # Also check root-level scripts
    for f in sorted(root.glob("*.ps1")):
        match = re.match(r"^(\d+)[_-](.+)\.ps1$", f.name, re.IGNORECASE)
        if match:
            scripts.append(ScriptInfo(
                number=match.group(1),
                name=match.group(2),
                filename=f.name,
                category="(root)",
                category_dir=".",
                full_path=str(f),
            ))
    return scripts


def _find_script(scripts: List[ScriptInfo], query: str) -> Optional[ScriptInfo]:
    """Find a script by number or partial name match."""
    q = query.lower().strip()
    # Exact number match
    for s in scripts:
        if s.number == q:
            return s
    # Partial name match
    for s in scripts:
        if q in s.name.lower() or q in s.filename.lower():
            return s
    return None


class AitherZeroPlugin(SlashCommand):
    name = "zero"
    description = "Run AitherZero PowerShell 7 automation scripts"
    aliases = ["az", "pwsh"]

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='zero',
            description='Run AitherZero PowerShell 7 automation scripts',
            aliases=['az', 'pwsh'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        root = _find_aitherzero_root()
        if not root:
            return "Error: AitherZero scripts not found. Set AITHERZERO_ROOT or AITHEROS_ROOT."

        if not args:
            return self._list_categories(root)

        subcmd = args[0].lower()
        rest = args[1:]

        if subcmd == "list":
            return self._list_scripts(root, " ".join(rest) if rest else None)
        elif subcmd == "search":
            return self._search_scripts(root, " ".join(rest))
        elif subcmd == "run":
            return await self._run_script(root, " ".join(rest), extra_args=rest[1:] if len(rest) > 1 else [])
        elif subcmd == "info":
            return self._script_info(root, " ".join(rest))
        elif subcmd == "path":
            return str(root)
        elif subcmd == "categories":
            return self._list_categories(root)
        else:
            # Treat as a direct run attempt
            return await self._run_script(root, subcmd, extra_args=rest)

    def _list_categories(self, root: Path) -> str:
        scripts = _scan_scripts(root)
        categories: Dict[str, int] = {}
        for s in scripts:
            categories[s.category] = categories.get(s.category, 0) + 1

        lines = ["AitherZero Script Categories", "=" * 40]
        for cat, count in sorted(categories.items()):
            lines.append(f"  {cat:<30} {count:>3} scripts")
        lines.append(f"\n  Total: {len(scripts)} scripts")
        lines.append("\nUsage: /zero list <category>  |  /zero run <number>  |  /zero search <keyword>")
        return "\n".join(lines)

    def _list_scripts(self, root: Path, category: Optional[str]) -> str:
        scripts = _scan_scripts(root)
        if category:
            cat = category.lower()
            scripts = [s for s in scripts if cat in s.category.lower() or cat in s.category_dir.lower()]
            if not scripts:
                return f"No scripts found in category matching '{category}'"

        lines = []
        current_cat = None
        for s in scripts:
            if s.category != current_cat:
                current_cat = s.category
                lines.append(f"\n[{current_cat}]")
            lines.append(f"  {s.number}  {s.name}")
        return "\n".join(lines) if lines else "No scripts found."

    def _search_scripts(self, root: Path, query: str) -> str:
        if not query:
            return "Usage: /zero search <keyword>"
        scripts = _scan_scripts(root)
        q = query.lower()
        matches = [s for s in scripts if q in s.name.lower() or q in s.filename.lower()]
        if not matches:
            return f"No scripts matching '{query}'"
        lines = [f"Scripts matching '{query}':"]
        for s in matches:
            lines.append(f"  {s.number}  {s.name:<40} [{s.category}]")
        return "\n".join(lines)

    def _script_info(self, root: Path, query: str) -> str:
        if not query:
            return "Usage: /zero info <number|name>"
        scripts = _scan_scripts(root)
        script = _find_script(scripts, query)
        if not script:
            return f"Script not found: {query}"

        # Read first comment block for synopsis
        synopsis = ""
        try:
            with open(script.full_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("#") or line.startswith("<#"):
                        synopsis += line.lstrip("#").lstrip("<#").strip() + "\n"
                    elif synopsis and not line.startswith("#"):
                        break
        except Exception:
            pass

        return (
            f"Script: {script.filename}\n"
            f"Number: {script.number}\n"
            f"Category: {script.category}\n"
            f"Path: {script.full_path}\n"
            f"\n{synopsis.strip() or '(no synopsis)'}"
        )

    async def _run_script(self, root: Path, query: str, extra_args: List[str] = []) -> str:
        if not query:
            return "Usage: /zero run <number|name> [args...]"
        scripts = _scan_scripts(root)
        script = _find_script(scripts, query)
        if not script:
            return f"Script not found: {query}"

        pwsh = _find_pwsh()
        cmd = [pwsh, "-NoProfile", "-File", script.full_path] + extra_args

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
                cwd=str(root.parent.parent),  # AitherZero root
            )
            output = result.stdout
            if result.stderr:
                output += f"\n[STDERR]\n{result.stderr}"
            if result.returncode != 0:
                output += f"\n[Exit code: {result.returncode}]"
            return output or "(no output)"
        except subprocess.TimeoutExpired:
            return f"Script timed out after 300s: {script.filename}"
        except FileNotFoundError:
            return f"PowerShell 7 not found. Install pwsh: https://aka.ms/powershell"
        except Exception as e:
            return f"Error running {script.filename}: {e}"
