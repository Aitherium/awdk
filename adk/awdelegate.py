#!/usr/bin/env python3
"""awdelegate.py: frontier models ask the question, local models do the digging.

WHY: the planning tier (Opus 5.5) and the execution tier (DeepSeek-V4-Flash) were
spending their tokens on exploration. A frontier model reads dozens of greps, file
reads and web lookups once and then throws the raw output away. This server moves
that loop onto the LOCAL models behind the spine (OpenAI shape,
http://127.0.0.1:8151). A local model runs up to 50 read-only tool calls and keeps
notes. It hands back only a short brief with path:line and URL citations.

This file is the awdk copy: `pip install awdk` puts it at `adk/awdelegate.py`, and
`adk claude setup` registers it at USER scope in `~/.claude.json`. The monorepo's
`tools/claude-backend/awdelegate.py` is a thin shim for registrations written
before the move; both run this implementation.

Every brief is CRYSTALLIZED. It is stored with the size and mtime of every file it
read or cites, so the same question is answered from the store until one of those
files changes. The full trajectory of every run is kept as training data for the
local driver. Each brief is also offered to awm, so it becomes recallable knowledge.

The tools reach every tier because the server is registered at USER scope: the
Opus main process, every DeepSeek subagent and every workflow agent get the same
three tools (research, recall, brief).

    python -S awdelegate.py --mcp                    # MCP stdio server (Claude Code)
    python -S awdelegate.py --ask "question" [--scope code|web|both]
    python -S awdelegate.py --recall "words"
    python -S awdelegate.py --self-test

Read-only by construction:
  * no tool writes, executes or deletes anything;
  * file tools are confined to the configured roots and refuse credential-shaped paths;
  * every tool result passes through a secret redactor before a model sees it.
"""
from __future__ import annotations

import getpass
import hashlib
import html
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

HOME = Path.home()
CONFIG_PATH = Path(os.environ.get("AWDELEGATE_CONFIG") or (HOME / ".aither" / "delegate.json"))
STORE = Path(os.environ.get("AWDELEGATE_STORE") or (HOME / ".aither" / "crystal"))
TRACE_DIR = Path(os.environ.get("AWROUTER_TRACE_DIR") or (HOME / ".aither" / "agent-trace"))
VERSION = "1.0"

DEFAULTS: Dict[str, Any] = {
    "spine": "127.0.0.1:8151",
    "model": "aither-orchestrator",
    # Availability, not a downgrade: the same spine's DeepSeek route takes over only
    # after the local driver fails twice in a row, and the brief says so.
    "fallback_model": "deepseek-v4-flash",
    # The session's working directory is the project Claude Code was started in;
    # the other two are the delegate's own store and Claude's project memory. A
    # machine that wants more (or different) roots sets "roots" in
    # ~/.aither/delegate.json. Nonexistent roots are dropped by Policy.
    "roots": [str(Path.cwd()),
              str(HOME / ".aither" / "crystal"),
              str(HOME / ".claude" / "projects")],
    "max_steps": 50,
    "budget_s": 300,
    "context_chars": 60000,
    # The local driver gathers; a stronger model WRITES the brief from the gathered
    # evidence only. Measured 2026-10-26: aither-orchestrator alone ran 31 tool calls
    # in 44.7s, but its brief contradicted itself and invented reason strings.
    # deepseek-v4-flash is the execution tier (not frontier); "deepseek-v4-flash-pool"
    # keeps it fully local at ~2 min per brief; "none" ships the driver's own draft.
    # A chain, tried in order: DeepSeek direct, then the same model family through
    # OpenRouter (the spine's or-* routes; measured 1.6 s for or-deepseek-v4.1-flash).
    "synth_model": ["deepseek-v4-flash", "or-deepseek-v4.1-flash"],
    "synth_chars": 120000,
    "synth_timeout_s": 120,
    "daemon_port": 9001,
    "step_timeout_s": 60,
    # Local first on a short leash: the 5090 is shared, and one tiny call measured
    # 0.19 s and the next 30.9 s (2026-10-26, GPU at 80% from other lanes). A local
    # step that misses this deadline is re-run on fallback_model; three misses in a
    # row move the rest of the run there.
    "local_step_timeout_s": 25,
    # Gap closing: measured 2026-10-26, the local driver made 50 calls of which 3 were
    # unique, so the brief could only report gaps. The synth model then gets this many
    # targeted tool turns on the same evidence before it writes the final brief.
    "gap_steps": 8,
    "gap_budget_s": 150,
    "ttl_days": {"code": 14, "web": 3, "both": 3},
    "search_port": 8114,       # AitherSearch over the HTTPS relay (CA: ~/.aither/aithernet-ca-bundle.pem)
}


def load_config() -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def _log(msg: str) -> None:
    # stdout is the MCP channel: diagnostics go to stderr only.
    sys.stderr.write("awdelegate: %s\n" % msg)
    sys.stderr.flush()


# ── safety: paths and secrets ────────────────────────────────────────────────

DENY_FILE = re.compile(
    r"^(\.env(\..*)?|\.netrc|\.npmrc|\.pypirc|\.git-credentials|\.credentials\.json|"
    r"credentials(\.json)?|secrets?\.(json|ya?ml|toml|env)|.*\.(pem|key|p12|pfx|kdbx|keystore|jks|secrets)|"
    r"id_(rsa|ed25519|ecdsa)(\.pub)?)$", re.I)
DENY_DIR = {".git", ".ssh", ".gnupg", "agent-tokens"}
RG_DENY = []
for _glob in (".git", ".ssh", ".gnupg", "agent-tokens", ".env", ".env.*", "*.pem", "*.key", "*.p12",
              "*.pfx", "*.kdbx", "*.keystore", "*.jks", "*.secrets", ".credentials.json", "credentials",
              "credentials.json", "secrets.*", "secret.*", "id_rsa*", "id_ed25519*", "id_ecdsa*", ".netrc",
              ".npmrc", ".pypirc", ".git-credentials"):
    RG_DENY += ["-g", "!" + _glob]

_SECRET_WHOLE = [
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\baither_sk_[A-Za-z0-9_]{12,}"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
]
_SECRET_VALUE = re.compile(
    r"(?i)((?:api[_-]?key|secret|token|passw(?:or)?d|authorization)[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?|bearer\s+)"
    r"([A-Za-z0-9_\-\.\+/=]{16,})")


def redact(text: str) -> str:
    for pattern in _SECRET_WHOLE:
        text = pattern.sub("[REDACTED]", text)
    return _SECRET_VALUE.sub(lambda m: m.group(1) + "[REDACTED]", text)


class Denied(Exception):
    pass


def _norm(raw: Any) -> str:
    p = str(raw or "").strip().strip('"').strip("'").replace("\\", "/")
    m = re.match(r"^/(?:mnt/)?([a-zA-Z])/(.*)$", p)            # /c/... and /mnt/c/...
    if m:
        p = "%s:/%s" % (m.group(1).upper(), m.group(2))
    if p.startswith("~"):
        p = str(HOME).replace("\\", "/") + p[1:]
    return p


def _within(path: Path, root: Path) -> bool:
    a, b = os.path.normcase(str(path)), os.path.normcase(str(root)).rstrip("\\/")
    return a == b or a.startswith(b + os.sep)


class Policy:
    def __init__(self, roots: List[str]):
        self.roots = [Path(_norm(r)).resolve() for r in roots if Path(_norm(r)).exists()]
        if not self.roots:
            raise SystemExit("awdelegate: none of the configured roots exist")

    def resolve(self, raw: Any, must_exist: bool = True) -> Path:
        text = _norm(raw)
        path = Path(text) if text else self.roots[0]
        if not path.is_absolute():
            path = self.roots[0] / path
        path = path.resolve()
        if not any(_within(path, root) for root in self.roots):
            raise Denied("outside the allowed roots (%s): %s"
                         % (", ".join(r.as_posix() for r in self.roots), path.as_posix()))
        if any(part in DENY_DIR for part in path.parts):
            raise Denied("refused: credential or VCS-internal directory")
        if DENY_FILE.match(path.name):
            raise Denied("refused: credential-shaped file name")
        if must_exist and not path.exists():
            raise Denied("not found: %s" % path.as_posix())
        return path


# ── tools the LOCAL model uses ───────────────────────────────────────────────

RG = shutil.which("rg") or shutil.which("rg.exe") or "rg"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(argv: List[str], timeout: float) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %ds; narrow the path or pattern" % timeout
    except OSError as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


def _fwd(line: str) -> str:
    """Forward slashes in the PATH part of an rg line only; code text is untouched."""
    m = re.match(r"^([A-Za-z]:.*?)([:-]\d+[:-].*)$", line, re.S)
    return (m.group(1).replace("\\", "/") + m.group(2)) if m else line


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


class Ctx:
    """Per-run state: what was read (freshness), which URLs were used, the notes."""

    def __init__(self, policy: Policy, cfg: Dict[str, Any]):
        self.policy, self.cfg = policy, cfg
        self.files: Dict[str, List[int]] = {}
        self.urls: List[str] = []
        self.notes: List[Dict[str, str]] = []
        self.answer: Optional[str] = None
        self.evidence: List[Dict[str, str]] = []
        self.seen: Dict[str, int] = {}

    def track(self, path: Path) -> None:
        try:
            st = path.stat()
            self.files[path.as_posix()] = [st.st_mtime_ns, st.st_size]
        except OSError:
            pass


def t_grep(ctx: Ctx, a: Dict[str, Any]) -> str:
    pattern = str(a.get("pattern") or "")
    if not pattern:
        return "error: pattern is required"
    if len(pattern.split()) > 4 and not re.search(r"[|\\.*+?\[\](){}^$]", pattern):
        return ("error: grep takes a short regex of identifiers or 1-3 keywords, not a sentence. "
                "Try alternatives such as 'fallback|oauth' or an exact function name.")
    base = ctx.policy.resolve(a.get("path"))
    one_file = base.is_file()
    argv = [RG, "-n", "-H", "--no-heading", "--color", "never", "--max-columns", "240",
            "--max-columns-preview", "-m", "200" if one_file else "8", "--hidden"] + RG_DENY
    if a.get("glob"):
        argv += ["-g", str(a["glob"])]
    if a.get("ignore_case"):
        argv.append("-i")
    if a.get("fixed_string"):
        argv.append("-F")
    around = _clamp(a.get("context"), 0, 4, 0)
    if around:
        argv += ["-C", str(around)]
    argv += ["-e", pattern, "--", str(base)]
    rc, out, err = _run(argv, 30)
    if rc == 1:
        return "no matches"
    if rc != 0:
        return "error: ripgrep %d: %s" % (rc, err.strip()[:240])
    lines = [_fwd(x) for x in out.splitlines() if x.strip()]
    limit = _clamp(a.get("max_results"), 1, 200, 150 if one_file else 60)
    text = "\n".join(lines[:limit])
    if len(lines) > limit:
        nxt = re.match(r"^.*?[:-](\d+)[:-]", lines[limit])
        text += ("\n... %d more matching lines; to see them call grep again with max_results=%d%s"
                 % (len(lines) - limit, min(200, limit * 2),
                    (", or read this file from offset=%s" % nxt.group(1)) if (nxt and one_file) else
                    ", or narrow the path/glob"))
    return text


def t_glob(ctx: Ctx, a: Dict[str, Any]) -> str:
    base = ctx.policy.resolve(a.get("path"))
    argv = [RG, "--files", "--hidden", "--color", "never"] + RG_DENY
    if a.get("pattern"):
        argv += ["-g", str(a["pattern"])]
    rc, out, err = _run(argv + ["--", str(base)], 30)
    if rc not in (0, 1):
        return "error: ripgrep %d: %s" % (rc, err.strip()[:240])
    files = [x.replace("\\", "/") for x in out.splitlines() if x.strip()]
    if not files:
        return "no files"
    limit = _clamp(a.get("max_results"), 1, 300, 100)
    text = "\n".join(files[:limit])
    if len(files) > limit:
        text += "\n... %d more files" % (len(files) - limit)
    return text


def t_list(ctx: Ctx, a: Dict[str, Any]) -> str:
    path = ctx.policy.resolve(a.get("path"))
    if path.is_file():
        return "%s is a file (%d bytes)" % (path.as_posix(), path.stat().st_size)
    rows = []
    with os.scandir(path) as it:
        for entry in it:
            if entry.name in DENY_DIR or DENY_FILE.match(entry.name):
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False
            rows.append((not is_dir, entry.name.lower(), entry.name + ("/" if is_dir else "")))
    rows.sort()
    names = [r[2] for r in rows]
    text = "\n".join(names[:250])
    return text + ("\n... %d more" % (len(names) - 250) if len(names) > 250 else "") or "(empty)"


def t_read(ctx: Ctx, a: Dict[str, Any]) -> str:
    path = ctx.policy.resolve(a.get("path"))
    if path.is_dir():
        return t_list(ctx, {"path": str(path)})
    with open(path, "rb") as fh:
        if b"\x00" in fh.read(4096):
            return "binary file (%d bytes); not shown" % path.stat().st_size
    offset = max(1, _clamp(a.get("offset"), 1, 10 ** 9, 1))
    limit = _clamp(a.get("limit"), 1, 300, 160)
    out: List[str] = []
    size, more = 0, False
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            if n < offset:
                continue
            if n >= offset + limit or size > 16000:
                more = True
                break
            line = line.rstrip("\r\n")
            if len(line) > 400:
                line = line[:400] + " ..."
            out.append("%6d\t%s" % (n, line))
            size += len(line)
    ctx.track(path)
    if not out:
        return "(no lines at offset %d)" % offset
    if more:
        out.append("... more below; read again with offset=%d" % (offset + len(out)))
    return "\n".join(out)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        host = urllib.parse.urlsplit(newurl).hostname or ""
        if not _public_host(host):
            raise urllib.error.URLError("redirect to a non-public host refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return bool(infos)


def html_to_text(page: str) -> str:
    page = re.sub(r"(?is)<(script|style|noscript|svg|head)\b.*?</\1>", " ", page)
    page = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6]|section|article|pre)>", "\n", page)
    page = html.unescape(re.sub(r"(?s)<[^>]+>", " ", page))
    lines = [re.sub(r"[ \t\r\f\v]+", " ", x).strip() for x in page.splitlines()]
    return "\n".join(x for x in lines if x)


def _http_get(url: str, timeout: float = 20, limit: int = 1_500_000) -> Tuple[str, str]:
    opener = urllib.request.build_opener(_SafeRedirect)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; awdelegate/%s; +https://aitherium.com)" % VERSION,
        "Accept": "text/html,text/plain,application/json;q=0.9,*/*;q=0.5"})
    with opener.open(req, timeout=timeout) as resp:
        ctype = resp.headers.get("Content-Type", "")
        raw = resp.read(limit)
    charset = (re.search(r"charset=([\w\-]+)", ctype) or [None, "utf-8"])[1]
    try:
        return ctype, raw.decode(charset, "replace")
    except LookupError:
        return ctype, raw.decode("utf-8", "replace")


def t_fetch(ctx: Ctx, a: Dict[str, Any]) -> str:
    url = str(a.get("url") or "")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "error: only absolute http(s) URLs"
    if not _public_host(parts.hostname):
        return "error: only public hosts are fetched"
    try:
        ctype, text = _http_get(url)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return "error: fetch failed: %s" % str(exc)[:200]
    if "html" in ctype or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        text = html_to_text(text)
    if url not in ctx.urls:
        ctx.urls.append(url)
    return text[:9000] + ("\n... (truncated)" if len(text) > 9000 else "")


def t_web_search(ctx: Ctx, a: Dict[str, Any]) -> str:
    query = str(a.get("query") or "").strip()
    if not query:
        return "error: query is required"
    n = _clamp(a.get("n"), 1, 10, 6)
    results = web_search(query, n, ctx.cfg)
    if isinstance(results, str):
        return results
    lines = []
    for i, r in enumerate(results, 1):
        lines.append("%d. %s\n   %s\n   %s" % (i, r.get("title", "")[:140], r.get("url", ""),
                                             (r.get("snippet") or "")[:280]))
    return "\n".join(lines) or "no results"


CA_BUNDLE = os.environ.get("AITHER_CA_BUNDLE") or str(HOME / ".aither" / "aithernet-ca-bundle.pem")


def _fleet_tls():
    import ssl
    try:
        return ssl.create_default_context(cafile=CA_BUNDLE)
    except (OSError, ValueError):
        return ssl._create_unverified_context()   # loopback only: every fleet port here is a local relay


def fleet_call(port: int, path: str, payload: Optional[Dict[str, Any]], timeout: float,
               headers: Optional[Dict[str, str]] = None, tls: bool = True) -> Tuple[int, Any]:
    """POST (GET when payload is None) to a fleet service on 127.0.0.1. Returns (status, json-or-text).

    The fleet ports on this host are wslrelay forwards that speak HTTPS only; plain
    http gets an empty reply that reads like "down". The awdk daemon is plain http.
    """
    conn = (http.client.HTTPSConnection("127.0.0.1", port, timeout=timeout, context=_fleet_tls()) if tls
            else http.client.HTTPConnection("127.0.0.1", port, timeout=timeout))
    head = {"Content-Type": "application/json"}
    head.update(headers or {})
    try:
        if payload is None:
            conn.request("GET", path, headers=head)
        else:
            conn.request("POST", path, body=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=head)
        resp = conn.getresponse()
        raw = resp.read(4_000_000)
    finally:
        conn.close()
    try:
        return resp.status, json.loads(raw)
    except ValueError:
        return resp.status, raw.decode("utf-8", "replace")


def web_search(query: str, n: int, cfg: Dict[str, Any]) -> Any:
    """AitherSearch (:8114, the platform's own search). [{title,url,snippet}] or an error string."""
    port = int(cfg.get("search_port") or 8114)
    try:
        status, data = fleet_call(port, "/search", {"query": query[:512], "limit": n, "synthesize": False}, 30)
    except (OSError, http.client.HTTPException) as exc:
        return "error: AitherSearch :%d unreachable (%s)" % (port, type(exc).__name__)
    if status != 200 or not isinstance(data, dict):
        return "error: AitherSearch :%d -> HTTP %s" % (port, status)
    out = []
    for r in data.get("results") or []:
        if isinstance(r, dict) and r.get("url"):
            out.append({"title": str(r.get("title") or ""), "url": str(r["url"]),
                        "snippet": str(r.get("snippet") or r.get("content") or "")})
    return out[:n]


def t_recall(ctx: Ctx, a: Dict[str, Any]) -> str:
    query = str(a.get("query") or "")
    hits = search_briefs(query, _clamp(a.get("k"), 1, 8, 4))
    lines = ["brief %s (%s ago): %s\n  %s" % (h["id"], _age(h["created"]), h["question"][:160],
                                              h["title"][:200]) for h in hits]
    mem = awm_recall(query, 4)
    if mem:
        lines.append("awm memory:\n" + mem)
    return "\n".join(lines) or "nothing stored yet for that query"


def t_note(ctx: Ctx, a: Dict[str, Any]) -> str:
    finding = str(a.get("finding") or "").strip()
    if not finding:
        return "error: finding is required"
    ctx.notes.append({"finding": finding[:600], "source": str(a.get("source") or "")[:300]})
    return "noted #%d (notes survive context trimming)" % len(ctx.notes)


def t_finish(ctx: Ctx, a: Dict[str, Any]) -> str:
    ctx.answer = str(a.get("answer") or "").strip()
    return "done"


def _fn(name: str, desc: str, props: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required}}}


_S = {"type": "string"}
_I = {"type": "integer"}
_B = {"type": "boolean"}
LOCAL_TOOLS = [
    _fn("grep", "Regex search of file contents (ripgrep). Returns path:line:text; context=2 adds the lines "
        "around each match.",
        {"pattern": _S, "path": _S, "glob": _S, "ignore_case": _B, "fixed_string": _B, "context": _I,
         "max_results": _I},
        ["pattern"]),
    _fn("glob", "List files matching a glob such as **/*.py under a path.",
        {"pattern": _S, "path": _S, "max_results": _I}, []),
    _fn("list_dir", "List a directory.", {"path": _S}, ["path"]),
    _fn("read", "Read numbered lines of a file. Use offset/limit to read only what you need.",
        {"path": _S, "offset": _I, "limit": _I}, ["path"]),
    _fn("web_search", "Search the web. Returns title, url, snippet.", {"query": _S, "n": _I}, ["query"]),
    _fn("fetch_url", "Fetch a public web page as plain text.", {"url": _S}, ["url"]),
    _fn("recall", "Search stored briefs and Aither memory for prior answers.", {"query": _S}, ["query"]),
    _fn("note", "Record one finding with its citation (path:line or URL). Notes survive trimming.",
        {"finding": _S, "source": _S}, ["finding"]),
    _fn("finish", "Return the final brief. This ends the investigation.", {"answer": _S}, ["answer"]),
]
TOOL_IMPL: Dict[str, Callable[[Ctx, Dict[str, Any]], str]] = {
    "grep": t_grep, "glob": t_glob, "list_dir": t_list, "read": t_read, "web_search": t_web_search,
    "fetch_url": t_fetch, "recall": t_recall, "note": t_note, "finish": t_finish,
}
WEB_TOOLS = {"web_search", "fetch_url"}
TOOL_CACHEABLE = {"grep", "glob", "list_dir", "read", "web_search", "fetch_url"}
CODE_TOOLS = {"grep", "glob", "list_dir", "read"}


# ── the local driver ─────────────────────────────────────────────────────────

class DriverError(Exception):
    pass


def chat(cfg: Dict[str, Any], model: str, messages: List[Dict[str, Any]],
         tools: Optional[List[Dict[str, Any]]], max_tokens: int, timeout: float) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    host, _, port = str(cfg["spine"]).rpartition(":")
    body: Dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens,
                            "temperature": 0.2, "stream": False}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        conn = http.client.HTTPConnection(host, int(port), timeout=timeout)
        conn.request("POST", "/v1/chat/completions", body=raw,
                     headers={"Content-Type": "application/json", "x-aither-caller": "awdelegate"})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
    except (OSError, http.client.HTTPException) as exc:
        raise DriverError("%s: %s" % (type(exc).__name__, str(exc)[:160]))
    if resp.status != 200:
        raise DriverError("HTTP %d: %s" % (resp.status, data[:240].decode("utf-8", "replace")))
    try:
        parsed = json.loads(data)
        return parsed["choices"][0]["message"] or {}, parsed.get("usage") or {}
    except (ValueError, KeyError, IndexError, TypeError):
        raise DriverError("unparseable reply: %r" % data[:200])


_TEXT_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


def tool_calls_of(msg: Dict[str, Any]) -> List[Dict[str, Any]]:
    calls = list(msg.get("tool_calls") or [])
    if calls:
        return calls
    # Some chat templates leave the call as text; accept the common <tool_call> form.
    for i, m in enumerate(_TEXT_CALL.finditer(msg.get("content") or "")):
        try:
            obj = json.loads(m.group(1))
        except ValueError:
            continue
        calls.append({"id": "text-%d" % i, "type": "function",
                      "function": {"name": obj.get("name"), "arguments": json.dumps(obj.get("arguments") or {})}})
    return calls


SYSTEM = """You are Aither's local research delegate. A frontier model sent you a question so that it does not have to spend its own tokens on the digging. Investigate with the tools, then call finish.

Rules:
- Tools are read-only. Paths are absolute or relative to an allowed root; allowed roots: {roots}.
- grep first (context=2 shows the code around a match), then read only the lines you need (offset/limit). Never read a whole large file, and never repeat an identical call.
- Search for identifiers and distinctive strings (function names, error text, config keys, quoted literals), never for the question's own sentence.
- Call note() for every finding worth keeping, with its citation (path:line or URL). Old tool output is trimmed; notes are not.
- Stop as soon as the question is answered. You have at most {steps} tool turns.
- The finish answer is ALL the frontier model will see:
  1. the answer first, at most 12 lines;
  2. "Evidence:" bullets, one citation each (path:line or URL);
  3. "Gaps:" anything you could not verify.
  Never present a guess as a fact. Today is {today}."""


SYNTH = """You write the final brief for a frontier model from evidence that a local agent gathered with read-only tools. Use ONLY that evidence.
Format:
1. The answer, at most 12 lines, concrete: names, values, conditions, exact strings.
2. "Evidence:" bullets, each one path:line (or URL) copied exactly from the evidence, plus what it shows.
3. "Gaps:" what the evidence does not settle.
Never invent a path, line number, identifier or value. If the local agent's draft contradicts the evidence, the evidence wins."""

_STOP = set("the and for with that this what where when which from into does how why are was were has have not "
            "but all any can its use used using about there their them then than also only out over under via per "
            "get gets make makes each".split())


def _relevance(text: str, words: set) -> float:
    low = text.lower()
    return sum(1 for w in words if w in low) / (1.0 + len(text) / 4000.0)


def synthesize(cfg: Dict[str, Any], question: str, ctx: "Ctx", draft: str,
               chat_fn: Callable[..., Tuple[Dict[str, Any], Dict[str, Any]]] = chat) -> Tuple[str, str]:
    """Write the brief from the gathered evidence on cfg['synth_model']. Returns (brief, note)."""
    chain = cfg.get("synth_model") or []
    chain = [str(m) for m in (chain if isinstance(chain, list) else [chain])]
    if not (ctx.evidence or ctx.notes) or not chain:
        return draft, ""
    words = {w for w in re.findall(r"[a-z0-9_]{3,}", question.lower()) if w not in _STOP}
    budget = int(cfg.get("synth_chars") or 120000)
    ranked = sorted(enumerate(ctx.evidence), key=lambda iv: (_relevance(iv[1]["result"], words), iv[0]),
                    reverse=True)
    chosen, used = [], 0
    for i, ev in ranked:
        block = "### [%d] %s %s\n%s\n" % (i + 1, ev["tool"], ev["args"], ev["result"])
        if used + len(block) <= budget:
            chosen.append((i, block))
            used += len(block)
    chosen.sort()
    notes = "\n".join("- %s (%s)" % (n["finding"], n["source"]) for n in ctx.notes) or "(none)"
    user = ("Question: %s\n\nLocal agent notes:\n%s\n\nLocal agent draft (may be wrong):\n%s\n\n"
            "Evidence (%d of %d tool results):\n%s" % (question, notes, draft or "(none)", len(chosen),
                                                     len(ctx.evidence), "".join(b for _, b in chosen)))
    t0 = time.time()
    failed: List[str] = []
    for model in chain:
        try:
            msg, _ = chat_fn(cfg, model, [{"role": "system", "content": SYNTH}, {"role": "user", "content": user}],
                             None, 1500, float(cfg.get("synth_timeout_s") or 120))
            break
        except DriverError as exc:
            failed.append("%s: %s" % (model, str(exc)[:80]))
    else:
        return draft, "synthesis failed (%s); brief is the local draft" % "; ".join(failed)
    text = redact(str(msg.get("content") or "").strip())
    if not text:
        return draft, "synthesis on %s returned nothing; brief is the local draft" % model
    return text, "written by %s in %.1fs from %d evidence blocks" % (model, time.time() - t0, len(chosen))


_CITE = re.compile(r"((?:[A-Za-z]:)?[\w./\\-]+\.[A-Za-z0-9_]+):(\d+)")


def _line_at(path: Path, n: int) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                if i == n:
                    return line.rstrip("\r\n")
    except OSError:
        return None
    return None


def verify_citations(answer: str, ctx: "Ctx", limit: int = 12) -> List[str]:
    """Re-read every cited path:line from disk: the caller sees the real line, or that it does not exist."""
    by_name: Dict[str, str] = {}
    for f in ctx.files:
        by_name.setdefault(Path(f).name.lower(), f)
    rows: List[str] = []
    seen: set = set()
    for m in _CITE.finditer(answer):
        raw, n = m.group(1).replace("\\", "/"), int(m.group(2))
        if not re.match(r"^[A-Za-z]:/", raw):
            raw = by_name.get(Path(raw).name.lower(), "")
            if not raw:
                continue
        if (raw.lower(), n) in seen:
            continue
        seen.add((raw.lower(), n))
        try:
            path = ctx.policy.resolve(raw)
        except Denied:
            rows.append("- %s:%d  (not readable here)" % (raw, n))
            continue
        line = _line_at(path, n)
        rows.append("- %s:%d  %s" % (path.as_posix(), n, ("`%s`" % redact(line.strip())[:160]) if line is not None
                                      else "(NO SUCH LINE: this citation is wrong)"))
        if len(rows) >= limit:
            break
    return rows


def _msg_chars(messages: List[Dict[str, Any]]) -> int:
    return sum(len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or "")) for m in messages)


def trim(messages: List[Dict[str, Any]], limit: int, notes: List[Dict[str, str]]) -> bool:
    """Elide the oldest tool results once the window is over budget. Keeps the last 6."""
    if _msg_chars(messages) <= limit:
        return False
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"][:-6]
    for i in tool_idx:
        content = str(messages[i].get("content") or "")
        if content.startswith("[trimmed"):
            continue
        messages[i]["content"] = "[trimmed; first line was: %s]" % content.split("\n", 1)[0][:160]
        if _msg_chars(messages) <= limit:
            break
    if notes:
        messages.append({"role": "user", "content": "Older tool output was trimmed. Your notes so far:\n" +
                         "\n".join("- %s (%s)" % (n["finding"], n["source"]) for n in notes)})
    return True


def investigate(question: str, scope: str, paths: List[str], cfg: Dict[str, Any], policy: Policy,
                max_steps: int, budget_s: float, model: str,
                progress: Optional[Callable[[int, int, str], None]] = None,
                cancelled: Optional[Callable[[], bool]] = None,
                chat_fn: Callable[..., Tuple[Dict[str, Any], Dict[str, Any]]] = chat,
                ctx: Optional["Ctx"] = None, extra: str = "") -> Dict[str, Any]:
    allowed = set(TOOL_IMPL)
    if scope == "code":
        allowed -= WEB_TOOLS
    elif scope == "web":
        allowed -= CODE_TOOLS
    tools = [t for t in LOCAL_TOOLS if t["function"]["name"] in allowed]
    if ctx is None:
        ctx = Ctx(policy, cfg)
    else:
        ctx.answer, ctx.seen = None, {}
    focus = ("\nFocus paths: " + ", ".join(paths)) if paths else ""
    if extra:
        focus += "\n\n" + extra
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": SYSTEM.format(
            roots=", ".join(r.as_posix() for r in policy.roots), steps=max_steps,
            today=time.strftime("%Y-%m-%d"))},
        {"role": "user", "content": "Question: %s\nScope: %s%s" % (question, scope, focus)},
    ]
    steps: List[Dict[str, Any]] = []
    t0 = time.time()
    driver, fails, limit = model, 0, int(cfg["context_chars"])
    notes_on_driver: List[str] = []
    turn = 0
    local_timeouts = 0
    models_used: Dict[str, int] = {}
    repeats = 0
    while turn < max_steps and ctx.answer is None:
        if cancelled and cancelled():
            notes_on_driver.append("cancelled by the caller")
            break
        left = budget_s - (time.time() - t0)
        if left < 5:
            notes_on_driver.append("time budget reached")
            break
        trim(messages, limit, ctx.notes)
        started = time.time()
        fallback = str(cfg.get("fallback_model") or "")
        on_local = bool(driver == model and fallback and fallback != model)
        try:
            wait = float(cfg.get("local_step_timeout_s") or 25) if on_local else float(cfg["step_timeout_s"])
            try:
                msg, _usage = chat_fn(cfg, driver, messages, tools, 1200, min(wait, left))
                step_model = driver
                if on_local:
                    local_timeouts = 0
            except DriverError as exc:
                if not (on_local and "timed out" in str(exc).lower()):
                    raise
                local_timeouts += 1
                if local_timeouts >= 3:
                    notes_on_driver.append("%s missed %ds three steps running; continued on %s"
                                           % (driver, wait, fallback))
                    driver = fallback
                left = max(5.0, budget_s - (time.time() - t0))
                msg, _usage = chat_fn(cfg, fallback, messages, tools, 1200, min(float(cfg["step_timeout_s"]), left))
                step_model = fallback
            fails = 0
        except DriverError as exc:
            fails += 1
            text = str(exc)
            if re.search(r"context|maximum|too long|exceed", text, re.I) and limit > 12000:
                limit //= 2
                notes_on_driver.append("context over the model window; trimming to %d chars" % limit)
                continue
            if fails >= 2 and cfg.get("fallback_model") and driver != cfg["fallback_model"]:
                notes_on_driver.append("%s failed twice (%s); continued on %s" % (driver, text[:120],
                                                                                cfg["fallback_model"]))
                driver, fails = str(cfg["fallback_model"]), 0
                continue
            if fails >= 3:
                notes_on_driver.append("driver failed: %s" % text[:160])
                break
            continue
        turn += 1
        models_used[step_model] = models_used.get(step_model, 0) + 1
        calls = tool_calls_of(msg)
        if not calls:
            content = str(msg.get("content") or "").strip()
            if content:
                ctx.answer = content
                break
            messages.append({"role": "user", "content": "Call a tool, or call finish with your brief."})
            continue
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            fn = (call.get("function") or {})
            name = str(fn.get("name") or "")
            try:
                args = fn.get("arguments")
                args = json.loads(args) if isinstance(args, str) else (args or {})
                if not isinstance(args, dict):
                    raise ValueError("arguments must be an object")
            except ValueError as exc:
                args, result = {}, "error: arguments were not valid JSON (%s)" % exc
            else:
                impl = TOOL_IMPL.get(name) if name in allowed else None
                if impl is None:
                    result = "error: unknown or out-of-scope tool %r" % name
                else:
                    tstart = time.time()
                    sig = name + json.dumps(args, sort_keys=True)
                    try:
                        if name in TOOL_CACHEABLE and sig in ctx.seen:
                            result = ("(identical call already answered at turn %d; use your notes, or ask for "
                                      "different lines)" % ctx.seen[sig])
                        else:
                            ctx.seen[sig] = turn
                            result = impl(ctx, args)
                    except Denied as exc:
                        result = "error: %s" % exc
                    except Exception as exc:  # a tool bug must not end the run
                        result = "error: %s: %s" % (type(exc).__name__, str(exc)[:200])
                    steps.append({"turn": turn, "tool": name, "args": {k: str(v)[:160] for k, v in args.items()},
                                  "ms": int((time.time() - tstart) * 1000), "chars": len(result)})
            result = redact(result)
            repeats = repeats + 1 if result.startswith("(identical call") else 0
            if name in TOOL_CACHEABLE and not result.startswith("(identical call"):
                ctx.evidence.append({"tool": name, "args": json.dumps(args, ensure_ascii=False)[:300],
                                     "result": result})
            messages.append({"role": "tool", "tool_call_id": str(call.get("id") or name), "content": result})
        if repeats >= 4:
            notes_on_driver.append("%s repeated identical calls; gathering stopped at %d unique results"
                                   % (step_model, len(ctx.evidence)))
            break
        if progress:
            last = steps[-1]["tool"] if steps else "thinking"
            progress(turn, max_steps, "%s (%.0fs)" % (last, time.time() - t0))
        _ = started
    synth_on = str(cfg.get("synth_model") or "none") != "none"
    if ctx.answer is None and not synth_on:
        messages.append({"role": "user", "content": "Stop investigating. Write the final brief now, in the "
                         "required format, from your notes and the evidence above."})
        trim(messages, limit, ctx.notes)
        try:
            msg, _usage = chat_fn(cfg, driver, messages, None, 1500, max(30.0, min(120.0, budget_s)))
            ctx.answer = str(msg.get("content") or "").strip() or None
        except DriverError as exc:
            notes_on_driver.append("final synthesis failed: %s" % str(exc)[:160])
    draft = redact(ctx.answer or "")
    answer, synth_note = synthesize(cfg, question, ctx, draft, chat_fn) if synth_on else (draft, "")
    if synth_note:
        notes_on_driver.append(synth_note)
    if not answer and ctx.notes:
        answer = "No synthesis; the local driver's notes:\n" + "\n".join(
            "- %s (%s)" % (n["finding"], n["source"]) for n in ctx.notes)
    for cited in re.findall(r"([A-Za-z]:/[^\s:`'\"()]+)(?::\d+)?", answer):
        try:
            p = policy.resolve(cited)
            if p.is_file():
                ctx.track(p)
        except Denied:
            pass
    checked = verify_citations(answer, ctx) if answer else []
    if checked:
        answer += "\n\nCitations checked against disk just now:\n" + "\n".join(checked)
    return {"answer": answer, "draft": draft, "notes": ctx.notes, "files": ctx.files, "urls": ctx.urls,
            "steps": steps,
            "driver": driver, "models": models_used, "driver_notes": notes_on_driver, "turns": turn,
            "seconds": round(time.time() - t0, 1),
            "tool_chars": sum(s["chars"] for s in steps), "messages": messages, "ctx": ctx}


# ── crystal store: briefs that are reused, not re-acquired ──────────────────

def _key(question: str, scope: str, paths: List[str]) -> str:
    norm = re.sub(r"\s+", " ", question.strip().lower()).rstrip("?!. ")
    return hashlib.sha1(("%s|%s|%s" % (norm, scope, ",".join(sorted(paths)))).encode("utf-8")).hexdigest()


def _age(created: float) -> str:
    s = max(0, time.time() - created)
    return "%dm" % (s // 60) if s < 7200 else ("%dh" % (s // 3600) if s < 172800 else "%dd" % (s // 86400))


def _index() -> List[Dict[str, Any]]:
    rows = []
    try:
        with open(STORE / "index.jsonl", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def fresh_brief(key: str, scope: str, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ttl = float((cfg.get("ttl_days") or {}).get(scope, 3)) * 86400
    for row in reversed(_index()):
        if row.get("key") != key:
            continue
        if time.time() - float(row.get("created") or 0) > ttl:
            return None
        for path, (mtime, size) in (row.get("files") or {}).items():
            try:
                st = os.stat(path)
            except OSError:
                return None
            if st.st_mtime_ns != mtime or st.st_size != size:
                return None
        return row
    return None


def save_brief(question: str, scope: str, paths: List[str], key: str, run: Dict[str, Any]) -> Dict[str, Any]:
    created = time.time()
    bid = "%s-%s" % (time.strftime("%Y%m%d"), key[:10])
    title = (run["answer"].strip().splitlines() or [""])[0][:200]
    row = {"id": bid, "key": key, "question": question, "scope": scope, "paths": paths, "created": created,
           "driver": run["driver"], "turns": run["turns"], "seconds": run["seconds"], "files": run["files"],
           "urls": run["urls"], "tool_chars": run["tool_chars"], "brief_chars": len(run["answer"]),
           "title": title}
    (STORE / "briefs").mkdir(parents=True, exist_ok=True)
    (STORE / "trajectories").mkdir(parents=True, exist_ok=True)
    body = ["# %s" % question, "",
            "<!-- crystal %s -->" % json.dumps({k: row[k] for k in ("id", "key", "scope", "driver", "turns",
                                                                   "seconds", "created")}), "",
            run["answer"], ""]
    if run["notes"]:
        body += ["## Notes", ""] + ["- %s (%s)" % (n["finding"], n["source"]) for n in run["notes"]] + [""]
    if run["files"] or run["urls"]:
        body += ["## Sources", ""] + ["- %s" % p for p in run["files"]] + ["- %s" % u for u in run["urls"]]
    (STORE / "briefs" / ("%s.md" % bid)).write_text("\n".join(body) + "\n", encoding="utf-8")
    # The trajectory is the learning signal: a local tool-use episode with its outcome.
    (STORE / "trajectories" / ("%s.json" % bid)).write_text(json.dumps(
        {"id": bid, "question": question, "scope": scope, "driver": run["driver"], "messages": run["messages"],
         "steps": run["steps"], "answer": run["answer"]}, ensure_ascii=False), encoding="utf-8")
    with open(STORE / "index.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def search_briefs(query: str, k: int) -> List[Dict[str, Any]]:
    words = {w for w in re.findall(r"[a-z0-9_]{3,}", query.lower())}
    latest: Dict[str, Dict[str, Any]] = {}
    for row in _index():
        latest[row.get("key", "")] = row
    scored = []
    for row in latest.values():
        hay = set(re.findall(r"[a-z0-9_]{3,}", (row.get("question", "") + " " + row.get("title", "")).lower()))
        score = len(words & hay) / max(1, len(words))
        if score > 0:
            scored.append((score, row.get("created", 0), row))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [r for _, _, r in scored[:k]]


def read_brief(bid: str) -> str:
    if not re.fullmatch(r"[0-9]{8}-[0-9a-f]{10}", bid or ""):
        return "error: brief ids look like 20261026-0123456789"
    try:
        return (STORE / "briefs" / ("%s.md" % bid)).read_text(encoding="utf-8")
    except OSError:
        return "error: no brief %s" % bid


# ── awm: briefs become recallable knowledge ──────────────────────────────────

AWM = shutil.which("awm") or shutil.which("awm.exe") or ""
_SCOPE_SAFE = re.compile(r"[^A-Za-z0-9._@+-]+")


def _clean_scope(text: str) -> str:
    """One scope segment: never a ':' (the separator) or '*' (the wildcard).
    Same cleaning as awm's own derivation, so both land on the same scope."""
    return _SCOPE_SAFE.sub("-", text.strip()).strip("-")


def _awm_scope(cfg: Optional[Dict[str, Any]] = None) -> str:
    """The awm scope for this checkout, derived the way awm derives it: the owner
    of the origin remote, the user (AWM_USER / AITHER_USER / login name), and the
    repository directory. ``awm_scope`` in the delegate config or AWM_SCOPE
    overrides all three. Empty when it cannot be derived: recalls then let awm
    derive for itself, and remembers are skipped rather than written to a guess."""
    # `cfg or load_config()` made an EXPLICIT empty dict mean "read the file", so a
    # caller passing {} to isolate itself still got the machine's awm_scope
    # (test_awm_scope_configuration_wins failed on any dev box that set one).
    configured = str(((cfg if cfg is not None else load_config()) or {}).get("awm_scope")
                     or os.environ.get("AWM_SCOPE") or "").strip()
    if configured:
        return configured
    here = Path.cwd().resolve()
    root = next((d for d in (here, *here.parents) if (d / ".git").exists()), None)
    if root is None:
        return ""
    rc, out, _ = _run(["git", "-C", str(root), "remote", "get-url", "origin"], 10)
    url = (out.strip() if rc == 0 else "").rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if "://" in url:
        tail = url.split("://", 1)[1]
        owner = tail.split("/", 1)[1].split("/")[0] if "/" in tail else ""
    elif ":" in url and not re.match(r"^[A-Za-z]:[\\/]", url):
        owner = url.split(":", 1)[1].split("/")[0]
    else:
        owner = ""
    try:
        user = (os.environ.get("AWM_USER") or os.environ.get("AITHER_USER")
                or getpass.getuser())
    except Exception:  # noqa: BLE001 -- no login name means no derivable scope
        user = ""
    parts = (_clean_scope(owner).lower(), _clean_scope(user),
             _clean_scope(root.name))
    if not all(parts) or parts[0] == "platform":
        return ""
    return ":".join(parts)


def awm_recall(query: str, k: int) -> str:
    if not AWM or not query.strip():
        return ""
    scope = _awm_scope()
    argv = [AWM, "recall"]
    if scope:
        argv += ["--scope", scope]
    argv += ["--query", query, "--limit", str(k)]
    rc, out, _ = _run(argv, 15)
    return redact(out.strip())[:2500] if rc == 0 else ""


def awm_remember(row: Dict[str, Any], answer: str) -> bool:
    if not AWM:
        return False
    scope = _awm_scope()
    if not scope:
        return False
    text = "Local brief %s answered: %s\n%s\nFull brief: %s" % (
        row["id"], row["question"][:300], answer[:700],
        (STORE / "briefs" / ("%s.md" % row["id"])).as_posix())
    rc, _, _ = _run([AWM, "remember", "--scope", scope, "--key", "crystal-brief-" + row["id"],
                     "--value", text], 15)
    return rc == 0


# ── the public operations ────────────────────────────────────────────────────

def research(args: Dict[str, Any], progress=None, cancelled=None,
             chat_fn: Callable[..., Tuple[Dict[str, Any], Dict[str, Any]]] = chat) -> str:
    cfg = load_config()
    question = str(args.get("question") or "").strip()
    if not question:
        return "error: question is required"
    scope = str(args.get("scope") or "both")
    scope = scope if scope in ("code", "web", "both") else "both"
    paths = [str(p) for p in (args.get("paths") or [])][:8]
    policy = Policy(list(cfg["roots"]))
    for p in paths:
        try:
            policy.resolve(p)
        except Denied as exc:
            return "error: path %s: %s" % (p, exc)
    key = _key(question, scope, paths)
    if not args.get("fresh"):
        hit = fresh_brief(key, scope, cfg)
        if hit:
            body = read_brief(hit["id"])
            if not body.startswith("error:"):
                write_trace({"event": "reuse", "id": hit["id"], "question": question[:300]})
                return ("[crystal brief %s, reused: stored %s ago, %d cited files unchanged, 0 tool calls]\n\n%s"
                        % (hit["id"], _age(hit["created"]), len(hit.get("files") or {}), body))
    max_steps = _clamp(args.get("max_steps"), 1, 80, int(cfg["max_steps"]))
    budget = float(_clamp(args.get("budget_s"), 20, 1800, int(cfg["budget_s"])))
    model = str(args.get("model") or cfg["model"])
    run = investigate(question, scope, paths, cfg, policy, max_steps, budget, model,
                      progress=progress, cancelled=cancelled, chat_fn=chat_fn)
    chain = cfg.get("synth_model") or "none"
    synth = str((chain if isinstance(chain, list) else [chain])[0])
    gaps = re.split(r"(?im)^\W*gaps\W*$|^\W*gaps\s*:", run["answer"] or "", maxsplit=1)
    if len(gaps) == 2 and gaps[1].strip() and synth != "none" and int(cfg.get("gap_steps") or 0) > 0:
        tried = "\n".join("- %s %s" % (st["tool"], json.dumps(st["args"], ensure_ascii=False)[:160])
                          for st in run["steps"][-25:])
        extra = ("A first pass by a smaller local model already ran these calls:\n%s\n\nIts brief left these "
                 "gaps:\n%s\n\nClose the gaps with a few targeted grep/read calls (different from the ones above), "
                 "then call finish." % (tried, gaps[1].strip()[:1500]))
        second = investigate(question, scope, paths, cfg, policy, int(cfg["gap_steps"]),
                             float(cfg.get("gap_budget_s") or 150), synth, progress=progress, cancelled=cancelled,
                             chat_fn=chat_fn, ctx=run["ctx"], extra=extra)
        if second["answer"]:
            models = dict(run.get("models") or {})
            for k, v in (second.get("models") or {}).items():
                models[k] = models.get(k, 0) + v
            run.update(answer=second["answer"], files=second["files"], urls=second["urls"],
                       steps=run["steps"] + second["steps"], models=models, turns=run["turns"] + second["turns"],
                       seconds=round(run["seconds"] + second["seconds"], 1),
                       tool_chars=run["tool_chars"] + second["tool_chars"],
                       messages=run["messages"] + [{"role": "system", "content": "--- gap pass ---"}] +
                       second["messages"],
                       driver_notes=run["driver_notes"] + ["gaps closed by %s in %d calls" % (synth, len(
                           second["steps"]))] + second["driver_notes"])
    if not run["answer"]:
        write_trace({"event": "failed", "question": question[:300], "driver": run["driver"],
                     "notes": run["driver_notes"], "turns": run["turns"], "seconds": run["seconds"]})
        return "error: the local driver produced no brief (%s)" % "; ".join(run["driver_notes"] or ["no reason"])
    row = save_brief(question, scope, paths, key, run)
    remembered = awm_remember(row, run["answer"])
    write_trace({"event": "brief", "id": row["id"], "question": question[:300], "scope": scope,
                 "driver": run["driver"], "turns": run["turns"], "seconds": run["seconds"],
                 "tools": len(run["steps"]), "tool_chars": run["tool_chars"],
                 "brief_chars": len(run["answer"]), "awm": remembered, "driver_notes": run["driver_notes"]})
    steps_by = ", ".join("%s x%d" % kv for kv in sorted(run.get("models", {}).items(), key=lambda kv: -kv[1]))
    head = ("[crystal brief %s: %s, %d tool calls in %ss, %s chars of tool output kept local, %d files tracked%s]"
            % (row["id"], steps_by or run["driver"], len(run["steps"]), run["seconds"], format(run["tool_chars"], ","),
               len(run["files"]), ("; " + "; ".join(run["driver_notes"])) if run["driver_notes"] else ""))
    return head + "\n\n" + run["answer"]


def recall(args: Dict[str, Any]) -> str:
    query = str(args.get("query") or "")
    hits = search_briefs(query, _clamp(args.get("k"), 1, 10, 5))
    lines = ["%s (%s ago, %s): %s\n  %s" % (h["id"], _age(h["created"]), h.get("scope"), h["question"][:200],
                                            h.get("title", "")[:220]) for h in hits]
    mem = awm_recall(query, 5)
    if mem:
        lines.append("\nawm memory:\n" + mem)
    return "\n".join(lines) or "no stored brief or memory matches that query"


def _render_research(data: Any) -> str:
    if isinstance(data, str):
        return data[:9000]
    if not isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False)[:6000]
    body = ""
    for key in ("report", "answer", "synthesis", "summary", "result", "content"):
        if isinstance(data.get(key), str) and data[key].strip():
            body = data[key].strip()
            break
    sources = []
    for key in ("sources", "citations", "results"):
        for r in data.get(key) or []:
            if isinstance(r, dict) and r.get("url"):
                sources.append("- %s: %s" % (str(r.get("title") or "")[:120], r["url"]))
            elif isinstance(r, str) and r.startswith("http"):
                sources.append("- %s" % r)
        if sources:
            break
    if not body and not sources:
        return json.dumps(data, ensure_ascii=False)[:6000]
    return (body[:8000] + ("\n\nSources:\n" + "\n".join(sources[:15]) if sources else "")).strip()


def deep_research(args: Dict[str, Any]) -> str:
    """The fleet's DeepResearchPipeline (AitherSearch /research), crystallized like a research brief."""
    cfg = load_config()
    question = str(args.get("question") or "").strip()
    if not question:
        return "error: question is required"
    depth = str(args.get("depth") or "standard")
    depth = depth if depth in ("quick", "standard", "deep", "exhaustive") else "standard"
    budget = _clamp(args.get("budget_s"), 30, 1800, 300)
    key = _key(question, "deep-" + depth, [])
    if not args.get("fresh"):
        hit = fresh_brief(key, "web", cfg)
        if hit and not read_brief(hit["id"]).startswith("error:"):
            return "[crystal brief %s, reused: stored %s ago]\n\n%s" % (hit["id"], _age(hit["created"]),
                                                                       read_brief(hit["id"]))
    t0 = time.time()
    try:
        status, data = fleet_call(int(cfg.get("search_port") or 8114), "/research",
                                  {"query": question, "depth": depth, "budget_s": budget}, budget + 90)
    except (OSError, http.client.HTTPException) as exc:
        return "error: AitherSearch /research unreachable (%s)" % type(exc).__name__
    if status != 200:
        return "error: AitherSearch /research -> HTTP %s: %s" % (status, redact(str(data))[:300])
    text = redact(_render_research(data))
    urls = re.findall(r"https?://[^\s)>\]]+", text)[:30]
    run = {"answer": text, "notes": [], "files": {}, "urls": urls, "steps": [], "messages": [],
           "driver": "AitherSearch DeepResearchPipeline (%s)" % depth, "turns": 0,
           "seconds": round(time.time() - t0, 1), "tool_chars": len(json.dumps(data, ensure_ascii=False))}
    row = save_brief(question, "web", [], key, run)
    awm_remember(row, text)
    write_trace({"event": "deep_research", "id": row["id"], "question": question[:300], "depth": depth,
                 "seconds": run["seconds"], "chars": len(text)})
    return "[crystal brief %s: deep research (%s) in %ss]\n\n%s" % (row["id"], depth, run["seconds"], text)


def ask_aither(args: Dict[str, Any]) -> str:
    """Hand a task to an Aither agent on the awdk daemon: local models plus the gateway's 1,700+ tools."""
    cfg = load_config()
    message = str(args.get("message") or "").strip()
    if not message:
        return "error: message is required"
    agent = str(args.get("agent") or "aither")
    try:
        token = (HOME / ".aither" / "daemon-token").read_text(encoding="utf-8").strip()
    except OSError:
        return "error: no ~/.aither/daemon-token; the awdk daemon is not installed for this user"
    payload: Dict[str, Any] = {"message": message, "agent": agent,
                               "session_id": str(args.get("session_id") or "claude-%s-%d" % (agent, time.time()))}
    if args.get("model"):
        payload["model"] = str(args["model"])
    t0 = time.time()
    try:
        status, data = fleet_call(int(cfg.get("daemon_port") or 9001), "/chat", payload,
                                  float(_clamp(args.get("timeout_s"), 10, 1800, 600)),
                                  headers={"X-Aither-Local-Token": token}, tls=False)
    except (OSError, http.client.HTTPException) as exc:
        return "error: awdk daemon :%s unreachable (%s)" % (cfg.get("daemon_port"), type(exc).__name__)
    if status != 200 or not isinstance(data, dict):
        return "error: awdk daemon -> HTTP %s: %s" % (status, redact(str(data))[:300])
    seconds = round(time.time() - t0, 1)
    used = ", ".join(str(t) for t in (data.get("tool_calls") or [])) or "none"
    wrote = "; ".join(str(a.get("path") or a.get("name")) for a in (data.get("artifacts") or [])
                      if isinstance(a, dict))
    write_trace({"event": "ask_aither", "agent": agent, "model": data.get("model"), "seconds": seconds,
                 "tokens": data.get("tokens_used"), "tools": data.get("tool_calls"), "wrote": wrote})
    return redact("[%s on %s, %ss, %s tokens, session %s, tools: %s%s]\n\n%s" % (
        data.get("agent") or agent, data.get("model") or "?", seconds, data.get("tokens_used") or "?",
        payload["session_id"], used, ("; WROTE: " + wrote) if wrote else "", data.get("response") or ""))


def write_trace(row: Dict[str, Any]) -> None:
    try:
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        row = dict(row, ts=time.strftime("%Y-%m-%dT%H:%M:%S"))
        with open(TRACE_DIR / ("delegate-%s.jsonl" % time.strftime("%Y-%m-%d")), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ── MCP stdio server ─────────────────────────────────────────────────────────

MCP_TOOLS = [
    {"name": "research",
     "description": (
         "Hand exploration to the LOCAL fleet instead of spending frontier tokens on it. A local model "
         "(aither-orchestrator by default) runs up to 50 read-only tool calls (ripgrep, file reads, directory "
         "listings, web search, page fetches, memory recall) and returns only a short brief with path:line / URL "
         "citations. Briefs are crystallized: the same question returns the stored brief instantly while every "
         "file it cites is unchanged. Use it for 'where/how/why does X work', codebase surveys, multi-file "
         "tracing, web research, and anything that would take you more than ~5 searches or reads. Not for "
         "edits, and not for one file you already know."),
     "inputSchema": {"type": "object", "properties": {
         "question": {"type": "string", "description": "The question, with any names or paths you already know."},
         "scope": {"type": "string", "enum": ["code", "web", "both"], "default": "both"},
         "paths": {"type": "array", "items": {"type": "string"}, "description": "Optional roots to focus on."},
         "max_steps": {"type": "integer", "default": 50, "maximum": 80},
         "budget_s": {"type": "integer", "default": 300, "maximum": 1800},
         "fresh": {"type": "boolean", "default": False, "description": "Ignore a stored brief and re-investigate."},
         "model": {"type": "string", "description": "Local driver override, e.g. deepseek-v4-flash-pool."}},
         "required": ["question"]}},
    {"name": "recall",
     "description": ("Search crystallized briefs and Aither memory before investigating anything again. Instant; "
                     "returns brief ids, ages and first lines. Then read one with the brief tool."),
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "k": {"type": "integer"}},
                     "required": ["query"]}},
    {"name": "brief",
     "description": "Return a stored brief in full (answer, notes, sources) by id.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "deep_research",
     "description": ("Run the fleet's DeepResearchPipeline (AitherSearch) on a question about the OUTSIDE world: "
                     "papers, libraries, vendors, standards, current events. Multi-source web research with "
                     "citations, synthesized by the fleet's own models (deep and exhaustive use the local "
                     "DeepSeek pool), crystallized for reuse. For this codebase use research instead."),
     "inputSchema": {"type": "object", "properties": {
         "question": {"type": "string"},
         "depth": {"type": "string", "enum": ["quick", "standard", "deep", "exhaustive"], "default": "standard"},
         "budget_s": {"type": "integer", "default": 300, "maximum": 1800},
         "fresh": {"type": "boolean", "default": False}}, "required": ["question"]}},
    {"name": "ask_aither",
     "description": ("Launch an Aither subagent on the LOCAL fleet (awdk daemon; aither-orchestrator unless a "
                     "model is named) with the platform's tools through the gateway: codegraph, memory, search, "
                     "media-forge, fleet agents. Use it to hand off a whole task that the fleet's own tools do "
                     "better than yours. Unlike research it CAN write files and take actions, so give it an "
                     "explicit, scoped instruction and say what it must not touch. Returns its answer, the tools "
                     "it used, and every file it wrote."),
     "inputSchema": {"type": "object", "properties": {
         "message": {"type": "string", "description": "The task, with paths, constraints and the expected output."},
         "agent": {"type": "string", "default": "aither",
                   "description": "Agent persona on the daemon, e.g. aither, atlas, lyra, demi."},
         "model": {"type": "string", "description": "Spine model override, e.g. deepseek-v4-flash-pool."},
         "session_id": {"type": "string", "description": "Reuse to continue a conversation with that agent."},
         "timeout_s": {"type": "integer", "default": 600, "maximum": 1800}}, "required": ["message"]}},
]
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")


class Server:
    def __init__(self, stdin=None, stdout=None):
        self.stdin = stdin or sys.stdin.buffer
        self.stdout = stdout or sys.stdout.buffer
        self.lock = threading.Lock()
        self.cancels: Dict[Any, threading.Event] = {}

    def send(self, msg: Dict[str, Any]) -> None:
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        with self.lock:
            self.stdout.write(data)
            self.stdout.flush()

    def reply(self, mid: Any, result: Dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "id": mid, "result": result})

    def error(self, mid: Any, code: int, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}})

    def call(self, msg: Dict[str, Any]) -> None:
        mid = msg.get("id")
        params = msg.get("params") or {}
        name, args = params.get("name"), params.get("arguments") or {}
        token = (params.get("_meta") or {}).get("progressToken")
        flag = self.cancels.setdefault(mid, threading.Event())

        def progress(step: int, total: int, text: str) -> None:
            if token is not None:
                self.send({"jsonrpc": "2.0", "method": "notifications/progress",
                           "params": {"progressToken": token, "progress": step, "total": total, "message": text}})
        try:
            if name == "research":
                text = research(args, progress=progress, cancelled=flag.is_set)
            elif name == "recall":
                text = recall(args)
            elif name == "brief":
                text = read_brief(str(args.get("id") or ""))
            elif name == "deep_research":
                text = deep_research(args)
            elif name == "ask_aither":
                text = ask_aither(args)
            else:
                self.error(mid, -32602, "unknown tool %r" % name)
                return
            if not flag.is_set():
                self.reply(mid, {"content": [{"type": "text", "text": text}], "isError": text.startswith("error:")})
        except Exception as exc:  # never let one call take the server down
            _log("tool %s crashed: %s" % (name, exc))
            self.reply(mid, {"content": [{"type": "text", "text": "error: %s: %s" % (type(exc).__name__, exc)}],
                             "isError": True})
        finally:
            self.cancels.pop(mid, None)

    def handle(self, msg: Dict[str, Any]) -> None:
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            asked = str((msg.get("params") or {}).get("protocolVersion") or "")
            self.reply(mid, {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                             "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": {"name": "aither-local", "version": VERSION},
                             "instructions": ("Local-first exploration: call research before doing more than ~5 "
                                              "searches/reads yourself, and recall before re-investigating.")})
        elif method == "tools/list":
            self.reply(mid, {"tools": MCP_TOOLS})
        elif method == "tools/call":
            threading.Thread(target=self.call, args=(msg,), daemon=True).start()
        elif method == "ping":
            self.reply(mid, {})
        elif method == "notifications/cancelled":
            req = (msg.get("params") or {}).get("requestId")
            if req in self.cancels:
                self.cancels[req].set()
        elif method and method.startswith("notifications/"):
            pass
        elif mid is not None:
            self.error(mid, -32601, "method not found: %s" % method)

    def serve(self) -> None:
        for raw in self.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                self.error(None, -32700, "parse error")
                continue
            for item in (msg if isinstance(msg, list) else [msg]):
                if isinstance(item, dict):
                    self.handle(item)


# ── self-test ────────────────────────────────────────────────────────────────

def self_test() -> int:
    global STORE, TRACE_DIR, AWM
    AWM = ""  # offline: never write a test brief into the real awm memory
    tmp = Path(tempfile.mkdtemp(prefix="awdelegate-"))
    STORE, TRACE_DIR = tmp / "crystal", tmp / "trace"
    repo = tmp / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "router.py").write_text("def refresh_oauth():\n    return 'ok'\n", encoding="utf-8")
    fake_key = "sk" + "-" + "abcdefghijklmnopqrstuvwxyz"          # never a key-shaped literal in git
    (repo / ".env").write_text("API_KEY=%s\n" % fake_key, encoding="utf-8")
    (repo / "k.pem").write_text("x", encoding="utf-8")
    cfg = load_config()
    cfg["roots"] = [str(repo)]
    cfg["synth_model"] = "fake-synth"
    policy = Policy(cfg["roots"])
    for bad in (str(repo / ".env"), str(repo / "k.pem"), "C:/Windows/win.ini", str(repo / ".." / "x")):
        try:
            policy.resolve(bad, must_exist=False)
            raise AssertionError("policy allowed %s" % bad)
        except Denied:
            pass
    assert policy.resolve("pkg/router.py").name == "router.py"
    assert "sk" + "-" not in redact("key %s and API_KEY=%s" % ("sk" + "-ant-api03-" + "abcdefghijklmnopqrstu",
                                                                  "abcdefghij" + "klmnop1234"))
    assert redact("token_count: 12") == "token_count: 12"
    assert "[REDACTED]" in redact("Authorization: Bearer " + "abcdefghijklmnop" + "qrstuvwxyz012345")

    script = iter([
        {"tool_calls": [{"id": "1", "type": "function", "function": {"name": "grep", "arguments": json.dumps(
            {"pattern": "def refresh_oauth", "path": str(repo)})}}]},
        {"tool_calls": [{"id": "2", "type": "function", "function": {"name": "read", "arguments": json.dumps(
            {"path": (repo / "pkg" / "router.py").as_posix(), "limit": 5})}},
                        {"id": "3", "type": "function", "function": {"name": "note", "arguments": json.dumps(
            {"finding": "refresh_oauth returns ok", "source": "pkg/router.py:1"})}}]},
        {"tool_calls": [{"id": "4", "type": "function", "function": {"name": "read", "arguments": json.dumps(
            {"path": (repo / ".env").as_posix()})}}]},
        {"tool_calls": [{"id": "5", "type": "function", "function": {"name": "finish", "arguments": json.dumps(
            {"answer": "refresh_oauth is defined at %s:1.\nEvidence:\n- %s:1" % (
                (repo / "pkg" / "router.py").as_posix(), (repo / "pkg" / "router.py").as_posix())})}}]},
    ])
    seen: List[List[Dict[str, Any]]] = []

    def fake_chat(cfg_, model, messages, tools, max_tokens, timeout):
        if model == "fake-synth":
            assert tools is None and "def refresh_oauth" in messages[-1]["content"], "synth must see the evidence"
            router = (repo / "pkg" / "router.py").as_posix()
            return {"content": "refresh_oauth is defined at %s:1 (synthesized).\nEvidence:\n- %s:1\n- %s:99"
                               % (router, router, router)}, {}
        seen.append(json.loads(json.dumps(messages)))
        return next(script), {}

    import builtins  # noqa: F401  (keeps the namespace explicit for readers)
    orig_load = globals()["load_config"]
    globals()["load_config"] = lambda: cfg
    try:
        out = research({"question": "Where is refresh_oauth defined?", "scope": "code"}, chat_fn=fake_chat)
        assert out.startswith("[crystal brief "), out
        assert "router.py:1" in out and "(synthesized)" in out, out
        assert "`def refresh_oauth():`" in out, "the cited line must be re-read from disk"
        assert "NO SUCH LINE" in out, "a citation past the end of the file must be flagged"
        assert _render_research({"report": "R", "sources": [{"title": "T", "url": "https://x.org"}]}) == \
            "R\n\nSources:\n- T: https://x.org"
        tool_texts = [m["content"] for m in seen[-1] if m.get("role") == "tool"]
        assert any("def refresh_oauth" in t for t in tool_texts), tool_texts
        assert any("refused" in t for t in tool_texts), "the .env read must be refused"
        assert not any(fake_key in t for t in tool_texts), "secret leaked into the trajectory"
        again = research({"question": "where is refresh_oauth defined", "scope": "code"}, chat_fn=fake_chat)
        assert "reused" in again, again
        time.sleep(0.05)
        (repo / "pkg" / "router.py").write_text("def refresh_oauth():\n    return 'changed'\n", encoding="utf-8")
        assert fresh_brief(_key("Where is refresh_oauth defined?", "code", []), "code", cfg) is None, \
            "a changed cited file must invalidate the brief"
        assert search_briefs("refresh_oauth", 3), "recall must find the stored brief"
        bid = search_briefs("refresh_oauth", 1)[0]["id"]
        assert "## Notes" in read_brief(bid)
        assert (STORE / "trajectories" / ("%s.json" % bid)).exists()
    finally:
        globals()["load_config"] = orig_load

    import io
    feed = b"\n".join(json.dumps(m).encode() for m in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "nope"},
    ]) + b"\n"
    sink = io.BytesIO()
    Server(stdin=io.BytesIO(feed), stdout=sink).serve()
    replies = [json.loads(x) for x in sink.getvalue().splitlines()]
    assert replies[0]["result"]["protocolVersion"] == "2025-06-18", replies[0]
    assert [t["name"] for t in replies[1]["result"]["tools"]] == ["research", "recall", "brief", "deep_research",
                                                                  "ask_aither"]
    assert replies[2]["error"]["code"] == -32601
    print("awdelegate self-test: OK (%s)" % tmp.as_posix())
    return 0


def _out(text: str) -> None:
    # The Windows console is cp1252; briefs carry arrows and quotes. Bytes, not print().
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    sys.stdout.flush()


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="local research delegate (MCP stdio server)")
    parser.add_argument("--mcp", action="store_true")
    parser.add_argument("--ask")
    parser.add_argument("--scope", default="both")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--recall")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.ask:
        _out(research({"question": args.ask, "scope": args.scope, "fresh": args.fresh},
                      progress=lambda s, t, m: _log("step %d/%d %s" % (s, t, m))))
        return 0
    if args.recall:
        _out(recall({"query": args.recall}))
        return 0
    if args.mcp:
        Server().serve()
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
