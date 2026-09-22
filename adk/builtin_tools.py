"""Built-in tools — core capabilities that work WITHOUT AitherOS/awnode.

These give agents real autonomy in standalone mode:
  - File I/O (read, write, edit, list, search)
  - Shell execution (subprocess with timeout + capture)
  - Python REPL (isolated exec with output capture)
  - Web search/fetch (via DuckDuckGo + httpx)
  - Secrets store (local encrypted keyring, no AitherSecrets needed)

When awnode is available, these are SUPPLEMENTED (not replaced) by the
449 MCP tools. Built-in tools always work offline.

Usage:
    from adk.builtin_tools import register_builtin_tools

    agent = AitherAgent("demiurge")
    register_builtin_tools(agent, categories=["file_io", "shell", "web"])
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from adk.agent import AitherAgent

logger = logging.getLogger("adk.builtin_tools")

# Safety: directories agents can access (expandable via AITHER_ALLOWED_ROOTS)
_DEFAULT_ALLOWED_ROOTS = [os.getcwd()]
_ALLOWED_ROOTS: list[str] | None = None


def _get_allowed_roots() -> list[str]:
    global _ALLOWED_ROOTS
    if _ALLOWED_ROOTS is None:
        extra = os.getenv("AITHER_ALLOWED_ROOTS", "")
        _ALLOWED_ROOTS = _DEFAULT_ALLOWED_ROOTS + [r for r in extra.split(";") if r]
    return _ALLOWED_ROOTS


def set_allowed_roots(roots: list[str]) -> None:
    """Explicit override of the agent's writable roots. Resets the memoized
    cache so it takes effect immediately (env-only binding is a no-op after
    the first file-tool call, which memoizes _ALLOWED_ROOTS)."""
    global _ALLOWED_ROOTS
    _ALLOWED_ROOTS = list(_DEFAULT_ALLOWED_ROOTS) + [r for r in roots if r]


def _is_safe_path(path: str) -> bool:
    """Check if a path is within allowed roots.

    When a pack scope is active (pack-UI bridge invocation), the ONLY allowed
    root is that pack's data dir — fail-closed so a pack UI can never read the
    owner's unrelated files through the built-in file tools.
    """
    try:
        from adk.pack_scope import get_pack_scope, path_in_scope
        if get_pack_scope() is not None:
            return path_in_scope(path)
    except ImportError:
        pass
    try:
        resolved = str(Path(path).resolve())
        return any(resolved.startswith(str(Path(r).resolve())) for r in _get_allowed_roots())
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# File I/O Tools
# ─────────────────────────────────────────────────────────────────────────────

def file_read(path: str, start_line: int = 0, end_line: int = 0, **_ignored) -> str:
    """Read a file from disk. Returns file contents.

    path: Absolute or relative file path
    start_line: Start reading from this line (0 = beginning)
    end_line: Stop reading at this line (0 = end of file)
    """
    if not _is_safe_path(path):
        return json.dumps({"error": f"Path outside allowed roots: {path}"})
    try:
        p = Path(path)
        if not p.exists():
            _alt, _note = _resolve_missing_path(path)
            if not _alt:
                return json.dumps({"error": f"File not found: {path}",
                                   "hint": _note or ("Locate the file with a "
                                                     "search and use the path "
                                                     "the search reported.")})
            p = Path(_alt)
        if p.stat().st_size > 10_000_000:  # 10MB limit
            return json.dumps({"error": "File too large (>10MB)"})
        content = p.read_text(encoding="utf-8", errors="replace")
        if start_line or end_line:
            lines = content.split("\n")
            start = max(0, start_line - 1) if start_line else 0
            end = end_line if end_line else len(lines)
            content = "\n".join(lines[start:end])
        return content
    except Exception as e:
        return json.dumps({"error": str(e)})


def _eol_of(raw: str) -> str:
    """The line ending a file actually uses, so an edit keeps it.

    Measured 2026-08-22 on SWE-bench-Live falcon-2366, driven from a Windows host:
    `Path.write_text` translates every LF to CRLF, so ONE exact-match edit in a Linux
    checkout came back as a 616-line whole-file rewrite (616 insertions / 616
    deletions, zero semantic change under --ignore-cr-at-eol). The graded patch was
    line-ending noise and nothing in the tool result said so. The rule is: the file
    decides, never the host.
    """
    crlf = raw.count(CR_LF)
    lf = raw.count(LF) - crlf
    return CR_LF if crlf > lf else LF


CR_LF = "\r\n"
LF = "\n"


def file_write(path: str, content: str, mode: str = "overwrite", **_ignored) -> str:
    """Write content to a file on disk.

    path: File path to write to
    content: Content to write
    mode: 'overwrite' or 'append'
    """
    if not _is_safe_path(path):
        return json.dumps({"error": f"Path outside allowed roots: {path}"})
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # newline="" = write the bytes the caller gave, never the host's line
        # ending. On append, match whatever the file already uses (see _eol_of).
        if mode == "append":
            if p.exists():
                with open(p, "r", encoding="utf-8", newline="") as f:
                    eol = _eol_of(f.read())
                content = content.replace(CR_LF, LF).replace(LF, eol)
            with open(p, "a", encoding="utf-8", newline="") as f:
                f.write(content)
        else:
            p.write_text(content, encoding="utf-8", newline="")
        return json.dumps({"success": True, "path": str(p), "bytes": len(content)})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _resolve_missing_path(path: str, max_hits: int = 5):
    """Locate a file the caller named wrongly, or explain what was found.

    MEASURED, and it overturned two earlier theories. On the A9 arm the
    dominant `file_edit` failure was NOT bad `old_text` at all -- it was
    `File not found`, 7 of 16 misses, including
    `dev/babel/babel/dates.py`: a real file under a prefix the model invented.
    (The theories it replaced: "the model edits without reading" -- false, only
    6 of 39 edits targeted an unread file; and "the content is evicted from
    context" -- false, failed edits sit at median distance 2 from the read,
    identical to successful ones.)

    So the model knows WHICH file it wants and cannot spell the path to it. A
    unique basename match under an allowed root is therefore not a guess, it is
    the file -- and resolving it is the difference between a turn spent and a
    turn wasted. Ambiguity is REPORTED rather than picked: two files named
    `utils.py` are a question only the caller can answer, and choosing one
    silently would edit the wrong file while reporting success.

    Returns (resolved_path | None, note).
    """
    try:
        want = Path(path).name
        if not want:
            return None, ""
        roots = []
        for r in (os.environ.get("AITHER_ALLOWED_ROOTS") or "").split(os.pathsep):
            r = r.strip()
            if r and Path(r).is_dir():
                roots.append(Path(r))
        if not roots:
            return None, ""
        hits = []
        for root in roots:
            for cand in root.rglob(want):
                if cand.is_file() and _is_safe_path(str(cand)):
                    hits.append(cand)
                    if len(hits) > max_hits:
                        break
            if len(hits) > max_hits:
                break
        if len(hits) == 1:
            return str(hits[0]), f"path resolved by basename to {hits[0]}"
        if len(hits) > 1:
            names = ", ".join(str(h) for h in hits[:max_hits])
            return None, f"{len(hits)} files match {want!r}: {names}"
        return None, ""
    except Exception as e:  # noqa: BLE001
        # Say WHY the search failed instead of folding it into the caller's
        # bare "File not found". A permission error on the tree walk and a
        # genuinely absent file produce the SAME message otherwise, and they
        # need opposite fixes -- the exact silence this resolver exists to end.
        return None, f"path search failed ({type(e).__name__}: {e})"


def _edit_miss_diagnosis(content: str, old_text: str, max_chars: int = 700) -> dict:
    """Explain WHY an exact-match edit missed, instead of just saying it did.

    `{"error": "old_text not found in file"}` is a dead end: it tells the model
    that its guess was wrong and nothing about how, so the only move left is to
    guess again. Measured 2026-08-21 on a 12-instance SWE-bench-Live run, an 8B
    agent failed **26 of 39** `file_edit` calls this way (67%), including one
    instance that retried the SAME failing edit four times on one file. A 27B on
    the identical harness failed 0 of 14 -- so this error message is most
    expensive exactly where the model is weakest, and it is the difference
    between a retry that can converge and one that cannot.

    Three causes are separated because they need different corrections:
      * whitespace/indentation only  -> re-copy the line verbatim;
      * present but not unique       -> add surrounding context;
      * genuinely absent             -> here is the closest region we found.
    """
    import difflib

    norm = lambda t: " ".join(t.split())          # noqa: E731
    if norm(old_text) and norm(old_text) in norm(content):
        return {"reason": "whitespace_or_indentation",
                "hint": ("The text IS present but its WHITESPACE or INDENTATION "
                         "differs. Re-read the file and copy the line exactly, "
                         "including leading spaces.")}

    stripped = old_text.strip()
    if stripped and stripped in content and stripped != old_text:
        return {"reason": "surrounding_whitespace",
                "hint": ("The text matches once stripped. Drop the leading or "
                         "trailing whitespace from old_text.")}

    lines = content.splitlines()
    first = (old_text.strip().splitlines() or [""])[0].strip()
    best_i, best_r = None, 0.0
    if first:
        for i, ln in enumerate(lines):
            r = difflib.SequenceMatcher(None, first, ln.strip()).ratio()
            if r > best_r:
                best_i, best_r = i, r
    if best_i is None or best_r < 0.5:
        return {"reason": "not_present",
                "hint": ("No similar text found. Read the file first and copy "
                         "the exact text you intend to replace.")}
    lo, hi = max(0, best_i - 3), min(len(lines), best_i + 6)
    nearest = "\n".join(lines[lo:hi])[:max_chars]
    return {"reason": "closest_match",
            "closest_line": best_i + 1,
            "similarity": round(best_r, 2),
            "nearest_text": nearest,
            "hint": ("Nearest region shown above. Copy from it EXACTLY -- "
                     "old_text must match the file byte for byte.")}


def file_edit(path: str, old_text: str, new_text: str, **_ignored) -> str:
    """Edit a file by replacing old_text with new_text (exact string match).

    path: File path to edit
    old_text: Exact text to find and replace
    new_text: Replacement text
    """
    if not _is_safe_path(path):
        return json.dumps({"error": f"Path outside allowed roots: {path}"})
    try:
        p = Path(path)
        if not p.exists():
            _alt, _note = _resolve_missing_path(path)
            if not _alt:
                return json.dumps({"error": f"File not found: {path}",
                                   "hint": _note or ("Locate the file with a "
                                                     "search and use the path "
                                                     "the search reported.")})
            p = Path(_alt)
            path = _alt
        if old_text == new_text:
            # A replacement that changes nothing used to answer {"success": true}:
            # measured 2026-08-22 (falcon-2366), the agent "edited" twice with
            # old_text == new_text, was told both succeeded, stopped, and shipped
            # an empty change. Silence is not a pass; say it.
            return json.dumps({"error": "old_text and new_text are identical — "
                                        "nothing would change. Supply the NEW text."})
        with open(p, "r", encoding="utf-8", newline="") as f:
            raw = f.read()  # newline="" keeps CR; Path.read_text(newline=) is 3.13+
        eol = _eol_of(raw)
        content = raw.replace(CR_LF, LF)          # match on LF, whatever the file uses
        old_lf = old_text.replace(CR_LF, LF)
        if old_lf not in content:
            diag = _edit_miss_diagnosis(content, old_lf)
            return json.dumps({"error": "old_text not found in file", **diag})
        count = content.count(old_lf)
        if count > 1:
            return json.dumps({"error": f"old_text found {count} times — must be unique. Add more context."})
        new_content = content.replace(old_lf, new_text.replace(CR_LF, LF), 1)
        if eol != LF:
            new_content = new_content.replace(LF, eol)
        p.write_text(new_content, encoding="utf-8", newline="")
        return json.dumps({"success": True, "path": str(p)})
    except Exception as e:
        return json.dumps({"error": str(e)})


def file_list(path: str = ".", pattern: str = "*", **_ignored) -> str:
    """List files in a directory.

    path: Directory path to list
    pattern: Glob pattern to filter (default: *)
    """
    if not _is_safe_path(path):
        return json.dumps({"error": f"Path outside allowed roots: {path}"})
    try:
        p = Path(path)
        if not p.is_dir():
            return json.dumps({"error": f"Not a directory: {path}"})
        entries = []
        for item in sorted(p.glob(pattern))[:200]:
            entries.append({
                "name": item.name,
                "type": "dir" if item.is_dir() else "file",
                "size": item.stat().st_size if item.is_file() else 0,
            })
        return json.dumps({"path": str(p), "entries": entries, "count": len(entries)})
    except Exception as e:
        return json.dumps({"error": str(e)})


def file_search(path: str = ".", pattern: str = "", content_pattern: str = "",
                max_results: int = 50, file_glob: str = "",
                query: str = "", **_ignored) -> str:
    """Search for files by name pattern, optionally grep for content.

    path: Root directory to search
    pattern: Glob pattern for filenames (e.g. '**/*.py')
    content_pattern: Optional text to search for inside matching files
    max_results: Max files to return (default 50)

    `max_results` exists because the SIBLINGS have it. `code_search` and
    `repowise_search` both accept it, so a model reasonably generalises across
    the *_search family and calls `file_search(max_results=...)` -- which
    raised `unexpected keyword argument` and burned a whole turn on a HARD
    failure. Measured 2026-08-21 on a real agent run: three consecutive
    file_search calls failed that way inside one instance before the agent gave
    up. The result was recorded as the model failing to find the defect.

    An inconsistent tool family is a harness defect, not a model error: the
    model's inference was correct and only our signature disagreed. The limit
    was already hardcoded at 50 below, so this exposes an existing behaviour
    rather than adding one.

    WHY `**_ignored` AND THE ALIASES, rather than adding kwargs one at a time.
    Patching `max_results` alone was whack-a-mole: the very next run failed on
    `file_glob` (which `code_search` has), in a different instance. A tool that
    HARD-FAILS on an unexpected keyword converts a near-miss into a lost turn,
    and an agent with a 15-step budget cannot afford three of them. So unknown
    keywords are absorbed and REPORTED in the payload rather than raising --
    visible, so a real schema drift is still noticed, but never fatal.
    `file_glob` and `query` are accepted as aliases for `pattern` because that
    is unambiguously what a caller means by them.
    """
    pattern = pattern or file_glob or query or "**/*"
    if not _is_safe_path(path):
        return json.dumps({"error": f"Path outside allowed roots: {path}"})
    try:
        p = Path(path)
        matches = []
        for item in p.glob(pattern):
            if not item.is_file():
                continue
            if content_pattern:
                try:
                    text = item.read_text(encoding="utf-8", errors="replace")
                    if content_pattern not in text:
                        continue
                    # Find line numbers
                    lines = []
                    for i, line in enumerate(text.split("\n"), 1):
                        if content_pattern in line:
                            lines.append({"line": i, "text": line.strip()[:200]})
                            if len(lines) >= 5:
                                break
                    matches.append({"path": str(item), "matches": lines})
                except Exception:
                    continue
            else:
                matches.append({"path": str(item)})
            if len(matches) >= max(1, int(max_results or 50)):
                break
        payload = {"results": matches, "count": len(matches)}
        if _ignored:
            # Surface, never swallow: a silently-absorbed kwarg is how a real
            # schema drift hides. The call still succeeds.
            payload["ignored_kwargs"] = sorted(_ignored)
        return json.dumps(payload)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Shell & Python Execution
# ─────────────────────────────────────────────────────────────────────────────

def shell_exec(command: str, timeout: int = 30) -> str:
    """Execute a shell command and return stdout + stderr.

    command: Shell command to run
    timeout: Maximum execution time in seconds (default 30)
    """
    import shlex

    # Block obviously dangerous patterns before execution
    _BLOCKED_PATTERNS = [
        "rm -rf /", "mkfs.", "dd if=", "> /dev/sd",
        ":(){ :|:", "chmod -R 777 /",
    ]
    cmd_lower = command.lower().strip()
    for pat in _BLOCKED_PATTERNS:
        if pat in cmd_lower:
            return json.dumps({"error": f"Blocked: dangerous pattern '{pat}'"})

    try:
        # On Unix, avoid shell=True to prevent injection.
        # On Windows, shell=True is needed for built-in commands.
        use_shell = sys.platform == "win32"
        cmd_arg = command if use_shell else shlex.split(command)
        result = subprocess.run(
            cmd_arg,
            shell=use_shell,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
            cwd=os.getcwd(),
            stdin=subprocess.DEVNULL,
        )
        output = {
            "exit_code": result.returncode,
            "stdout": result.stdout[:50_000],
            "stderr": result.stderr[:10_000],
        }
        return json.dumps(output)
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"Command timed out after {timeout}s"})
    except Exception as e:
        return json.dumps({"error": str(e)})


_SAFE_BUILTINS = {
    k: getattr(__builtins__, k) if hasattr(__builtins__, k) else __builtins__[k]
    for k in (
        "abs", "all", "any", "bool", "bytes", "callable", "chr", "dict",
        "dir", "divmod", "enumerate", "filter", "float", "format",
        "frozenset", "getattr", "hasattr", "hash", "hex", "id", "int",
        "isinstance", "issubclass", "iter", "len", "list", "map", "max",
        "min", "next", "oct", "ord", "pow", "print", "range", "repr",
        "reversed", "round", "set", "slice", "sorted", "str", "sum",
        "tuple", "type", "zip",
        # Exception types — code legitimately raises/catches these
        "Exception", "BaseException", "ValueError", "TypeError", "KeyError",
        "IndexError", "AttributeError", "RuntimeError", "NameError",
        "ZeroDivisionError", "ArithmeticError", "LookupError", "StopIteration",
        "AssertionError", "NotImplementedError", "OSError", "FileNotFoundError",
        "ImportError", "OverflowError",
    )
    if (hasattr(__builtins__, k) if isinstance(__builtins__, type) else k in __builtins__)
}
# Allow safe imports only
_ALLOWED_MODULES = {
    "math", "json", "re", "datetime", "collections", "itertools",
    "functools", "string", "textwrap", "statistics", "random",
    "pathlib", "csv", "io", "base64", "hashlib", "urllib.parse", "sys",
}


def _restricted_import(name, *args, **kwargs):
    if name.split(".")[0] not in _ALLOWED_MODULES:
        raise ImportError(
            f"Module '{name}' not allowed in sandbox. "
            f"Allowed: {', '.join(sorted(_ALLOWED_MODULES))}"
        )
    return __import__(name, *args, **kwargs)


def python_exec(code: str) -> str:
    """Execute Python code in a restricted sandbox and capture output.

    code: Python code to execute
    """
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()
    safe_builtins = {**_SAFE_BUILTINS, "__import__": _restricted_import}
    namespace: dict = {"__builtins__": safe_builtins}
    result_val = None

    try:
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            exec(code, namespace)  # noqa: S102
            # If there's a 'result' variable, capture it
            if "result" in namespace:
                result_val = namespace["result"]
    except Exception as e:
        stderr_capture.write(f"\n{type(e).__name__}: {e}")

    output = {
        "stdout": stdout_capture.getvalue()[:50_000],
        "stderr": stderr_capture.getvalue()[:10_000],
    }
    if result_val is not None:
        try:
            output["result"] = json.loads(json.dumps(result_val, default=str))
        except Exception:
            output["result"] = str(result_val)
    return json.dumps(output)


# ─────────────────────────────────────────────────────────────────────────────
# Web Tools
# ─────────────────────────────────────────────────────────────────────────────

async def web_search(query: str, limit: int = 5, max_results: int = 0, **_extra: object) -> str:
    """Search the web using DuckDuckGo. Returns search results.

    query: Search query string
    limit: Maximum number of results (default 5)
    max_results: alias for `limit` (the other convention in this codebase)
    """

    # A small local model (e.g. the offline orchestrator) frequently emits
    # `max_results=` or an invented kwarg instead of `limit=`. A bare
    # `web_search(query, limit=5)` then raises TypeError, which the agent loop
    # surfaces as "no answer from the agent" after a long stall — the exact
    # symptom of a broken tool. Tolerate the alias and swallow stray kwargs so an
    # unknown argument degrades to a real search, never a crash.
    if max_results and max_results > 0:
        limit = max_results
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 5
    if limit < 1:
        limit = 5

    # USE awfind -- the search client that already exists.
    #
    # Everything below this is a REGEX SCRAPE OF DUCKDUCKGO'S HTML, and its
    # failure mode is the bad one: when the markup changes the title selector
    # keeps matching while the snippet pattern stops, so results come back with
    # titles, URLs and EMPTY TEXT. That reads as "the web had nothing useful"
    # rather than as a broken tool, and it is the class this repo's pack
    # contract checker exists for.
    #
    # The first fix here hand-rolled an httpx POST at the service. That was the
    # same mistake one layer over: `awfind` IS the client for exactly this
    # service -- typed rows, mode selection, a bearer that is the CALLER's, and
    # it RAISES rather than returning an empty list, because "the search failed"
    # and "the search matched nothing" are different facts. Reimplementing it
    # produced a second thing to keep in step with a service neither of us owns.
    #
    # Configured by ENV with no default host: this package ships to PyPI and
    # must carry nobody's topology, and a URL nobody set cannot go stale.
    # ENV FIRST, THEN THE USER'S OWN CONFIG -- and the second half is why this
    # lane had never once run. Measured 2026-08-23 on the author's box: awfind
    # was installed, AitherSearch was Up 2 hours (healthy) publishing 8114, and
    # neither env var was set anywhere in the tree -- one reader, zero writers.
    # So EVERY search fell through to the DuckDuckGo scrape below, which is the
    # failure this function's own comment warns about, and the user-visible
    # symptom was exactly what that comment predicts: results with titles and
    # no usable snippets, reported as "the web had nothing useful".
    #
    # An env-only switch is a switch nothing flips. ~/.aither/config.* is the
    # store the ADK already reads and the user already owns, so honouring it
    # keeps the PyPI package carrying nobody's topology while giving the box
    # that HAS a search service a way to say so.
    _cfg: dict = {}
    try:
        from adk.config import load_saved_config

        _cfg = load_saved_config() or {}
    except Exception:
        _cfg = {}

    def _resolve(*names: str) -> str:
        for n in names:
            v = os.environ.get(n)
            if v:
                return v
        for n in names:
            v = _cfg.get(n.lower().replace("adk_", "").replace("aither_", ""))
            if v:
                return str(v)
        return ""

    _svc = _resolve("ADK_SEARCH_URL", "AITHER_SEARCH_URL")
    if _svc:
        try:
            from awfind import FindClient

            # verify: never False. A private-CA deployment passes its bundle
            # path; anything else keeps full verification.
            _ca = _resolve("ADK_SEARCH_CA_BUNDLE")
            _client = FindClient(_svc, token=_resolve("ADK_SEARCH_TOKEN") or None,
                                 verify=_ca if _ca else True)
            _answer = _client.quick(query, limit=limit)
            _rows = list(_answer)
            if _rows:
                _nl = chr(10)
                return (_nl + _nl).join(
                    _nl.join([r.title or "", r.url or "", r.snippet or ""])
                    for r in _rows[:limit]
                )
            # Empty is not an error, but it is not an answer either: fall
            # through rather than report "nothing found" from a service that
            # may simply be warming up.
        except ImportError:
            pass  # awfind not installed -> the scrape below still answers
        except Exception:
            pass  # service down or refusing -> same

    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "AitherADK/1.0"},
            )
            resp.raise_for_status()
            text = resp.text

        # Parse results from HTML (simple extraction)
        results = []
        import re
        links = re.findall(r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', text)
        snippets = re.findall(r'class="result__snippet">(.*?)</a>', text, re.DOTALL)

        for i, (url, title) in enumerate(links[:limit]):
            snippet = snippets[i].strip() if i < len(snippets) else ""
            # Clean HTML tags
            title = re.sub(r'<[^>]+>', '', title).strip()
            snippet = re.sub(r'<[^>]+>', '', snippet).strip()
            # Decode DuckDuckGo redirect URL
            if "uddg=" in url:
                from urllib.parse import unquote, parse_qs, urlparse
                parsed = urlparse(url)
                params = parse_qs(parsed.query)
                url = unquote(params.get("uddg", [url])[0])
            results.append({"title": title, "url": url, "snippet": snippet[:300]})

        return json.dumps({"query": query, "results": results})
    except ImportError:
        return json.dumps({"error": "httpx required for web search"})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _is_safe_url(url: str) -> bool:
    """Block SSRF: reject private IPs, localhost, metadata endpoints.

    The guard moved to ``adk.webfetch.is_safe_url`` so every fetch engine (httpx,
    Scrapling, the stealth browser) sits behind the same gate; this name stays
    for callers that imported it from here.
    """
    from adk.webfetch import is_safe_url
    return is_safe_url(url)


async def web_fetch(url: str, max_chars: int = 20000, stealth: bool = False) -> str:
    """Fetch a webpage and return its text content.

    url: URL to fetch
    max_chars: Maximum characters to return (default 20000)
    stealth: Use the anti-bot fetch ladder (needs the awdk[scrape] extra); default false
    """
    from adk.webfetch import fetch
    try:
        result = await fetch(url, max_chars=max_chars, stealth=stealth)
    except Exception as e:
        return json.dumps({"error": str(e)})
    if result.error and not result.text:
        return json.dumps({"error": result.error, "engine": result.engine, "status": result.status})
    return result.text[:max_chars]


# ─────────────────────────────────────────────────────────────────────────────
# Secrets Store (local, standalone)
# ─────────────────────────────────────────────────────────────────────────────

_secrets_cache: dict[str, str] | None = None
_SECRETS_FILE = Path(os.getenv("AITHER_DATA_DIR", os.path.expanduser("~/.aither"))) / "secrets.enc"
_SECRETS_FILE_LEGACY = _SECRETS_FILE.with_suffix(".json")


def _derive_key() -> bytes:
    """Derive an encryption key from machine-specific data."""
    import hashlib
    # Combine username + home dir + machine-specific salt. os.getlogin() needs a
    # controlling terminal and raises OSError under CI/daemons/containers — fall
    # back to getpass (env-based) there; the two agree on normal interactive
    # boxes, so existing secret files keep decrypting.
    try:
        user = os.getlogin()
    except OSError:
        import getpass
        user = getpass.getuser()
    material = f"{user}:{Path.home()}:aither-adk-secrets-v1"
    return hashlib.pbkdf2_hmac("sha256", material.encode(), b"adk-salt-v1", 100_000)


def _encrypt_secrets(data: dict) -> bytes:
    """XOR-based obfuscation with derived key. Not military-grade but prevents
    casual plaintext exposure in backups/cloud sync."""
    import hashlib
    key = _derive_key()
    plaintext = json.dumps(data).encode()
    stream = hashlib.sha256(key).digest()  # Expand key
    result = bytearray()
    for i, b in enumerate(plaintext):
        result.append(b ^ stream[i % len(stream)])
    return bytes(result)


def _decrypt_secrets(data: bytes) -> dict:
    """Reverse XOR obfuscation."""
    import hashlib
    key = _derive_key()
    stream = hashlib.sha256(key).digest()
    result = bytearray()
    for i, b in enumerate(data):
        result.append(b ^ stream[i % len(stream)])
    return json.loads(bytes(result).decode())


def _load_secrets() -> dict[str, str]:
    global _secrets_cache
    if _secrets_cache is not None:
        return _secrets_cache
    # Try encrypted file first
    if _SECRETS_FILE.exists():
        try:
            _secrets_cache = _decrypt_secrets(_SECRETS_FILE.read_bytes())
            return _secrets_cache
        except Exception:
            _secrets_cache = {}
    # Migrate legacy plaintext if exists
    if _SECRETS_FILE_LEGACY.exists():
        try:
            _secrets_cache = json.loads(
                _SECRETS_FILE_LEGACY.read_text(encoding="utf-8")
            )
            _save_secrets(_secrets_cache)  # Re-save encrypted
            _SECRETS_FILE_LEGACY.unlink()  # Remove plaintext
            return _secrets_cache
        except Exception:
            _secrets_cache = {}
    else:
        _secrets_cache = {}
    return _secrets_cache


def _save_secrets(data: dict[str, str]):
    global _secrets_cache
    _secrets_cache = data
    _SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SECRETS_FILE.write_bytes(_encrypt_secrets(data))
    # Restrict permissions
    try:
        os.chmod(_SECRETS_FILE, 0o600)
    except (OSError, AttributeError):
        pass  # Windows — best-effort


def secret_get(key: str) -> str:
    """Get a secret value by key. Checks env vars first, then local store.

    key: Secret key name
    """
    # Env var takes priority
    env_val = os.getenv(key)
    if env_val:
        return env_val
    secrets = _load_secrets()
    val = secrets.get(key)
    if val is None:
        return json.dumps({"error": f"Secret '{key}' not found"})
    return val


def secret_set(key: str, value: str) -> str:
    """Store a secret value. Persists to ~/.aither/secrets.json.

    key: Secret key name
    value: Secret value to store
    """
    secrets = _load_secrets()
    secrets[key] = value
    _save_secrets(secrets)
    return json.dumps({"success": True, "key": key})


def secret_list() -> str:
    """List all stored secret keys (values are NOT shown)."""
    secrets = _load_secrets()
    return json.dumps({"keys": list(secrets.keys()), "count": len(secrets)})


# ─────────────────────────────────────────────────────────────────────────────
# Creative Tools (AitherCanvas / ComfyUI)
# ─────────────────────────────────────────────────────────────────────────────

_CANVAS_URL = os.getenv("AITHER_CANVAS_URL", "http://localhost:8108")


def image_generate(
    prompt: str,
    negative_prompt: str = "",
    width: int = 1024,
    height: int = 1024,
    steps: int = 20,
) -> str:
    """Generate an image using AitherCanvas (ComfyUI).

    prompt: Detailed description of the image to generate
    negative_prompt: What to avoid in the image
    width: Image width in pixels (default 1024)
    height: Image height in pixels (default 1024)
    steps: Sampling steps (default 20)
    """
    try:
        import httpx
        resp = httpx.post(
            f"{_CANVAS_URL}/generate",
            json={
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "width": width,
                "height": height,
                "steps": steps,
            },
            timeout=300.0,
        )
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result", data)
        images = result.get("images", [])
        if images:
            import base64 as b64mod
            out_dir = os.path.join(os.getcwd(), "generated")
            os.makedirs(out_dir, exist_ok=True)
            timestamp = int(time.time())
            path = os.path.join(out_dir, f"gen_{timestamp}.png")
            with open(path, "wb") as f:
                f.write(b64mod.b64decode(images[0]))
            return json.dumps({
                "success": True,
                "path": path,
                "base64": images[0][:100] + "...",
                "count": len(images),
            })
        if result.get("paths"):
            return json.dumps({"success": True, "paths": result["paths"]})
        return json.dumps({"success": False, "error": "No images in response"})
    except Exception as e:
        err_msg = str(e)
        if "ConnectError" in type(e).__name__ or "Connection refused" in err_msg:
            return json.dumps({
                "success": False,
                "error": "AitherCanvas not running locally. Use MCP bridge to access "
                         "cloud image generation: MCPBridge(api_key=...).call_tool('generate_image', ...)",
            })
        return json.dumps({"success": False, "error": err_msg})


def image_refine(
    image_path: str,
    prompt: str,
    denoise: float = 0.5,
    negative_prompt: str = "",
) -> str:
    """Refine an existing image using AitherCanvas (Img2Img).

    image_path: Path to the source image
    prompt: Prompt to guide the refinement
    denoise: Denoising strength 0.0-1.0 (lower preserves more)
    negative_prompt: What to avoid
    """
    try:
        import httpx
        resp = httpx.post(
            f"{_CANVAS_URL}/generate",
            json={
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "source_image_path": image_path,
                "denoise": denoise,
                "mode": "img2img",
            },
            timeout=300.0,
        )
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result", data)
        images = result.get("images", [])
        if images:
            import base64 as b64mod
            out_dir = os.path.join(os.getcwd(), "generated")
            os.makedirs(out_dir, exist_ok=True)
            timestamp = int(time.time())
            path = os.path.join(out_dir, f"refine_{timestamp}.png")
            with open(path, "wb") as f:
                f.write(b64mod.b64decode(images[0]))
            return json.dumps({"success": True, "path": path, "count": len(images)})
        if result.get("paths"):
            return json.dumps({"success": True, "paths": result["paths"]})
        return json.dumps({"success": False, "error": "No images in response"})
    except Exception as e:
        err_msg = str(e)
        if "ConnectError" in type(e).__name__ or "Connection refused" in err_msg:
            return json.dumps({
                "success": False,
                "error": "AitherCanvas not running locally. Use MCP bridge for cloud access.",
            })
        return json.dumps({"success": False, "error": err_msg})


def image_smart(
    prompt: str,
    negative_prompt: str = "",
    width: int = 1024,
    height: int = 1024,
) -> str:
    """Smart generate — auto-detects diagram vs artistic image.

    prompt: Description of what to generate
    negative_prompt: What to avoid
    width: Image width (default 1024)
    height: Image height (default 1024)
    """
    try:
        import httpx
        resp = httpx.post(
            f"{_CANVAS_URL}/smart-generate",
            json={
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "width": width,
                "height": height,
            },
            timeout=300.0,
        )
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result", data)
        images = result.get("images", [])
        if images:
            import base64 as b64mod
            is_diagram = bool(result.get("mermaid_code"))
            out_dir = os.path.join(os.getcwd(), "generated")
            os.makedirs(out_dir, exist_ok=True)
            prefix = "diagram" if is_diagram else "smart"
            timestamp = int(time.time())
            path = os.path.join(out_dir, f"{prefix}_{timestamp}.png")
            with open(path, "wb") as f:
                f.write(b64mod.b64decode(images[0]))
            out = {"success": True, "path": path, "is_diagram": is_diagram}
            if is_diagram:
                out["mermaid_code"] = result.get("mermaid_code", "")
            return json.dumps(out)
        return json.dumps({"success": False, "error": "No images in response"})
    except Exception as e:
        err_msg = str(e)
        if "ConnectError" in type(e).__name__ or "Connection refused" in err_msg:
            return json.dumps({
                "success": False,
                "error": "AitherCanvas not running locally. Use MCP bridge for cloud access.",
            })
        return json.dumps({"success": False, "error": err_msg})


# ─────────────────────────────────────────────────────────────────────────────
# Git tools — essential for coding agents
# ─────────────────────────────────────────────────────────────────────────────


def git_status(path: str = ".") -> str:
    """Show working tree status (modified, staged, untracked files)."""
    try:
        r = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, cwd=path,
        )
        return r.stdout or "(clean)"
    except Exception as e:
        return f"Error: {e}"


def git_diff(path: str = ".", staged: bool = False) -> str:
    """Show file changes. Set staged=true for staged changes only."""
    cmd = ["git", "diff"]
    if staged:
        cmd.append("--staged")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, cwd=path)
        return r.stdout[:20000] or "(no changes)"
    except Exception as e:
        return f"Error: {e}"


def git_log(path: str = ".", count: int = 10) -> str:
    """Show recent commit history."""
    try:
        r = subprocess.run(
            ["git", "log", f"-{count}", "--oneline", "--no-decorate"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, cwd=path,
        )
        return r.stdout or "(no commits)"
    except Exception as e:
        return f"Error: {e}"


def git_add(files: str, path: str = ".") -> str:
    """Stage files for commit. Use '.' for all changes."""
    try:
        r = subprocess.run(
            ["git", "add"] + files.split(),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, cwd=path,
        )
        return r.stdout + r.stderr or "Staged"
    except Exception as e:
        return f"Error: {e}"


def git_commit(message: str, path: str = ".") -> str:
    """Create a commit with the given message."""
    try:
        r = subprocess.run(
            ["git", "commit", "-m", message],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, cwd=path,
        )
        return r.stdout + r.stderr
    except Exception as e:
        return f"Error: {e}"


def git_branch_list(path: str = ".") -> str:
    """List all branches, marking the current one."""
    try:
        r = subprocess.run(
            ["git", "branch", "-a", "--no-color"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, cwd=path,
        )
        return r.stdout or "(no branches)"
    except Exception as e:
        return f"Error: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Code search — grep/ripgrep for coding agents
# ─────────────────────────────────────────────────────────────────────────────


def code_search(pattern: str, path: str = ".", file_glob: str = "", max_results: int = 50) -> str:
    """Search code for a regex pattern. Uses ripgrep if available, falls back to grep.

    Args:
        pattern: Regex pattern to search for.
        path: Directory to search in.
        file_glob: Optional file pattern filter (e.g. '*.py', '*.ts').
        max_results: Max matching lines to return.
    """
    # Try ripgrep first (much faster)
    for cmd_name in ["rg", "grep"]:
        try:
            cmd = [cmd_name, "-n", "--no-heading"]
            if cmd_name == "rg":
                cmd.extend(["--max-count", str(max_results)])
                if file_glob:
                    cmd.extend(["--glob", file_glob])
            elif cmd_name == "grep":
                cmd.extend(["-r", f"--include={file_glob}" if file_glob else "-r"])
            cmd.extend([pattern, path])

            r = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            output = r.stdout[:30000]
            lines = output.strip().split("\n")
            if len(lines) > max_results:
                lines = lines[:max_results]
                lines.append(f"... ({len(output.strip().split(chr(10)))} total matches, showing {max_results})")
            return "\n".join(lines) or "(no matches)"
        except FileNotFoundError:
            continue
        except Exception as e:
            return f"Error: {e}"
    return "Error: neither rg nor grep found"


def repowise_search(query: str, max_results: int = 10) -> str:
    """Search codebase using Repowise semantic + keyword hybrid search.

    Uses the Repowise intelligence service for deep code understanding.
    Falls back to ripgrep code_search if Repowise is unavailable.

    Args:
        query: Natural language or keyword query
        max_results: Maximum results to return
    """
    import json as _json
    repowise_url = os.environ.get("AITHER_REPOWISE_URL", "http://localhost:7337")
    try:
        import httpx
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                f"{repowise_url}/v1/search",
                json={"query": query, "limit": max_results},
            )
            if resp.status_code == 200:
                data = resp.json()
                results = []
                for r in data.get("results", []):
                    results.append({
                        "file": r.get("file", ""),
                        "symbol": r.get("symbol", ""),
                        "snippet": r.get("snippet", "")[:200],
                        "score": round(r.get("score", 0), 3),
                    })
                return _json.dumps({"results": results, "count": len(results), "source": "repowise"})
    except Exception:
        pass
    # Fallback to ripgrep
    return code_search(pattern=query, max_results=max_results)


def _swarm_code_local(problem: str, mode: str = "forge", effort: int = 8) -> str:
    """Local A2A-based multi-agent dispatch for sovereign swarm coding.

    Runs a bounded fan-out of subtasks to local agents instead of calling Genesis.
    This is used when AITHER_OFFLINE=1 (sovereign mode).

    Args:
        problem: Task description
        mode: "llm" (text-only), "forge" (with tools/sandbox), "plan_only" (design only)
        effort: Effort level 1-10

    Returns:
        JSON string with status, plan, code, tests, artifacts keys
    """
    import json as _json

    try:
        import asyncio
        from adk.a2a_dispatch import bounded_recursive_dispatch

        # Run the async dispatcher in a new event loop (if not already in one)
        try:
            loop = asyncio.get_running_loop()
            # Already in an event loop; create a task
            task = loop.create_task(
                bounded_recursive_dispatch(
                    problem,
                    max_depth=2,  # Bounded depth
                    max_breadth=3,  # Bounded fan-out
                )
            )
            # For sync context, we can't await; return a placeholder
            return _json.dumps({
                "status": "working",
                "message": "Local swarm dispatch submitted; results will be available async",
            })
        except RuntimeError:
            # No event loop; create one
            result = asyncio.run(
                bounded_recursive_dispatch(
                    problem,
                    max_depth=2,  # Bounded depth
                    max_breadth=3,  # Bounded fan-out
                )
            )
            return _json.dumps({
                "status": result.get("status", "unknown"),
                "plan": result.get("plan", "")[:2000],
                "code": "",  # Local dispatch doesn't generate code; that's follow-up work
                "tests": "",
                "artifacts": result.get("results", []),
            })

    except Exception as e:
        return _json.dumps({
            "status": "failed",
            "error": f"Local dispatch failed: {str(e)}",
        })


def swarm_code(problem: str, mode: str = "forge", effort: int = 8) -> str:
    """Dispatch to AitherOS swarm coding engine for complex implementation tasks.

    The swarm runs 11 specialized agents in 4 phases:
    ARCHITECT -> SWARM (8 parallel) -> REVIEW -> JUDGE

    In sovereign (AITHER_OFFLINE=1) mode, uses local A2A dispatch instead of Genesis.
    In cloud mode, delegates to Genesis.

    Args:
        problem: Task or feature description to implement
        mode: "llm" (text-only), "forge" (with tools/sandbox), "plan_only" (design only)
        effort: Effort level 1-10 (affects model selection)
    """
    import json as _json

    # GATED: swarm coding drives the Genesis multi-agent engine — a paid-tier
    # capability. Free agents get a clear upgrade prompt instead of a dispatch.
    try:
        from adk.licensing import get_license_manager
        if not get_license_manager().can_use_swarm():
            return _json.dumps({
                "status": "failed",
                "error": (
                    "Swarm coding requires a Professional tier. Upgrade at "
                    "api.aitherium.com/portal/marketplace/packs"
                ),
            })
    except ImportError:
        pass

    # Sovereign (offline) mode: use local A2A dispatch
    if os.environ.get("AITHER_OFFLINE") == "1":
        return _swarm_code_local(problem, mode, effort)

    # Cloud mode: call Genesis
    genesis_url = os.environ.get("AITHER_GENESIS_URL", "http://localhost:8001")
    try:
        import httpx
        resp = httpx.post(
            f"{genesis_url}/swarm/code/sync",
            json={"problem": problem, "mode": mode, "effort": effort},
            timeout=600,
        )
        if resp.status_code == 200:
            data = resp.json()
            return _json.dumps({
                "status": data.get("status", "unknown"),
                "plan": data.get("architect_plan", "")[:2000],
                "code": data.get("code", "")[:5000],
                "tests": data.get("tests", "")[:2000],
                "artifacts": data.get("artifacts", []),
            })
        return _json.dumps({"error": f"Genesis returned {resp.status_code}"})
    except Exception as e:
        return _json.dumps({"error": str(e)})


def code_symbols(path: str, pattern: str = "") -> str:
    """List function/class definitions in a file. Optionally filter by pattern."""
    import ast as _ast
    try:
        source = Path(path).read_text(encoding="utf-8")
        tree = _ast.parse(source)
        symbols = []
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                name = f"def {node.name}() line {node.lineno}"
                if not pattern or pattern.lower() in node.name.lower():
                    symbols.append(name)
            elif isinstance(node, _ast.ClassDef):
                name = f"class {node.name} line {node.lineno}"
                if not pattern or pattern.lower() in node.name.lower():
                    symbols.append(name)
        return "\n".join(symbols) or "(no symbols found)"
    except Exception as e:
        return f"Error: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Faculty Graph Tools (registered when agent.set_code_graph/set_memory_graph)
# ─────────────────────────────────────────────────────────────────────────────


def _register_code_graph_tools(agent: "AitherAgent", code_graph) -> int:
    """Register CodeGraph-backed tools on an agent.

    Called by agent.set_code_graph(). Adds code_search and code_context tools.
    """
    import asyncio as _asyncio

    def cg_search(query: str, max_results: int = 10) -> str:
        """Search indexed code for functions/classes matching a query.

        query: Natural language or keyword query (e.g. 'authentication middleware')
        max_results: Maximum results to return (default 10)
        """
        try:
            try:
                loop = _asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    results = pool.submit(_asyncio.run, code_graph.query(query, max_results=max_results)).result(timeout=30)
            else:
                results = _asyncio.run(code_graph.query(query, max_results=max_results))
            items = []
            for chunk in results:
                items.append({
                    "name": chunk.name,
                    "type": chunk.chunk_type.value,
                    "file": chunk.source_path,
                    "line": chunk.start_line,
                    "signature": chunk.signature,
                    "calls": chunk.calls[:5],
                    "called_by": chunk.called_by[:5],
                })
            return json.dumps({"results": items, "count": len(items)})
        except Exception as e:
            return json.dumps({"error": str(e)})

    def cg_context(chunk_id: str) -> str:
        """Get full context for a code chunk including callers and callees.

        chunk_id: The chunk ID from a code_search result
        """
        try:
            ctx = code_graph.get_context_for_chunk(chunk_id)
            if not ctx:
                return json.dumps({"error": "Chunk not found"})
            chunk = ctx["chunk"]
            result = {
                "name": chunk.name,
                "signature": chunk.signature,
                "docstring": chunk.docstring,
                "file": chunk.source_path,
                "lines": f"{chunk.start_line}-{chunk.end_line}",
                "callers": [{"name": c.name, "file": c.source_path} for c in ctx.get("callers", [])],
                "callees": [{"name": c.name, "file": c.source_path} for c in ctx.get("callees", [])],
            }
            body = code_graph.get_full_body(chunk_id)
            if body:
                result["body"] = body[:5000]
            return json.dumps(result)
        except Exception as e:
            return json.dumps({"error": str(e)})

    agent._tools.register(cg_search, name="code_search", description="Search indexed code for functions/classes matching a query")
    agent._tools.register(cg_context, name="code_context", description="Get full context for a code chunk including callers and callees")
    logger.info("Registered CodeGraph tools (code_search, code_context) on agent %s", agent.name)
    return 2


def _register_memory_graph_tools(agent: "AitherAgent", memory_graph) -> int:
    """Register MemoryGraph-backed tools on an agent.

    Called by agent.set_memory_graph(). Adds mg_remember, mg_recall, mg_query tools.
    """
    from types import SimpleNamespace
    import hashlib as _hl

    def mg_remember(content: str, title: str = "", tags: str = "") -> str:
        """Store a memory in the agent's knowledge graph.

        content: The memory content to store
        title: Short title for the memory (optional)
        tags: Comma-separated tags (optional)
        """
        try:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
            mid = _hl.md5(content[:200].encode()).hexdigest()
            mem = SimpleNamespace(
                id=mid,
                title=title or content[:80],
                content=content,
                memory_type="episodic",
                tags=tag_list,
                source_agent=agent.name,
                importance=0.5,
                embedding=None,
                created_at=time.time(),
                archived=False,
                scope="shared",
            )
            memory_graph.add_node(mem, upsert=True)
            memory_graph.save()
            return json.dumps({"success": True, "id": mid})
        except Exception as e:
            return json.dumps({"error": str(e)})

    def mg_recall(query: str, max_results: int = 5) -> str:
        """Search agent memory for relevant past knowledge.

        query: What to search for in memory
        max_results: Maximum memories to return (default 5)
        """
        try:
            results = memory_graph.hybrid_query(query, max_results=max_results)
            items = []
            for node, score in results:
                mem = node.memory
                items.append({
                    "title": getattr(mem, "title", ""),
                    "content": getattr(mem, "content", "")[:500],
                    "tags": list(getattr(mem, "tags", []) or []),
                    "score": score,
                })
            return json.dumps({"results": items, "count": len(items)})
        except Exception as e:
            return json.dumps({"error": str(e)})

    def mg_stats() -> str:
        """Get memory graph statistics (node count, edges, etc.)."""
        try:
            stats = memory_graph.get_stats()
            return json.dumps(stats)
        except Exception as e:
            return json.dumps({"error": str(e)})

    agent._tools.register(mg_remember, name="remember", description="Store a memory in the agent's knowledge graph")
    agent._tools.register(mg_recall, name="recall", description="Search agent memory for relevant past knowledge")
    agent._tools.register(mg_stats, name="memory_stats", description="Get memory graph statistics")
    logger.info("Registered MemoryGraph tools (remember, recall, memory_stats) on agent %s", agent.name)
    return 3


# ─────────────────────────────────────────────────────────────────────────────
# Self-introspection tools (B.3 from .AITHEROS/31-AGENT-LONGRUN-AUDIT.md)
#
# The Reddit pain point: "I asked the agent what it had done and it lied."
# Solution: first-class tools that let an agent read its own audit trail
# instead of hallucinating one. Data lives in `agent._introspection` (a
# bounded deque) and `agent._files_touched`, populated by the dispatch loop
# in adk/agent.py. Tools are closures over `agent` so each agent only sees
# its own history.
# ─────────────────────────────────────────────────────────────────────────────

def register_self_tools(agent: AitherAgent) -> int:
    """Register `self_*` introspection tools that let an agent inspect what it did.

    Tools registered:
      - self_recent_tool_calls(n)   : last N tool calls (tool, args, latency, error)
      - self_files_touched()        : files the agent has read/written/edited this session
      - self_session_summary()      : counts + budget + memory + identity overview
      - self_memory_search(query)   : search the agent's own memory tier only
    """

    def self_recent_tool_calls(n: int = 10) -> str:
        """Return the last N tool calls this agent made in the current process.

        Use this to honestly answer the user's question "what did you just do?"
        instead of guessing from chat context.

        n: how many recent calls to return (default 10, max 200).
        """
        try:
            buf = list(getattr(agent, "_introspection", ()))
            if not buf:
                return json.dumps({"calls": [], "count": 0,
                                    "note": "no tool calls recorded yet"})
            n = max(1, min(int(n or 10), 200))
            return json.dumps({"calls": buf[-n:], "count": len(buf[-n:]),
                                "total_recorded": len(buf)}, default=str)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def self_files_touched() -> str:
        """Return every file path this agent has read/written/edited this session.

        Output: {path: {first_ts, last_ts, ops: [read|write|edit, ...]}}.
        Use this before claiming you modified a file \u2014 if it isn't here you didn't.
        """
        try:
            touched = dict(getattr(agent, "_files_touched", {}))
            return json.dumps({"files": touched, "count": len(touched)}, default=str)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def self_session_summary() -> str:
        """Return a structured summary of this agent's current session.

        Includes: identity, session id, total tool calls by name, distinct files
        touched, per-agent meter (tokens/cost so far), memory entry count.
        """
        try:
            buf = list(getattr(agent, "_introspection", ()))
            by_tool: dict[str, int] = {}
            errs = 0
            for rec in buf:
                by_tool[rec.get("tool", "?")] = by_tool.get(rec.get("tool", "?"), 0) + 1
                if rec.get("error"):
                    errs += 1
            meter_snap: dict = {}
            try:
                meter_snap = agent.meter.snapshot() if hasattr(agent.meter, "snapshot") else {}
            except Exception:
                pass
            return json.dumps({
                "agent": agent.name,
                "session_id": getattr(agent, "_session_id", None),
                "tool_calls_total": len(buf),
                "tool_calls_by_name": by_tool,
                "tool_errors": errs,
                "files_touched": len(getattr(agent, "_files_touched", {})),
                "meter": meter_snap,
            }, default=str)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def self_memory_search(query: str, k: int = 5) -> str:
        """Search this agent's OWN memory tier (not the global graph) for past context.

        query: free-text search
        k: max results (default 5, max 25)
        """
        try:
            mem = getattr(agent, "memory", None)
            if mem is None:
                return json.dumps({"results": [], "note": "no memory backend"})
            k = max(1, min(int(k or 5), 25))
            # memory.search may be sync or async; coerce
            res = mem.search(query, k=k) if hasattr(mem, "search") else []
            if asyncio.iscoroutine(res):
                # We're inside a sync tool; surface the coroutine name and ask caller to use recall.
                return json.dumps({"results": [],
                                    "note": "memory.search is async; use the 'recall' tool"})
            return json.dumps({"results": list(res) if res else [], "count": len(res or [])},
                                default=str)
        except Exception as e:
            return json.dumps({"error": str(e)})

    agent._tools.register(self_recent_tool_calls, name="self_recent_tool_calls",
        description="Return the last N tool calls this agent made (honest audit, no hallucination).")
    agent._tools.register(self_files_touched, name="self_files_touched",
        description="Return every file path this agent has read/written/edited this session.")
    agent._tools.register(self_session_summary, name="self_session_summary",
        description="Return a structured summary of this session: tool counts, files, meter, identity.")
    agent._tools.register(self_memory_search, name="self_memory_search",
        description="Search this agent's own memory tier for past context.")
    logger.info("Registered 4 self-introspection tools on agent %s", agent.name)
    return 4


# ─────────────────────────────────────────────────────────────────────────────
# AitherGraph tools (calls Genesis /api/graph/ — requires AitherOS running)
# ─────────────────────────────────────────────────────────────────────────────


def _graph_url() -> str:
    return os.getenv("AITHER_GENESIS_URL", "http://localhost:8001") + "/api/graph"


def _graph_request(
    method: str, path: str, params: dict | None = None,
    payload: dict | None = None, timeout: float = 30,
) -> str:
    """HTTP request to Genesis /api/graph/."""
    import httpx
    url = f"{_graph_url()}{path}"
    try:
        _tls = os.getenv("AITHER_TLS_VERIFY", "true").lower() != "false"
        with httpx.Client(timeout=timeout, verify=_tls) as c:
            if method == "GET":
                resp = c.get(url, params=params)
            else:
                resp = c.post(url, json=payload, params=params)
            resp.raise_for_status()
            return json.dumps(resp.json(), indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


def graph_search(query: str, domain: str = "", limit: int = 10) -> str:
    """Search across all AitherGraph domains (code, knowledge, events, memory).

    Args:
        query: What to search for.
        domain: Optional domain filter (code, knowledge, events, memory, etc.).
        limit: Max results.
    """
    params: dict = {"q": query, "limit": limit}
    if domain:
        params["domain"] = domain
    return _graph_request("GET", "/search", params=params)


def graph_code_search(query: str, limit: int = 10) -> str:
    """Search CodeGraph for functions, classes, and symbols.

    Args:
        query: Symbol name or search text.
        limit: Max results.
    """
    return _graph_request("GET", "/code/search", params={"q": query, "limit": limit})


def code_concept_search(query: str, limit: int = 5) -> str:
    """Token-cheap code lookup: concept cards (signature + call graph) only.

    ~30-40 tokens per result vs ~130 for full chunks — use this FIRST for
    "what calls X?" / "where does X live?" questions; fall back to
    graph_code_search when you actually need docstrings/bodies.

    Args:
        query: Symbol name or natural-language code question.
        limit: Max results (keep small — cards are dense).
    """
    import httpx
    base = os.getenv("AITHER_GENESIS_URL", "http://localhost:8001")
    url = f"{base}/codegraph/search"
    try:
        _tls = os.getenv("AITHER_TLS_VERIFY", "true").lower() != "false"
        with httpx.Client(timeout=15, verify=_tls) as c:
            resp = c.get(url, params={"q": query, "limit": limit, "compact": "true"})
            resp.raise_for_status()
            data = resp.json()
        return json.dumps({"count": data.get("count", 0), "results": data.get("results", [])})
    except Exception as e:
        return json.dumps({"error": str(e)})


def graph_kb_query(base_id: str, query: str, limit: int = 10) -> str:
    """RAG query against a knowledge base.

    Args:
        base_id: Knowledge base ID.
        query: Natural language query.
        limit: Max results.
    """
    return _graph_request(
        "POST", f"/knowledge/bases/{base_id}/query",
        params={"q": query, "limit": limit},
    )


def graph_memory_query(query: str, limit: int = 10) -> str:
    """Query the memory graph.

    Args:
        query: What to recall.
        limit: Max results.
    """
    return _graph_request(
        "POST", "/memory/query",
        payload={"query": query, "limit": limit},
    )


def graph_research(query: str, effort: str = "library_session") -> str:
    """Trigger autonomous research on a topic.

    Args:
        query: Research topic.
        effort: Depth (quick_glance, library_session, deep_dive, leave_no_stone).
    """
    return _graph_request(
        "POST", "/research",
        payload={"query": query, "effort": effort}, timeout=120,
    )


_GRAPH_TOOLS = [
    graph_search, graph_code_search, code_concept_search, graph_kb_query,
    graph_memory_query, graph_research,
]


def _init_graph_tools():
    """Lazily populate the graph category."""
    if not TOOL_CATEGORIES.get("graph"):
        TOOL_CATEGORIES["graph"] = _GRAPH_TOOLS


# ─────────────────────────────────────────────────────────────────────────
# Relay tools — the third of the aw family.
#
#   awgraph  what the code IS and what depends on what   (graph tools above)
#   awgit    what CHANGED and who is editing it          (tool_guards lease guard)
#   awrelay  who FOUND what and who still needs to hear it  (here)
#
# WHY THESE ARE BUILTINS RATHER THAN A PACK. An agent that finds something and
# cannot tell anyone has done half a job. escalate_to_human already covers "I need
# a DECISION"; this covers the far more common "here is what I found" — a different
# act with a different audience, and until now adk had no way to perform it.
#
# The names match the MCP tool names exactly (relay_send / relay_history /
# relay_channels) on purpose: an agent should not have to learn two vocabularies
# for one system depending on whether it is running in-fleet or standalone.
#
# AUTH: the caller's own token, never a platform credential. awrelay ships publicly,
# so an internal key would either fail for external callers or work for anyone who
# reads the source. Same rule as the IRC gateway.
# ─────────────────────────────────────────────────────────────────────────

def _relay_client(nick: str | None = None):
    """A RelayClient for this agent, or None when relay is not configured.

    Imported lazily: adk declares awrelay, but an agent whose task never touches
    messaging should not pay the import, and a missing package must degrade rather
    than take every tool listing down with it.
    """
    try:
        from awrelay.client import RelayClient
    except Exception:
        return None
    base = os.getenv("AITHERRELAY_URL", "https://relay.aitherium.com/api/relay")
    token = os.getenv("AITHER_RELAY_TOKEN") or os.getenv("AITHER_SESSION_BEARER")
    return RelayClient(base, token=token, nick=nick or os.getenv("AITHER_AGENT_NAME"))


def relay_channels() -> str:
    """List the relay channels this agent can see.

    Call before posting: joining a room that exists beats inventing one.
    """
    client = _relay_client()
    if client is None:
        return json.dumps({"error": "awrelay not available", "fix": "pip install awrelay"})
    try:
        return json.dumps(client.channels(), default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def relay_send(channel: str, text: str, kind: str = "FINDING",
               payload: dict | None = None, correlation_id: str = "") -> str:
    """Post a structured message to a relay channel, where humans can read it too.

    Args:
        channel: e.g. "#ops". Must exist — see relay_channels.
        text: the human-readable summary. REQUIRED and not decoration: humans share
            these rooms, and a message only a machine can read defeats the point.
        kind: FINDING (a concrete result, self-contained enough to act on without
            reading your session) | ALERT (something is WRONG) | MESSAGE | REQUEST
            (ask another agent to act; pair with correlation_id) | STEER | ACK.
        payload: structured data other agents can act on.
        correlation_id: ties a REQUEST to its ACK, or a follow-up FINDING to the
            original ask. Without it a reply cannot be matched to its question.
    """
    client = _relay_client()
    if client is None:
        return json.dumps({"error": "awrelay not available", "fix": "pip install awrelay"})
    try:
        from awrelay.envelope import Kind
        try:
            resolved = Kind[kind.upper()]
        except KeyError:
            # Name the valid set so the agent can retry, rather than silently
            # downgrading its FINDING to an ordinary chat line.
            return json.dumps({"error": f"unknown kind {kind!r}",
                               "valid": [k.name for k in Kind]})
        return json.dumps(client.send_text(
            channel=channel, text=text, kind=resolved,
            payload=payload or {}, correlation_id=correlation_id or None,
        ), default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def relay_history(channel: str, limit: int = 30, envelopes_only: bool = False) -> str:
    """Read recent messages from a channel.

    envelopes_only=True returns just the structured envelopes, skipping human
    chatter — useful when you want findings rather than conversation.
    """
    client = _relay_client()
    if client is None:
        return json.dumps({"error": "awrelay not available", "fix": "pip install awrelay"})
    try:
        return json.dumps(client.history(channel=channel, limit=limit,
                                         envelopes_only=envelopes_only), default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


_RELAY_TOOLS = [relay_channels, relay_send, relay_history]


def _init_relay_tools():
    """Lazily populate the relay category."""
    if not TOOL_CATEGORIES.get("relay"):
        TOOL_CATEGORIES["relay"] = _RELAY_TOOLS


# ─────────────────────────────────────────────────────────────────────────
# awrun — a priority-aware queue for agentic runs and ad-hoc CI builds, so an
# agent can manage its OWN work instead of hand-rolling a submit/poll loop
# (the exact pain that started this package: ~40 minutes lost to a queued
# rebuild with no way to move it up, plus a session of hand-typed
# `gh workflow run`/`gh run cancel`/polling).
#
# Same shape as the awrelay block above on purpose -- one vocabulary for
# "call a tool, get JSON back or a `{error, fix}` object", not two.
#
# AUTH note: `awrun_submit` with kind="comet-deploy" is trust-plane gated
# INSIDE awrun itself (awrun.authz — resolves AITHER_SESSION_BEARER, checks
# an awbac permission, writes an awdit audit record) — this wrapper does not
# duplicate that gate, it just surfaces whatever awrun.cli already decided.
# ─────────────────────────────────────────────────────────────────────────

def _queue_store():
    """A RunStore, or None when awrun is not installed (it is an EXTRA,
    `awdk[queue]` — see pyproject.toml). Imported lazily for the same
    reason as `_relay_client()`: an agent whose task never touches the run
    queue should not pay the import, and a missing package must degrade
    rather than take every tool listing down with it."""
    try:
        from awrun.store import RunStore
    except Exception:
        return None
    return RunStore()


def queue_submit(kind: str, priority: int = 0, paths: list[str] | None = None,
                  task: str = "", agent: str = "", adk_args: list[str] | None = None,
                  workflow: str = "", ref: str = "", inputs: dict | None = None,
                  service_name: str = "", target: str = "",
                  spec: dict | None = None) -> str:
    """Queue a new run. Higher `priority` runs first; equal priority is FIFO.

    Args:
        kind: "agent" (needs task + agent), "ci" (needs workflow), or
            "comet-deploy" (needs service_name -- trust-plane gated, see
            module note above; requires AITHER_SESSION_BEARER to resolve to
            an authorized session or this call is refused).
        paths: files this run will touch -- an agent run whose paths collide
            with another actor's live awgit lease is skipped by the
            dispatcher until the lease clears, rather than colliding.
        task, agent, adk_args: kind=agent fields.
        workflow, ref, inputs: kind=ci fields (ref defaults to develop).
        service_name, target, spec: kind=comet-deploy fields; `spec` merges
            in as the full AitherComet DeployRequest body, service_name/
            target override matching keys in it.
    """
    try:
        from awrun.store import RunError, RunStore
    except Exception:
        return json.dumps({"error": "awrun not available", "fix": "pip install awdk[queue]"})
    store = RunStore()
    built: dict = dict(spec or {})
    if kind == "agent":
        built["task"] = task
        built["agent"] = agent
        if adk_args:
            built["adk_args"] = adk_args
    elif kind == "ci":
        built["workflow"] = workflow
        built["ref"] = ref or "develop"
        built["inputs"] = inputs or {}
    elif kind == "comet-deploy":
        if service_name:
            built["service_name"] = service_name
        if target:
            built["target"] = target
        # Same gate `awrun submit --kind comet-deploy` runs on the CLI path --
        # called here too so an agent using the tool directly cannot skip it.
        from awrun import authz
        token = os.getenv("AITHER_SESSION_BEARER", "").strip()
        subject_id = authz.resolve_session(token)
        if not subject_id:
            authz.audit("comet-deploy-denied", reason="no resolved session", spec=built)
            return json.dumps({"error": "comet-deploy requires a resolved awiam session",
                               "fix": "set AITHER_SESSION_BEARER to a valid session token"})
        decision = authz.check_permission(subject_id)
        if not decision:
            authz.audit("comet-deploy-denied", subject=subject_id,
                        reason=decision.reason, spec=built)
            return json.dumps({"error": f"comet-deploy refused for {subject_id!r}: "
                                        f"{decision.reason}"})
        record = authz.audit("comet-deploy-submitted", subject=subject_id,
                             reason=decision.reason, spec=built)
        if record is None:
            return json.dumps({"error": "comet-deploy refused: could not write the "
                                        "audit record (spend must be auditable)"})
    try:
        item = store.submit(kind, built, priority=priority, paths=paths or [])
    except RunError as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps(item.to_dict(), default=str)


def queue_list(kind: str = "", include_closed: bool = False) -> str:
    """List queued runs, highest priority first. `kind` filters
    (agent/ci/comet-deploy); leave empty for all. `include_closed=True`
    also shows done/failed/cancelled runs, not just open ones."""
    store = _queue_store()
    if store is None:
        return json.dumps({"error": "awrun not available", "fix": "pip install awdk[queue]"})
    from awrun.store import OPEN_STATUSES
    statuses = None if include_closed else list(OPEN_STATUSES)
    items = store.list(statuses=statuses, kind=kind or None)
    return json.dumps([i.to_dict() for i in items], default=str)


def queue_status(run_id: str) -> str:
    """Full state of one run by id."""
    store = _queue_store()
    if store is None:
        return json.dumps({"error": "awrun not available", "fix": "pip install awdk[queue]"})
    try:
        item = store.get(run_id)
    except Exception as exc:
        # store.get() -> _locate() -> _validate_id() RAISES RunError on a
        # malformed id instead of returning None (unlike a merely-absent,
        # well-formed one, which IS a clean None). queue_bump/queue_cancel
        # already catch this; this function did not, so a malformed id
        # crashed the whole call with an unhandled exception instead of the
        # same clean {"error": ...} every other outcome here returns. Caught
        # live wiring the harness daemon's /awrun/status/{run_id} route,
        # which is exactly the caller that types a run_id straight from a
        # human/agent, malformed or not.
        return json.dumps({"error": str(exc)})
    if item is None:
        return json.dumps({"error": f"no such run: {run_id}"})
    return json.dumps(item.to_dict(), default=str)


def queue_bump(run_id: str, priority: int) -> str:
    """Change a queued/claimed run's priority -- this is how an urgent run
    jumps ahead of what is already waiting. Refused (not silently ignored)
    on a run that has already finished."""
    try:
        from awrun.store import RunError, RunStore
    except Exception:
        return json.dumps({"error": "awrun not available", "fix": "pip install awdk[queue]"})
    store = RunStore()
    try:
        item = store.bump(run_id, priority)
    except RunError as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps(item.to_dict(), default=str)


def queue_cancel(run_id: str) -> str:
    """Withdraw a queued/claimed run. Idempotent on an already-finished run
    (returns it unchanged rather than erroring)."""
    try:
        from awrun.store import RunError, RunStore
    except Exception:
        return json.dumps({"error": "awrun not available", "fix": "pip install awdk[queue]"})
    store = RunStore()
    try:
        item = store.cancel(run_id)
    except RunError as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps(item.to_dict(), default=str)


_QUEUE_TOOLS = [queue_submit, queue_list, queue_status, queue_bump, queue_cancel]


def _init_queue_tools():
    """Lazily populate the queue category."""
    if not TOOL_CATEGORIES.get("queue"):
        TOOL_CATEGORIES["queue"] = _QUEUE_TOOLS


# ─────────────────────────────────────────────────────────────────────────
# awnest — is there a human there, and can this agent prove one stood behind
# a change. Needs `pip install awdk[nest]`.
#
# WHY AN AGENT GETS THESE AT ALL. Two of the four are about being HONEST rather
# than getting in: an agent that meets a human-gated surface should declare what
# it is and take the agent door, not imitate a person. If the only way through a
# door is to be human, every automation is taught to imitate one, and the check
# has trained the thing it exists to detect.
#
# 🚨 THE ONE RULE THAT MAKES `nest_attest_commit` MEAN ANYTHING. The agent holds
# the signing secret in its own process, so it can mint whatever it likes — and a
# tool that let it mint "a human stood behind this" would make every commit
# attestation worthless the moment anyone noticed. So this mints verdict AGENT by
# default, and carries a HUMAN verdict across only when it is HANDED a human
# attestation it can verify. The claim is derived, never asserted.
# ─────────────────────────────────────────────────────────────────────────

def _nest_secret() -> "str | None":
    """The shared attestation secret, or None. Never defaulted: a default signing
    key is a key everybody has, including whoever is trying to get in."""
    return os.getenv("AWNEST_SECRET") or None


def nest_declare_agent(audience: str, reason: str = "") -> str:
    """Declare, honestly, that you are an agent at a gated door.

    Use this when a surface asks whether you are a person. Declaring costs you the
    human door and buys you the agent door — which is a real door, with its own
    limits, and is the path that stays open. Pretending is the path that gets the
    whole class of automation blocked.

    Args:
        audience: the door, e.g. "channel:#help" or "repo:acme/widgets".
        reason: what you are trying to do there. Shown to whoever runs the door.
    """
    try:
        from awnest import DECLARED_AGENT, Evidence, Verdict, assess
    except ImportError:
        return json.dumps({"error": "awnest not available", "fix": "pip install awdk[nest]"})
    a = assess([Evidence(DECLARED_AGENT, source="adk-agent", detail=reason)])
    return json.dumps({
        "audience": audience,
        "verdict": a.verdict.value,
        "declared": True,
        "admitted_if_door_allows_agents": a.verdict is Verdict.AGENT,
        "reasons": list(a.reasons),
    })


def nest_verify(token: str, audience: str, subject: str = "", context: str = "") -> str:
    """Verify an attestation somebody presented to YOU, against one door.

    `audience` is required and is the door you are guarding. Verifying without it
    accepts a claim earned somewhere else entirely, which is the whole attack.
    """
    try:
        from awnest import AttestationError, HmacKey, verify
    except ImportError:
        return json.dumps({"error": "awnest not available", "fix": "pip install awdk[nest]"})
    secret = _nest_secret()
    if not secret:
        return json.dumps({"error": "AWNEST_SECRET is not set",
                           "note": "refusing to verify with a key nobody configured"})
    try:
        att = verify(token, HmacKey(secret), audience=audience,
                     subject=subject or None, context=context or None)
    except (AttestationError, ValueError) as exc:
        # REFUSED is a result, not an error to swallow: the caller has to be able to
        # tell "this claim does not hold" from "the tool broke".
        return json.dumps({"admitted": False, "refused": str(exc)})
    return json.dumps({"admitted": True, "subject": att.sub, "audience": att.aud,
                       "verdict": att.verdict.value, "score": att.score,
                       "method": att.method, "age_s": int(att.age_s())})


def nest_attest_commit(repo: str, tree_sha: str, human_token: str = "",
                       ref: str = "", identity: str = "") -> str:
    """Produce the `Awnest-Attestation:` trailer for a commit you are about to make.

    Without `human_token` this attests what is true: an AGENT made this change.
    With one, it verifies that attestation and carries the person's identity and
    score across — so "a human stood behind this" is a claim you can only make when
    you can show the check that produced it.

    Args:
        repo: e.g. "acme/widgets".
        tree_sha: `git rev-parse HEAD^{tree}` — the CONTENT, not the commit sha,
            which cannot work because the trailer lives inside the commit.
        human_token: an attestation for this repo's door, if a person authorised it.
        ref: for a branch-specific door, e.g. "release".
        identity: who to name when there is no human token.
    """
    try:
        from awnest import (
            AttestationError,
            HmacKey,
            Verdict,
            attest_commit,
            trailer_line,
        )
        from awnest import verify as _verify
        from awnest.commit import repo_audience
    except ImportError:
        return json.dumps({"error": "awnest not available", "fix": "pip install awdk[nest]"})
    secret = _nest_secret()
    if not secret:
        return json.dumps({"error": "AWNEST_SECRET is not set"})
    key = HmacKey(secret)

    verdict, score = Verdict.AGENT, None
    subject, method = (identity or "adk-agent"), "declared"
    if human_token:
        try:
            att = _verify(human_token, key, audience=repo_audience(repo, ref or None))
        except (AttestationError, ValueError) as exc:
            return json.dumps({"error": f"the human attestation does not hold: {exc}",
                               "note": "refusing to claim a person stood behind this"})
        if att.verdict is not Verdict.HUMAN:
            return json.dumps({"error": f"that attestation records {att.verdict.value}, "
                                        "not a human"})
        verdict, score, subject, method = att.verdict, att.score, att.sub, att.method

    try:
        token = attest_commit(key, identity=subject, tree_sha=tree_sha, repo=repo,
                              ref=ref or None, verdict=verdict, score=score, method=method)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps({"trailer": trailer_line(token), "verdict": verdict.value,
                       "identity": subject, "score": score})


def nest_verify_commit(message: str, repo: str, tree_sha: str, ref: str = "") -> str:
    """Read a commit's trailer and check it against the CONTENT in front of you.

    The content comparison is the part that matters: a valid attestation lifted off
    a different commit verifies perfectly, and only the tree check notices.
    """
    try:
        from awnest import AttestationError, HmacKey, verify_commit
    except ImportError:
        return json.dumps({"error": "awnest not available", "fix": "pip install awdk[nest]"})
    secret = _nest_secret()
    if not secret:
        return json.dumps({"error": "AWNEST_SECRET is not set"})
    try:
        att = verify_commit(message, HmacKey(secret), repo=repo, tree_sha=tree_sha,
                            ref=ref or None)
    except (AttestationError, ValueError) as exc:
        return json.dumps({"attested": False, "refused": str(exc)})
    return json.dumps({"attested": True, "identity": att.sub, "verdict": att.verdict.value,
                       "score": att.score, "content": att.ctx, "method": att.method})


_NEST_TOOLS = [nest_declare_agent, nest_verify, nest_attest_commit, nest_verify_commit]


def _init_nest_tools():
    """Lazily populate the nest category."""
    if not TOOL_CATEGORIES.get("nest"):
        TOOL_CATEGORIES["nest"] = _NEST_TOOLS


# ─────────────────────────────────────────────────────────────────────────
# awbrowse — portable browser client for agents
# ─────────────────────────────────────────────────────────────────────────

def browse_page(url: str, action: str = "read") -> str:
    """Navigate and inspect a web page.

    Args:
        url: The page to navigate to
        action: "read" to extract content, "screenshot" to capture visual, "dom" for structure

    This tool requires pip install awdk[senses] and an AitherBrowser-shaped service.
    """
    try:
        from awbrowse import Browser
    except ImportError:
        return json.dumps({"error": "awbrowse not available", "fix": "pip install awdk[senses]"})

    browser = Browser()
    try:
        browser.navigate(url)
        if action == "read":
            return json.dumps({"url": url, "content": browser.get_text()})
        elif action == "screenshot":
            return json.dumps({"url": url, "screenshot": browser.screenshot()})
        elif action == "dom":
            return json.dumps({"url": url, "dom": browser.get_dom()})
        else:
            return json.dumps({"error": f"unknown action: {action}"})
    except Exception as e:
        return json.dumps({"error": str(e), "url": url})
    finally:
        browser.close()


def browse_fill_form(url: str, fields: dict) -> str:
    """Fill and submit a form on a page.

    Args:
        url: The page containing the form
        fields: Dict of field names and values to fill
    """
    try:
        from awbrowse import Browser
    except ImportError:
        return json.dumps({"error": "awbrowse not available", "fix": "pip install awdk[senses]"})

    browser = Browser()
    try:
        browser.navigate(url)
        for field_name, value in fields.items():
            browser.fill_field(field_name, value)
        browser.submit()
        return json.dumps({"url": url, "submitted": True, "result": browser.get_text()})
    except Exception as e:
        return json.dumps({"error": str(e), "url": url})
    finally:
        browser.close()


_BROWSE_TOOLS = [browse_page, browse_fill_form]


def _init_browse_tools():
    """Lazily populate the browse category."""
    if not TOOL_CATEGORIES.get("browse"):
        TOOL_CATEGORIES["browse"] = _BROWSE_TOOLS


# ─────────────────────────────────────────────────────────────────────────
# awfind — portable search client for agents
# ─────────────────────────────────────────────────────────────────────────

def find_search(query: str, limit: int = 5) -> str:
    """Search the web and get ranked results.

    Args:
        query: What to search for
        limit: Maximum number of results to return (default 5)

    This tool requires pip install awdk[senses] and a search-shaped service.
    Returns ranked results sorted by relevance.
    """
    try:
        from awfind import Finder
    except ImportError:
        return json.dumps({"error": "awfind not available", "fix": "pip install awdk[senses]"})

    finder = Finder()
    try:
        results = finder.search(query, limit=limit)
        return json.dumps({
            "query": query,
            "count": len(results),
            "results": [
                {"title": r.title, "url": r.url, "snippet": r.snippet, "rank": i+1}
                for i, r in enumerate(results)
            ]
        })
    except Exception as e:
        return json.dumps({"error": str(e), "query": query})


_FIND_TOOLS = [find_search]


def _init_find_tools():
    """Lazily populate the find category."""
    if not TOOL_CATEGORIES.get("find"):
        TOOL_CATEGORIES["find"] = _FIND_TOOLS


# ─────────────────────────────────────────────────────────────────────────
# Workspace Intelligence tools — people analytics, meetings, email, collab
# ─────────────────────────────────────────────────────────────────────────

def _wi_base_url() -> str:
    """Resolve workspace intelligence base URL."""
    return os.getenv(
        "ADK_APP_PROXY_URL",
        os.getenv("AITHER_GENESIS_URL", "http://localhost:8001"),
    ).rstrip("/")


def _wi_get(path: str, params: dict | None = None) -> str:
    """GET workspace intelligence endpoint with graceful degradation."""
    import httpx
    url = f"{_wi_base_url()}{path}"
    try:
        resp = httpx.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            return json.dumps(resp.json(), indent=2)
        return json.dumps({"error": f"HTTP {resp.status_code}", "detail": resp.text[:300]})
    except Exception as e:
        return json.dumps({"error": str(e)})


def workspace_health(days: int = 7) -> str:
    """Get workspace health score and engagement metrics.

    Returns composite health score (0-100), active users, engagement rate,
    meeting/email/message volume, activity trends, and top contributors.

    Args:
        days: Number of days to analyze (default 7)
    """
    return _wi_get("/api/workspace-intelligence/health", {"days": days})


def email_intelligence(days: int = 30) -> str:
    """Analyze email patterns: top senders, categories, busiest hours.

    Provides sender frequency, VIP/urgent/approval/FYI categorization,
    peak activity hours, and thread depth analysis.

    Args:
        days: Number of days to analyze (default 30)
    """
    return _wi_get("/api/workspace-intelligence/email-intelligence", {"days": days})


def meeting_intelligence(days: int = 30) -> str:
    """Analyze meeting patterns: time spent, types, top collaborators, free time.

    Shows total hours in meetings, type breakdown (1:1, team, all-hands),
    top meeting partners, busiest days, and free time percentage.

    Args:
        days: Number of days to analyze (default 30)
    """
    return _wi_get("/api/workspace-intelligence/meeting-intelligence", {"days": days})


def collaboration_signals(days: int = 30) -> str:
    """Get team collaboration metrics: interaction density, pairs, silos.

    Analyzes cross-channel collaboration from meetings, emails, messages.
    Identifies strongest pairs, communication flow, and potential silos.

    Args:
        days: Number of days to analyze (default 30)
    """
    return _wi_get("/api/workspace-intelligence/collaboration", {"days": days})


def person_intelligence(person_id: str, days: int = 30) -> str:
    """Get engagement and activity profile for a specific person.

    Returns engagement score, email volume, meetings, activity trends,
    and top contacts for the specified person.

    Args:
        person_id: Person identifier (email or user ID)
        days: Number of days to analyze (default 30)
    """
    return _wi_get(f"/api/workspace-intelligence/person/{person_id}", {"days": days})


def relationship_insights() -> str:
    """Get network relationship analytics from the social graph.

    Returns relationship type distribution, network density,
    key connectors, and isolated members needing support.
    """
    return _wi_get("/api/workspace-intelligence/relationships")


def post_to_social(text: str, platforms: str = "bluesky,linkedin") -> str:
    """Create a social media post across selected platforms.

    Args:
        text: Post content
        platforms: Comma-separated platform names (default: bluesky,linkedin)
    """
    import httpx
    url = f"{_wi_base_url()}/api/social/posts/draft"
    plat_list = [p.strip() for p in platforms.split(",") if p.strip()]
    try:
        resp = httpx.post(url, json={"text": text, "platforms": plat_list}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            post_id = data.get("id", "")
            if post_id:
                httpx.post(f"{_wi_base_url()}/api/social/posts/{post_id}/publish", timeout=10)
            return json.dumps(data, indent=2)
        return json.dumps({"error": f"HTTP {resp.status_code}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


def social_analytics(days: int = 30) -> str:
    """Get social media engagement metrics and top posts.

    Args:
        days: Number of days to analyze (default 30)
    """
    return _wi_get("/api/social/analytics", {"days": days})


def executive_briefing() -> str:
    """Get unified morning briefing: calendar, priority emails, tasks, messages.

    Returns today's events, next meeting countdown, priority items
    (VIP emails, urgent tasks, imminent meetings), and unread counts.
    """
    return _wi_get("/api/executive/briefing")


def meeting_prep(event_id: str, title: str = "") -> str:
    """Prepare for a meeting: talking points, related docs, attendee context.

    Args:
        event_id: Calendar event ID to prepare for
        title: Meeting title for additional context
    """
    import httpx
    url = f"{_wi_base_url()}/api/executive/meeting/prep"
    try:
        resp = httpx.post(url, json={"event_id": event_id, "title": title}, timeout=15)
        if resp.status_code == 200:
            return json.dumps(resp.json(), indent=2)
        return json.dumps({"error": f"HTTP {resp.status_code}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


def email_triage(email_id: str, subject: str, sender: str, body: str) -> str:
    """Categorize an email by importance: VIP, urgent, approval, or FYI.

    Args:
        email_id: Email identifier
        subject: Email subject line
        sender: Sender email address
        body: Email body text
    """
    import httpx
    url = f"{_wi_base_url()}/api/executive/email/triage"
    try:
        resp = httpx.post(url, json={
            "email_id": email_id, "subject": subject,
            "sender": sender, "body": body,
        }, timeout=10)
        if resp.status_code == 200:
            return json.dumps(resp.json(), indent=2)
        return json.dumps({"error": f"HTTP {resp.status_code}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


def rag_search(query: str, top_k: int = 5) -> str:
    """Search document knowledge base for relevant content.

    Args:
        query: Search query
        top_k: Number of results (default 5)
    """
    import httpx
    url = f"{_wi_base_url()}/api/documents/search"
    try:
        resp = httpx.post(url, json={"query": query, "top_k": top_k}, timeout=10)
        return resp.text if resp.status_code == 200 else json.dumps({"error": f"HTTP {resp.status_code}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


def staff_search(query: str) -> str:
    """Search staff/team members by skills, role, department, or name.

    Args:
        query: Search criteria (name, skill, role, etc.)
    """
    import httpx
    url = f"{_wi_base_url()}/api/people/search"
    try:
        resp = httpx.post(url, json={"query": query}, timeout=10)
        return resp.text if resp.status_code == 200 else json.dumps({"error": f"HTTP {resp.status_code}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


def generate_document(document_type: str, instructions: str = "", context: str = "") -> str:
    """Generate a formatted document (report, summary, proposal, etc.).

    Args:
        document_type: Type of document (report, summary, proposal, resume, etc.)
        instructions: Additional generation instructions
        context: Background context for the document
    """
    import httpx
    url = f"{_wi_base_url()}/api/content/generate"
    try:
        resp = httpx.post(url, json={
            "document_type": document_type,
            "instructions": instructions, "context": context,
        }, timeout=30)
        return resp.text if resp.status_code == 200 else json.dumps({"error": f"HTTP {resp.status_code}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


# Domain tags for workspace tools — matches capability_domains.yaml
_WORKSPACE_TOOL_DOMAINS: dict[str, str] = {
    "workspace_health": "workspace_intelligence",
    "email_intelligence": "workspace_intelligence",
    "meeting_intelligence": "workspace_intelligence",
    "collaboration_signals": "workspace_intelligence",
    "person_intelligence": "workspace_intelligence",
    "relationship_insights": "workspace_intelligence",
    "post_to_social": "social_marketing",
    "social_analytics": "social_marketing",
    "executive_briefing": "executive_assistant",
    "meeting_prep": "executive_assistant",
    "email_triage": "executive_assistant",
    "rag_search": "documents",
    "staff_search": "people",
    "generate_document": "documents",
}

_WORKSPACE_TOOLS = [
    # Intelligence
    workspace_health, email_intelligence, meeting_intelligence,
    collaboration_signals, person_intelligence, relationship_insights,
    # Social / Marketing
    post_to_social, social_analytics,
    # Executive Assistant
    executive_briefing, meeting_prep, email_triage,
    # Documents / Knowledge
    rag_search, staff_search, generate_document,
]


def _init_workspace_tools():
    """Lazily populate the workspace category.

    Respects AITHER_ENABLED_DOMAINS env var (comma-separated).
    If unset, all workspace tools are registered.
    """
    if TOOL_CATEGORIES.get("workspace"):
        return
    enabled_raw = os.getenv("AITHER_ENABLED_DOMAINS", "")
    if enabled_raw:
        enabled = {d.strip() for d in enabled_raw.split(",") if d.strip()}
        TOOL_CATEGORIES["workspace"] = [
            fn for fn in _WORKSPACE_TOOLS
            if _WORKSPACE_TOOL_DOMAINS.get(fn.__name__, "") in enabled
        ]
    else:
        TOOL_CATEGORIES["workspace"] = _WORKSPACE_TOOLS


# ─────────────────────────────────────────────────────────────────────────
# Safety & Escalation tools
# ─────────────────────────────────────────────────────────────────────────

async def escalate_to_human(
    reason: str,
    action: str = "",
    urgency: str = "medium",
) -> str:
    """Request human review for uncertain or risky actions.

    Use when you're about to perform an irreversible action, when
    confidence is low, or when the task involves sensitive systems.

    Args:
        reason: Why escalation is needed
        action: The action requiring approval (e.g. deploy.production)
        urgency: low, medium, high, or critical
    """
    # Try AitherOS escalation endpoint first
    try:
        from adk.aither_bridge import get_bridge
        bridge = get_bridge()
        if bridge and bridge.connected:
            result = await bridge.post(
                "/escalations/create",
                {"reason": reason, "action_type": action, "urgency": urgency},
            )
            return json.dumps(result)
    except Exception:
        pass

    # Standalone: raise a DECISION CARD. This used to be a log line plus
    # "status": "logged_locally", which is the silent-no-op pattern living in
    # the one tool whose entire purpose is to reach a person — the agent
    # reported an escalation, the owner was told nothing, and the run either
    # stalled or guessed. A card is durable, raises a window the owner can
    # click, and delivers the answer back to this session.
    logger.warning("[ESCALATION] %s | action=%s urgency=%s", reason, action, urgency)
    try:
        from adk.decisions.agent_tools import raise_card

        card = raise_card(
            (action or "Approval needed")[:120],
            summary=reason,
            kind="blocked",
            # This tool's vocabulary is low/medium/high/critical; the card's is
            # low/normal/high/critical. Mapping it wrong would silently downgrade
            # every "medium" escalation to the quietest tier.
            urgency={"medium": "normal"}.get(urgency, urgency if urgency in
                                             ("low", "normal", "high", "critical")
                                             else "high"),
            options=[
                {"key": "approve", "label": "Approve — go ahead",
                 "consequence": f"I perform: {action or reason}"},
                {"key": "deny", "label": "Deny — do not do it",
                 "consequence": "I stop and report what I was about to do."},
            ],
            recommend="deny",
            default="deny",  # fail-closed: no answer must never mean approval
        )
    except Exception as exc:  # noqa: BLE001 - escalation must not kill the turn
        return json.dumps({
            "status": "escalation_failed",
            "error": str(exc),
            "reason": reason,
            "action": action,
            "guidance": (
                "The escalation could NOT be raised to the owner. Do not treat "
                "this as approval — stop and report that you could not ask."
            ),
        })
    return json.dumps({
        "status": "awaiting_human",
        "card_id": card.id,
        "reason": reason,
        "action": action,
        "urgency": card.urgency,
        "default_if_unanswered": card.default_key,
        "guidance": (
            f"Raised decision card {card.id} to the owner. Poll it with "
            f"check_human('{card.id}'), or call ask_human(..., wait_seconds=N) "
            f"next time if you need to block. If nobody answers, the declared "
            f"default is '{card.default_key}' — which is DENY."
        ),
    })


async def check_safety_gate(
    tool_name: str,
    args: str = "{}",
) -> str:
    """Check if a tool call is allowed by safety gates.

    Call this before performing risky operations to verify
    the action is within your permission level.

    Args:
        tool_name: Name of the tool to check
        args: JSON string of tool arguments
    """
    from adk.safety import ActionGate, GateDecision
    gate = ActionGate()
    action_type = gate.classify_tool(tool_name)
    try:
        parsed_args = json.loads(args) if args else {}
    except json.JSONDecodeError:
        parsed_args = {}
    result = gate.check(action_type, parsed_args)
    return json.dumps({
        "decision": result.decision.value,
        "reason": result.reason,
        "action_type": action_type,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Structured-data ML (TabFM tabular + TimesFM time-series) — zero-shot inference.
# These are NON-LLM foundation models served by a structured-ML inference service
# (default :8192). The tools POST directly to that service, resolved from the
# AITHER_STRUCTURED_ML_URL env var (set this to your deployment's service URL).
# The agent-side gate is pack-domain enablement — only identities with the
# "structured_ml" category get these tools. "Training" here is in-context: hand
# over a labeled support set, get predictions in one forward pass — no gradient step.
# ─────────────────────────────────────────────────────────────────────────────


_STRUCTURED_ML_DEFAULT_URL = "http://localhost:8192"


def _structured_ml_url() -> str:
    """Resolve the structured-ML service base URL from env, http/https only.

    Defense-in-depth: reject any URL whose scheme isn't http/https so a mis-set
    value can't redirect the tool to a file://, gopher:// (etc.) target. A
    private/loopback host is allowed here (unlike web_fetch) — the inference
    service is a trusted deployment endpoint set via AITHER_STRUCTURED_ML_URL.
    """
    url = os.getenv("AITHER_STRUCTURED_ML_URL", _STRUCTURED_ML_DEFAULT_URL).strip()
    if not url.lower().startswith(("http://", "https://")):
        return _STRUCTURED_ML_DEFAULT_URL
    return url


# Cap the JSON we hand back to the model — a big query batch can return a large
# predictions/probabilities matrix that would bloat the agent's context. Beyond
# this, we return a compact summary (counts + a small sample) instead of the raw
# blob, and tell the agent how to narrow the request.
_STRUCTURED_ML_MAX_RESP_CHARS = 20_000


def _structured_ml_post(path: str, payload: dict, timeout: float = 90.0) -> str:
    import httpx

    url = f"{_structured_ml_url()}{path}"
    try:
        _tls = os.getenv("AITHER_TLS_VERIFY", "true").lower() != "false"
        with httpx.Client(timeout=timeout, verify=_tls) as c:
            resp = c.post(url, json=payload)
            if resp.status_code >= 400:
                try:
                    detail = resp.json().get("detail", resp.text)
                except Exception:  # noqa: BLE001 - fall back to raw body
                    detail = resp.text
                return json.dumps({"error": f"HTTP {resp.status_code}: {detail}"})
            return _cap_response(resp.json())
    except Exception as e:  # noqa: BLE001 - surface transport errors to the agent
        return json.dumps({"error": str(e)})


def _cap_response(data: dict) -> str:
    """Serialise a service response, summarising it if it would bloat context."""
    full = json.dumps(data)
    if len(full) <= _STRUCTURED_ML_MAX_RESP_CHARS:
        return full
    preds = data.get("predictions") if isinstance(data, dict) else None
    summary: dict = {
        "truncated": True,
        "note": (
            "Response too large to return in full; showing a summary. Re-run on a "
            "smaller batch of query_rows/series to get complete per-row output."
        ),
    }
    if isinstance(preds, list):
        summary["n_predictions"] = len(preds)
        summary["predictions_sample"] = preds[:50]
    else:
        summary["keys"] = list(data.keys()) if isinstance(data, dict) else None
    return json.dumps(summary)


def tabular_classify(
    support_rows: list[dict[str, Any]], target: str, query_rows: list[dict[str, Any]]
) -> str:
    """Classify tabular rows with TabFM (zero-shot in-context; up to 10 classes).

    Hand over a few labeled examples as the support set and get predicted classes +
    probabilities for the query rows — no training step. Use this to adapt to a new
    labeled dataset on the fly instead of getting stuck.

    Args:
        support_rows: Labeled examples; each is a dict of features INCLUDING the target column.
        target: Name of the label column present in support_rows.
        query_rows: Rows (dicts of features) to classify.
    """
    return _structured_ml_post(
        "/tabular/classify",
        {"support_rows": support_rows, "target": target, "query_rows": query_rows},
    )


def tabular_regress(
    support_rows: list[dict[str, Any]], target: str, query_rows: list[dict[str, Any]]
) -> str:
    """Predict a numeric target for tabular rows with TabFM (zero-shot in-context).

    Args:
        support_rows: Labeled examples; each is a dict of features INCLUDING the numeric target column.
        target: Name of the numeric target column present in support_rows.
        query_rows: Rows (dicts of features) to predict.
    """
    return _structured_ml_post(
        "/tabular/regress",
        {"support_rows": support_rows, "target": target, "query_rows": query_rows},
    )


def timeseries_forecast(series: list[float], horizon: int) -> str:
    """Forecast future values of a time series with TimesFM (zero-shot).

    Args:
        series: History as a list of numbers. (Several series at once — a list of
            lists — is also accepted by the service for batch forecasting.)
        horizon: Number of future steps to forecast.
    """
    return _structured_ml_post(
        "/timeseries/forecast", {"series": series, "horizon": horizon}
    )


def tabular_teach(
    task: str, labeled_rows: list[dict[str, Any]], target: str, mode: str = "classify"
) -> str:
    """Teach a tabular task new labeled examples so it adapts to new data.

    Accumulates a per-task support set (kept under YOUR tenant) and only keeps the
    new rows if held-out accuracy doesn't regress (accept-or-rollback). This is how
    an agent gets better at a classification/regression task over time WITHOUT any
    training run — next `tabular_classify` on the same task can reuse what was taught.

    Args:
        task: A stable task name (e.g. "lead-scoring"); namespaced under your tenant.
        labeled_rows: New labeled examples; each a dict of features INCLUDING the target column.
        target: Name of the target column present in labeled_rows.
        mode: "classify" (default) or "regress".
    """
    import httpx

    url = f"{_genesis_url()}/ml/teach"
    payload = {"task": task, "labeled_rows": labeled_rows, "target": target, "mode": mode}
    try:
        _tls = os.getenv("AITHER_TLS_VERIFY", "true").lower() != "false"
        with httpx.Client(timeout=90.0, verify=_tls) as c:
            resp = c.post(url, json=payload)
            if resp.status_code >= 400:
                try:
                    detail = resp.json().get("detail", resp.text)
                except Exception:  # noqa: BLE001
                    detail = resp.text
                return json.dumps({"error": f"HTTP {resp.status_code}: {detail}"})
            return _cap_response(resp.json())
    except Exception as e:  # noqa: BLE001 - surface transport errors to the agent
        return json.dumps({"error": str(e)})


def _genesis_url() -> str:
    url = os.getenv("AITHER_GENESIS_URL", "http://localhost:8001").strip()
    if not url.lower().startswith(("http://", "https://")):
        return "http://localhost:8001"
    return url


# ─────────────────────────────────────────────────────────────────────────────
# Registration
# ─────────────────────────────────────────────────────────────────────────────

# Intent-aware tool categorization (for filtering by intent type)
# Maps tool functions to the intent types they're available for.
# Empty list = available for all intents (fail-open).
TOOL_INTENT_CATEGORIES = {
    # File I/O tools → code, file, analysis intents
    file_read: ["code", "file", "analysis"],
    file_write: ["code", "file"],
    file_edit: ["code", "file"],
    file_list: ["code", "file"],
    file_search: ["code", "file", "analysis"],
    # Shell execution → code, command intents
    shell_exec: ["code", "command"],
    # Python execution → code, analysis intents
    python_exec: ["code", "analysis"],
    # Web tools → research, web_research, question intents
    web_search: ["research", "web_research", "question"],
    web_fetch: ["research", "web_research"],
    # Secrets management → all intents (security-relevant)
    secret_get: [],
    secret_set: [],
    secret_list: [],
    # Creative tools → all intents
    image_generate: [],
    image_refine: [],
    image_smart: [],
    # Git tools → code, analysis intents
    git_status: ["code", "analysis"],
    git_diff: ["code", "analysis"],
    git_log: ["code", "analysis"],
    git_add: ["code"],
    git_commit: ["code"],
    git_branch_list: ["code", "analysis"],
    # Code analysis → code, analysis intents
    code_search: ["code", "analysis"],
    code_symbols: ["code", "analysis"],
    repowise_search: ["code", "analysis"],
    swarm_code: ["code", "analysis"],
    # ML tools → analysis, research intents
    tabular_classify: ["analysis", "research"],
    tabular_regress: ["analysis", "research"],
    timeseries_forecast: ["analysis", "research"],
    tabular_teach: ["analysis", "research"],
    # Safety tools → all intents
    escalate_to_human: [],
    check_safety_gate: [],
}

# Agent Notebook tools are registered lazily (they proxy the Genesis /notebooks/*
# router) — their intent categories are attached in _init_notebook_tools().

# Vectorless tree search over long Markdown documents (PageIndex-style; adk/graph_rag).
from adk.graph_rag.page_index import doc_tree_search  # noqa: E402

# Tool category definitions
TOOL_CATEGORIES: dict = {
    "file_io": [file_read, file_write, file_edit, file_list, file_search],
    "shell": [shell_exec],
    "python": [python_exec],
    "web": [web_search, web_fetch],
    "documents": [doc_tree_search],
    "secrets": [secret_get, secret_set, secret_list],
    "creative": [image_generate, image_refine, image_smart],
    "git": [git_status, git_diff, git_log, git_add, git_commit, git_branch_list],
    "code": [code_search, code_symbols],
    "repowise": [repowise_search],
    "swarm": [swarm_code],
    "structured_ml": [tabular_classify, tabular_regress, timeseries_forecast, tabular_teach],
    "graph": [],  # populated lazily by _init_graph_tools()
    "safety": [escalate_to_human, check_safety_gate],
    "workspace": [],  # populated lazily by _init_workspace_tools()
    # "self" is registered via register_self_tools(agent) (closures over agent state),
    # not a flat function list — see register_builtin_tools below.
    "self": [],
    "voice": [],  # populated lazily by _init_voice_tools()
    "formbridge": [],  # populated lazily by _init_formbridge_tools()
    "notebooks": [],  # populated lazily by _init_notebook_tools()
    "persona": [],  # populated lazily by _init_persona_tools()
    "decisions": [],  # populated lazily by _init_decision_tools()
    "relay": [],  # populated lazily by _init_relay_tools()
    "queue": [],  # populated lazily by _init_queue_tools()
    "nest": [],  # populated lazily by _init_nest_tools()
    "browse": [],  # populated lazily by _init_browse_tools()
    "find": [],  # populated lazily by _init_find_tools()
}

# Default categories for common identity profiles
# Every identity gets "self" by default — self-introspection is universally safe and
# directly addresses Reddit pain ("I asked the agent what it did and it lied").
#: Named toolpacks: a small, task-shaped subset a process can ask for by one word in
#: ADK_BUILTIN_TOOL_CATEGORIES (mixed with plain category names). "coding" is the set
#: the SWE-bench harness measured as sufficient for a fix (file, shell, python, git,
#: code navigation) plus the decision channel every agent keeps.
TOOLPACK_ALIASES: dict[str, list[str]] = {
    "coding": ["file_io", "shell", "python", "git", "code", "decisions"],
    "minimal": ["file_io", "decisions"],
    "research": ["file_io", "web", "browse", "find", "decisions"],
}


def categories_from_env(value: str) -> list[str] | None:
    """Parse ADK_BUILTIN_TOOL_CATEGORIES: a comma list of category names and/or toolpack
    aliases. Empty/unset -> None (identity default). Unknown names are dropped with a
    warning; an entry that resolves to NOTHING is also None, so a typo can never leave an
    agent with zero tools silently."""
    names = [n.strip() for n in (value or "").split(",") if n.strip()]
    if not names:
        return None
    out: list[str] = []
    for name in names:
        if name in TOOLPACK_ALIASES:
            out.extend(c for c in TOOLPACK_ALIASES[name] if c not in out)
        elif name in TOOL_CATEGORIES or name == "self":
            if name not in out:
                out.append(name)
        else:
            logger.warning("ADK_BUILTIN_TOOL_CATEGORIES: unknown category/toolpack %r ignored",
                           name)
    return out or None


IDENTITY_DEFAULTS = {
    "adk-daemon": [
        "file_io", "shell", "python", "web", "git", "code", "repowise", "swarm", "graph",
        "workspace", "notebooks", "safety", "self", "decisions"
    ],
    "demiurge": [
        "file_io", "shell", "python", "web", "git", "code", "repowise", "swarm", "graph",
        "workspace", "notebooks", "safety", "self", "decisions"
    ],
    "analyst": [
        "file_io", "web", "python", "code", "graph", "structured_ml", "workspace", "notebooks",
        "safety", "self", "decisions"
    ],
    "atlas": [
        "file_io", "web", "secrets", "code", "graph", "workspace", "notebooks", "safety",
        "self", "decisions"
    ],
    "aither": ["file_io", "shell", "web", "creative", "self", "decisions"],
    "lyra": ["file_io", "web", "graph", "workspace", "voice", "safety", "self", "decisions"],
    "hydra": [
        "file_io", "shell", "python", "git", "code", "repowise", "graph", "workspace", "safety",
        "self", "decisions"
    ],
    "prometheus": [
        "file_io", "shell", "secrets", "git", "workspace", "safety", "self", "decisions"
    ],
    "apollo": [
        "file_io", "shell", "python", "code", "repowise", "graph", "workspace", "safety",
        "self", "decisions"
    ],
    "athena": [
        "file_io", "web", "secrets", "code", "graph", "workspace", "safety", "self", "decisions"
    ],
    "scribe": [
        "file_io", "web", "code", "repowise", "graph", "workspace", "safety", "self",
        "decisions"
    ],
    "iris": ["file_io", "web", "creative", "workspace", "voice", "safety", "self", "decisions"],
    "muse": ["file_io", "web", "creative", "workspace", "voice", "safety", "self", "decisions"],
}


def _init_voice_tools():
    """Lazily populate the voice category.

    Voice tools are optional (require voice-local or voice-cloud extras).
    This function is called once at tool registration time.
    """
    if TOOL_CATEGORIES.get("voice"):
        return  # Already initialized
    try:
        # say_to_file (returns a path string), NOT say (returns raw bytes that would
        # be JSON-stringified into the model's context).
        from adk.builtin_tools_voice import hear, say_to_file, analyze_voice_emotion
        TOOL_CATEGORIES["voice"] = [hear, say_to_file, analyze_voice_emotion]
        logger.info("Voice tools initialized (hear, say_to_file, analyze_voice_emotion)")
    except ImportError:
        logger.debug("Voice tools not available; voice category remains empty")
        TOOL_CATEGORIES["voice"] = []


def _init_formbridge_tools():
    """Lazily populate the formbridge category (local form automation).

    Redacting-by-construction tools — results carry field NAMES/counts/job
    ids, never captured values (see adk/formbridge/tools.py).
    """
    if TOOL_CATEGORIES.get("formbridge"):
        return  # Already initialized
    try:
        from adk.formbridge.tools import (
            formbridge_fill_form,
            formbridge_list_patients,
            formbridge_pack_health,
            formbridge_purge,
        )
        TOOL_CATEGORIES["formbridge"] = [
            formbridge_list_patients,
            formbridge_fill_form,
            formbridge_pack_health,
            formbridge_purge,
        ]
        logger.info("FormBridge tools initialized (list/fill/health/purge)")
    except ImportError:
        logger.debug("FormBridge tools not available; category remains empty")
        TOOL_CATEGORIES["formbridge"] = []


def _init_notebook_tools():
    """Lazily populate the notebooks category (Agent Notebook proxy tools).

    These proxy the Genesis /notebooks/* router — plan, run, inspect, and export
    ``.anb`` Agent Notebooks. They no-op cleanly if the module can't be imported.
    """
    if TOOL_CATEGORIES.get("notebooks"):
        return  # Already initialized
    try:
        from adk.notebook_tools import NOTEBOOK_TOOLS
        TOOL_CATEGORIES["notebooks"] = list(NOTEBOOK_TOOLS)
        # Notebook orchestration is code/analysis work.
        for fn in NOTEBOOK_TOOLS:
            TOOL_INTENT_CATEGORIES.setdefault(fn, ["code", "analysis"])
        logger.info("Agent Notebook tools initialized (%d tools)", len(NOTEBOOK_TOOLS))
    except ImportError:
        logger.debug("Notebook tools not available; notebooks category remains empty")
        TOOL_CATEGORIES["notebooks"] = []


def _init_decision_tools():
    """Lazily populate the decisions category (ask the owner, structured).

    This is the channel an ADK agent previously did not have. Standalone,
    ``escalate_to_human`` wrote a log line and returned "logged_locally" — the
    owner was never told anything. Cards are durable, they raise a window the
    owner can click, and the answer is delivered back to the raising session.
    """
    if TOOL_CATEGORIES.get("decisions"):
        return  # Already initialized
    try:
        from adk.decisions.agent_tools import DECISION_TOOLS
        TOOL_CATEGORIES["decisions"] = list(DECISION_TOOLS)
        for fn in DECISION_TOOLS:
            TOOL_INTENT_CATEGORIES.setdefault(fn, [])
        logger.info("Decision-card tools initialized (%d tools)", len(DECISION_TOOLS))
    except ImportError:
        logger.debug("Decision tools not available; decisions category remains empty")
        TOOL_CATEGORIES["decisions"] = []


def _init_persona_tools():
    """Lazily populate the persona category (desktop avatar bridge).

    Persona tools are optional (require local D:\\persona running on loopback).
    Fire-and-forget — if persona is unavailable, tools silently no-op.
    This function is called once at tool registration time.
    """
    if TOOL_CATEGORIES.get("persona"):
        return  # Already initialized
    try:
        from adk.persona import PERSONA_TOOLS
        TOOL_CATEGORIES["persona"] = list(PERSONA_TOOLS)
        for fn in PERSONA_TOOLS:
            TOOL_INTENT_CATEGORIES.setdefault(fn, [])
        logger.info("Persona tools initialized (%d tools)", len(PERSONA_TOOLS))
    except ImportError:
        logger.debug("Persona tools not available; persona category remains empty")
        TOOL_CATEGORIES["persona"] = []


def register_builtin_tools(
    agent: AitherAgent,
    categories: list[str] | None = None,
    auto: bool = True,
) -> int:
    """Register built-in tools on an agent.

    Args:
        agent: The AitherAgent to register tools on.
        categories: Specific categories to register. If None and auto=True,
                    picks based on agent identity name.
        auto: If True and categories is None, auto-detect from identity.

    Returns:
        Number of tools registered.
    """
    _init_graph_tools()  # lazily populate graph category
    _init_workspace_tools()  # lazily populate workspace category
    _init_voice_tools()  # lazily populate voice category
    _init_formbridge_tools()  # lazily populate formbridge category
    _init_notebook_tools()  # lazily populate notebooks category
    _init_persona_tools()  # lazily populate persona category
    _init_decision_tools()  # lazily populate decisions category
    # `relay` had an initialiser and no caller until 2026-08-19, so its three
    # tools were registered by nothing: written, imported, and unreachable.
    _init_relay_tools()  # lazily populate relay category
    _init_queue_tools()  # lazily populate queue category (awrun)
    _init_nest_tools()  # lazily populate nest category
    _init_browse_tools()  # lazily populate browse category (awbrowse)
    _init_find_tools()  # lazily populate find category (awfind)

    if categories is None:
        # SCHEMA TAX. Measured 2026-09-21 with tiktoken: the daemon identity's 14
        # categories are 48-55 tools whose OpenAI schema is ~5.5k tokens, a third of a
        # 16,384-token slot, paid on EVERY call before a byte of the task. A named
        # toolpack (or explicit categories) in ADK_BUILTIN_TOOL_CATEGORIES narrows the
        # set for the whole process; unknown names are logged and dropped, never fatal.
        categories = categories_from_env(os.environ.get("ADK_BUILTIN_TOOL_CATEGORIES", ""))
    if categories is None and auto:
        # Unknown identities get a minimal, fully-local default. "workspace"
        # tools call a workspace-intelligence service and aren't useful to an
        # unknown standalone agent, so they're opt-in per named identity only.
        # "decisions" IS in the minimal set: an agent that cannot reach its owner
        # has to guess, and a wrong guess is what the whole channel exists to
        # prevent. It needs nothing but the filesystem.
        categories = IDENTITY_DEFAULTS.get(agent.name, ["file_io", "web", "decisions"])

    if categories is None:
        categories = list(TOOL_CATEGORIES.keys())

    count = 0
    for cat in categories:
        if cat == "self":
            # Closure-based registration — each agent gets its own bound copies.
            count += register_self_tools(agent)
            continue
        fns = TOOL_CATEGORIES.get(cat)
        if fns is None:
            # Unknown category — try loading as a tool pack ID
            count += register_tool_packs(agent, pack_ids=[cat])
            continue
        for fn in fns:
            intent_cats = TOOL_INTENT_CATEGORIES.get(fn, [])
            agent._tools.register(fn, intent_categories=intent_cats)
            count += 1

    if count:
        logger.info("Registered %d built-in tools (%s) on agent %s",
                     count, ", ".join(categories), agent.name)
    return count


def register_tool_packs(
    agent: AitherAgent,
    pack_ids: list[str] | None = None,
    packs_dir: str | None = None,
) -> int:
    """Discover and register ToolPack tools on an ADK agent.

    Args:
        agent: The AitherAgent to register tools on.
        pack_ids: Specific pack IDs to load. If None, loads all discovered.
        packs_dir: Additional directory to scan for packs.

    Returns:
        Number of tools registered.
    """
    try:
        from pathlib import Path as _P

        # Tool-pack loading is an optional add-on (adk.tool_pack_loader).
        # Standalone agents without it run with their built-in tools.
        try:
            from adk.tool_pack_loader import get_tool_pack_loader
        except ImportError:
            logger.debug("Tool-pack loader not installed; skipping pack load")
            return 0

        extra = [_P(packs_dir)] if packs_dir else []
        loader = get_tool_pack_loader(extra_dirs=extra)
        loader.discover()
        manifests = loader.load_packs(pack_ids) if pack_ids else list(loader._manifests.values())
        count = 0
        for manifest in manifests:
            allowed, denial = loader.check_license(manifest)
            if not allowed:
                logger.info("Pack %s denied: %s", manifest.id, denial)
                continue
            count += loader.register_on_adk_agent(manifest, agent)
        if count:
            logger.info("Registered %d tool-pack tools on agent %s", count, agent.name)
        return count
    except Exception as exc:
        logger.warning("Tool pack registration failed: %s", exc)
        return 0
