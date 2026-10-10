"""Read-only git state for the browser IDE: ``/git/status`` and ``/git/diff``.

Same containment rule as :mod:`adk.harnesses.fs`: the PATH asked about must
resolve inside a browsable root. The repository that contains it may be wider
than the root (a root can be a subdirectory of a repo), so status entries are
filtered back to the browsable roots before they are returned -- the caller
never learns the names of files it could not have listed.

git is run with an argument LIST (never a shell), a timeout, no optional locks
(a status poll must not fight a concurrent ``git commit`` for ``index.lock``)
and literal pathspecs (a file named ``*.py`` is that file, not a glob).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from adk.harnesses.fs import _contained, resolve_within

#: Status entries returned before ``truncated`` is set.
MAX_STATUS_ENTRIES = int(os.environ.get("AITHER_HARNESS_MAX_GIT_ENTRIES", "2000"))

#: Largest diff returned, in bytes.
MAX_DIFF_BYTES = int(os.environ.get("AITHER_HARNESS_MAX_DIFF", str(512 * 1024)))

GIT_TIMEOUT_S = 15


class GitUnavailableError(Exception):
    """git cannot answer for this path; the message is the ``reason``."""


def _git(args: list[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    exe = shutil.which("git")
    if not exe:
        raise GitUnavailableError("git not installed")
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("LC_ALL", "C")
    cmd = [exe, "--no-pager", "--literal-pathspecs", "-c", "core.quotepath=off"]
    if cwd is not None:
        cmd += ["-C", str(cwd)]
    try:
        return subprocess.run(
            cmd + args,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_S,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise GitUnavailableError(f"git did not answer within {GIT_TIMEOUT_S}s") from exc
    except OSError as exc:
        raise GitUnavailableError(f"cannot run git: {exc}") from exc


def _toplevel(target: Path) -> Path:
    directory = target if target.is_dir() else target.parent
    proc = _git(["rev-parse", "--show-toplevel"], cwd=directory)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise GitUnavailableError(f"not a git repository: {err[-1] if err else directory}")
    out = proc.stdout.decode("utf-8", "replace").strip()
    if not out:
        raise GitUnavailableError("not a git repository (no work tree)")
    return Path(out).resolve()


_AHEAD = re.compile(r"ahead (\d+)")
_BEHIND = re.compile(r"behind (\d+)")


def _parse_branch(line: str) -> tuple[str, int, int]:
    """``## main...origin/main [ahead 1, behind 2]`` -> (branch, ahead, behind)."""
    head = line[3:] if line.startswith("## ") else line
    ahead = int(m.group(1)) if (m := _AHEAD.search(head)) else 0
    behind = int(m.group(1)) if (m := _BEHIND.search(head)) else 0
    for prefix in ("No commits yet on ", "Initial commit on "):
        if head.startswith(prefix):
            return head[len(prefix):].strip(), ahead, behind
    name = head.split(" [", 1)[0].split("...", 1)[0].strip()
    if name.startswith("HEAD (no branch)"):
        name = "HEAD"
    return name, ahead, behind


def _unavailable(reason: str, **extra: Any) -> dict[str, Any]:
    return {"available": False, "reason": reason, "root": "", **extra}


def git_status(path: str, roots: list[Path]) -> dict[str, Any]:
    target = resolve_within(path, roots)
    empty = {"branch": "", "ahead": 0, "behind": 0, "entries": [], "truncated": False}
    try:
        top = _toplevel(target)
        proc = _git(["status", "--porcelain=v1", "-b", "-z", "--untracked-files=normal"], cwd=top)
    except GitUnavailableError as exc:
        return _unavailable(str(exc), **empty)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        return {"available": False, "reason": f"git status failed: {err[:500]}",
                "root": str(top), **empty}

    fields = proc.stdout.decode("utf-8", "replace").split("\0")
    branch, ahead, behind = "", 0, 0
    entries: list[dict[str, Any]] = []
    truncated = False
    i = 0
    while i < len(fields):
        field = fields[i]
        i += 1
        if not field:
            continue
        if field.startswith("## "):
            branch, ahead, behind = _parse_branch(field)
            continue
        if len(field) < 4:
            continue
        code, rel = field[:2], field[3:]
        orig = ""
        if code[0] in "RC" or code[1] in "RC":
            # -z: a rename/copy's ORIGINAL path is the next NUL-separated field.
            orig = fields[i] if i < len(fields) else ""
            i += 1
        absolute = Path(os.path.normpath(top / rel))
        if not _contained(absolute, roots):
            continue
        if len(entries) >= MAX_STATUS_ENTRIES:
            truncated = True
            break
        entry = {"path": str(absolute), "rel": rel, "index": code[0], "worktree": code[1],
                 "status": code}
        if orig:
            entry["orig_rel"] = orig
        entries.append(entry)
    return {"available": True, "reason": "", "root": str(top), "branch": branch,
            "ahead": ahead, "behind": behind, "entries": entries, "truncated": truncated}


def _is_untracked(top: Path, rel: str) -> bool:
    proc = _git(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", rel], cwd=top)
    if proc.returncode != 0:
        return False
    return any(f.startswith("?? ") for f in proc.stdout.decode("utf-8", "replace").split("\0"))


def _new_file_diff(target: Path, rel: str) -> bytes:
    """A unified diff that adds ``target`` -- what ``git diff`` would show once staged."""
    raw = target.read_bytes()
    head = f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n".encode("utf-8")
    if b"\x00" in raw[:8192]:
        return head + f"Binary files /dev/null and b/{rel} differ\n".encode("utf-8")
    if not raw:
        return head
    lines = raw.split(b"\n")
    missing_eol = lines[-1] != b""
    if not missing_eol:
        lines = lines[:-1]
    out = [head, f"--- /dev/null\n+++ b/{rel}\n@@ -0,0 +1,{len(lines)} @@\n".encode("utf-8")]
    out.extend(b"+" + line + b"\n" for line in lines)
    if missing_eol:
        out.append(b"\\ No newline at end of file\n")
    return b"".join(out)


def git_diff(path: str, roots: list[Path], staged: bool = False) -> dict[str, Any]:
    target = resolve_within(path, roots)
    base = {"path": str(target), "diff": "", "truncated": False}
    try:
        top = _toplevel(target)
        try:
            rel = target.relative_to(top).as_posix() or "."
        except ValueError:
            return _unavailable(f"{target} is not inside the repository at {top}", **base)
        if not staged and target.is_file() and _is_untracked(top, rel):
            raw = _new_file_diff(target, rel)
        else:
            args = ["diff", "--no-color", "--no-ext-diff"]
            if staged:
                args.append("--cached")
            proc = _git(args + ["--", rel], cwd=top)
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", "replace").strip()
                return {"available": False, "reason": f"git diff failed: {err[:500]}",
                        "root": str(top), **base}
            raw = proc.stdout
    except GitUnavailableError as exc:
        return _unavailable(str(exc), **base)
    except OSError as exc:
        return _unavailable(f"cannot read {target}: {exc}", **base)

    truncated = len(raw) > MAX_DIFF_BYTES
    if truncated:
        raw = raw[:MAX_DIFF_BYTES]
    return {"available": True, "reason": "", "root": str(top), "path": str(target),
            "diff": raw.decode("utf-8", "replace"), "truncated": truncated}
