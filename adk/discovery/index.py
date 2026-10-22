"""Build and read the discovery index.

Layout (``AITHER_DISCOVERY_DIR`` overrides the directory)::

    ~/.aither/discovery/user-<fp12>.json   sources that do not depend on the cwd, one
                                           per CALLER (fp = sha256 of the bearer)
    ~/.aither/discovery/cwd-<sha12>.json   one per working directory
    ~/.aither/discovery/last_refresh.json  the receipt of the last full build

Each part is ``{"schema", "part", "built_at", "adk_version", "cwd", "caller",
"caller_error", "sources": {id: receipt}, "dropped": {reason: n}, "cards": [...]}``,
written atomically (temp file + ``os.replace``). The user part is keyed by the bearer
fingerprint, so a tenant session and the owner never share one file; the cwd part
holds only local files, and every read re-filters with the reading caller.

A lock file keeps two builders of one part from running at once; a lock whose pid is
dead, or that is older than LOCK_STALE_S, is a dead builder's and is taken over.

Refresh: nothing in a session hook builds. An hourly ``awrise`` wake runs a full
:func:`build` (``adk discover build --all-cwds``), and :func:`build` writes
``last_refresh.json``, the receipt the wake and CD001 read.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from adk.discovery import scope, sources

SCHEMA = 2
LOCK_STALE_S = 300.0
#: The wake's cadence; CD001 holds the index and its receipt to 2x this.
REFRESH_EVERY_S = 3600.0
RECEIPT = "last_refresh.json"


def index_dir() -> Path:
    override = os.environ.get("AITHER_DISCOVERY_DIR", "").strip()
    if override:
        return Path(override)
    home = Path(os.environ.get("AITHER_HOME_OVERRIDE", "") or Path.home())
    return home / ".aither" / "discovery"


def default_cwd() -> str:
    """The project a session belongs to: Claude Code's ``CLAUDE_PROJECT_DIR`` (a shell
    in a subdirectory still belongs to the project), else the process cwd."""
    return os.environ.get("CLAUDE_PROJECT_DIR", "").strip() or os.getcwd()


def cwd_key(cwd: "os.PathLike[str] | str") -> str:
    norm = os.path.normcase(str(Path(cwd).resolve()))
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]


def user_key(fp: Optional[str] = None) -> str:
    """The user part's key: the bearer fingerprint, ``anon`` when there is no bearer."""
    got = sources.bearer_fingerprint() if fp is None else fp
    return got or "anon"


def part_path(part: str, cwd: "os.PathLike[str] | str | None" = None,
              fp: Optional[str] = None) -> Path:
    if part == "user":
        return index_dir() / f"user-{user_key(fp)}.json"
    return index_dir() / f"cwd-{cwd_key(cwd or default_cwd())}.json"


def receipt_path() -> Path:
    return index_dir() / RECEIPT


def _adk_version() -> str:
    try:
        import adk
        return str(getattr(adk, "__version__", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def read_part(path: Path) -> Optional[dict]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == SCHEMA else None


def _write_atomic(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):  # the temp file may never have been created
            os.unlink(tmp)
        raise


def pid_alive(pid: int) -> bool:
    """True when ``pid`` is a running process. Unknown (0, or no way to ask) is alive,
    so the age rule still governs such a lock."""
    if pid <= 0:
        return True
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, owned by someone else
    return True


class _Lock:
    """An O_EXCL lock file holding the builder's pid. A lock whose pid is dead, or that
    is older than LOCK_STALE_S, is a dead builder's and is removed at once."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.held = False

    def _stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return True  # vanished between the open and the stat: try again
        if age > LOCK_STALE_S:
            return True
        try:
            pid = int(self.path.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            return False  # being written right now
        return not pid_alive(pid)

    def __enter__(self) -> "_Lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._stale():
                    with contextlib.suppress(OSError):
                        self.path.unlink()
                    continue
                return self
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self.held = True
            return self
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.held:
            with contextlib.suppress(OSError):  # a stale-lock sweep may have removed it
                self.path.unlink()


def _run_sources(table: list, cwd: Path, previous: Optional[dict], force: bool,
                 now: float, caller_fp: str = "") -> "tuple[list, dict]":
    """Run each source, or reuse its cached cards within its TTL.

    A caller-scoped source (the gateway's lists) is reused ONLY when the cached
    receipt was fetched with the same bearer fingerprint; its cards are stamped
    ``fetched_for`` so the scope filter can refuse them to anyone else.
    """
    prev_sources = (previous or {}).get("sources") or {}
    prev_cards = (previous or {}).get("cards") or []
    cards: list = []
    receipts: dict = {}
    for sid, fn in table:
        scoped = sid in sources.CALLER_SCOPED
        prev = prev_sources.get(sid)
        ttl = sources.NETWORK_TTL_S.get(sid)
        same_caller = (not scoped) or (isinstance(prev, dict)
                                       and prev.get("caller_fp") == caller_fp)
        if (not force and ttl is not None and isinstance(prev, dict) and prev.get("ok")
                and same_caller and now - float(prev.get("at") or 0) < ttl):
            reused = [c for c in prev_cards if c.get("_src") == sid]
            cards += reused
            receipts[sid] = dict(prev, reused=True)
            continue
        try:
            got, rec = fn(cwd)
        except Exception as exc:  # noqa: BLE001 - a source must never sink the build
            got, rec = [], {"ok": False, "count": 0, "error": f"{type(exc).__name__}: {exc}"}
        for c in got:
            c["_src"] = sid
            if scoped:
                c["fetched_for"] = caller_fp
        rec = dict(rec, at=now, reused=False)
        if scoped:
            rec["caller_fp"] = caller_fp
        receipts[sid] = rec
        cards += got
    return cards, receipts


def _dedupe(cards: list) -> list:
    """First card per (domain, name) wins; the source tables are ordered by priority."""
    seen: set = set()
    out = []
    for c in cards:
        name = str(c.get("name")).lower()
        if c.get("domain") == "skills":
            # `awsh:foo` from a plugin and a project's own `foo` are one skill to a reader;
            # listing both is the duplicate-pair tax every session pays.
            name = name.split(":", 1)[-1]
        key = (c.get("domain"), name)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def build_part(part: str, cwd: "os.PathLike[str] | str | None" = None, force: bool = False,
               caller: Optional[scope.Caller] = None,
               table: Optional[list] = None, caller_error: str = "") -> dict:
    """Build one part and write it. Returns the written document."""
    cwd_p = Path(cwd or default_cwd()).resolve()
    caller_err = caller_error
    if caller is None:
        caller, caller_err = sources.gateway_caller()
    path = part_path(part, cwd_p, fp=caller.fingerprint)
    now = time.time()
    previous = read_part(path)
    if table is None:
        table = sources.USER_SOURCES if part == "user" else sources.CWD_SOURCES
    with _Lock(path.with_suffix(".lock")) as lock:
        if not lock.held:
            return previous or {}
        cards, receipts = _run_sources(table, cwd_p, previous, force, now,
                                       caller_fp=caller.fingerprint)
        kept, dropped = scope.filter_cards(_dedupe(cards), caller)
        doc = {"schema": SCHEMA, "part": part, "built_at": now, "adk_version": _adk_version(),
               "cwd": str(cwd_p) if part == "cwd" else "", "caller": caller.as_dict(),
               "caller_error": caller_err, "sources": receipts, "dropped": dropped,
               "cards": kept}
        _write_atomic(path, doc)
    return doc


def build(cwd: "os.PathLike[str] | str | None" = None, force: bool = False,
          parts: tuple = ("user", "cwd"), caller: Optional[scope.Caller] = None,
          caller_error: str = "") -> dict:
    """Build every part, then write the refresh receipt. Returns ``{part: doc}``.

    The caller is resolved ONCE, up front, and passed down explicitly, so nothing in the
    build re-reads the environment halfway through.
    """
    if caller is None:
        caller, caller_error = sources.gateway_caller()
    out = {}
    t0 = time.time()
    err = ""
    try:
        for part in parts:
            out[part] = build_part(part, cwd=cwd, force=force, caller=caller,
                                   caller_error=caller_error)
    except Exception as exc:  # noqa: BLE001 - recorded in the receipt, then re-raised
        err = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _write_receipt(out, caller, t0, err)
    legacy = index_dir() / "user.json"  # schema 1: one user part shared by every caller
    with contextlib.suppress(OSError):
        legacy.unlink()
    return out


def _write_receipt(out: dict, caller: scope.Caller, t0: float, err: str) -> None:
    dead = sorted({sid for doc in out.values() for sid, r in
                   ((doc or {}).get("sources") or {}).items()
                   if isinstance(r, dict) and not r.get("ok")})
    present = {sid for doc in out.values() for sid in ((doc or {}).get("sources") or {})}
    critical_dead = []
    for domain, sids in sources.CRITICAL.items():
        known = [s for s in sids if s in present]
        if known and all(s in dead for s in known):
            critical_dead.append(domain)
    rc = 1 if (err or critical_dead or not out) else 0
    doc = {"exit_code": rc, "ts": time.time(), "seconds": round(time.time() - t0, 2),
           "parts": {p: {"cards": len((d or {}).get("cards") or []),
                         "path": str(part_path(p, (d or {}).get("cwd") or None,
                                               fp=caller.fingerprint))}
                     for p, d in out.items()},
           "caller_fp": caller.fingerprint, "dead_sources": dead,
           "critical_dead": critical_dead, "error": err}
    with contextlib.suppress(OSError):
        _write_atomic(receipt_path(), doc)


def read_receipt() -> Optional[dict]:
    try:
        doc = json.loads(receipt_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def known_cwds() -> list:
    """Every directory that already has a cwd part and still exists (the wake refreshes
    each one, so a worktree's skills stay as fresh as the main checkout's)."""
    out = []
    for p in sorted(index_dir().glob("cwd-*.json")):
        doc = read_part(p)
        cwd = str((doc or {}).get("cwd") or "")
        if cwd and Path(cwd).is_dir():
            out.append(cwd)
    return out


def detach_build(cwd: "os.PathLike[str] | str | None" = None, force: bool = False) -> int:
    """Start ``adk discover build`` in a detached process and return at once.

    For interactive use only. Session hooks must NOT call this (a spawn per session
    leaks kernel pool on Windows); the hourly awrise wake is the refresher.
    """
    import subprocess
    args = [sys.executable, "-m", "adk.discovery.cli", "build"]
    if force:
        args.append("--force")
    kw: dict = {"cwd": str(cwd or default_cwd()), "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED|NEWGROUP|NOWINDOW
    else:
        kw["start_new_session"] = True
    proc = subprocess.Popen(args, **kw)  # noqa: S603 - our own interpreter and module
    return proc.pid


def load_merged(cwd: "os.PathLike[str] | str | None" = None,
                fp: Optional[str] = None) -> dict:
    """This caller's user part and this cwd's part merged: ``{"cards", "parts"}``.

    ``fp`` defaults to this process's bearer fingerprint, so a session reads only the
    user part built for its own bearer. A cwd card shadows a user card of the same
    (domain, name) -- the project-local claim is the more specific one.
    """
    parts = {}
    cards: list = []
    for part in ("cwd", "user"):
        path = part_path(part, cwd, fp=fp)
        doc = read_part(path)
        if doc is None:
            parts[part] = None
            continue
        parts[part] = {k: doc.get(k) for k in ("built_at", "adk_version", "cwd", "caller",
                                               "sources", "dropped")}
        try:
            parts[part]["mtime"] = path.stat().st_mtime
        except OSError:
            parts[part]["mtime"] = 0.0
        cards += doc.get("cards") or []
    return {"cards": _dedupe(cards), "parts": parts,
            "fingerprint": user_key(fp)}


def is_stale(part_meta: Optional[dict], max_age_s: float, now: Optional[float] = None) -> bool:
    if not part_meta:
        return True
    t = time.time() if now is None else now
    return t - float(part_meta.get("built_at") or 0) > max_age_s


__all__ = ["REFRESH_EVERY_S", "build", "build_part", "cwd_key", "default_cwd",
           "detach_build", "index_dir", "is_stale", "known_cwds", "load_merged",
           "part_path", "pid_alive", "read_part", "read_receipt", "receipt_path",
           "user_key"]
