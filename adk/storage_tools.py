"""Agent-side awstorage tools: SUGGEST a deletion, and stay off the sweep's list.

Two things, both small:

* ``suggest_deletion(path, reason, action="quarantine")`` -- an agent tool that files
  a deletion SUGGESTION with the awstorage brick (``awstorage.suggest``). The brick
  decides whether it is auto-approved, needs an owner decision card, is refused or is
  a duplicate; the agent never deletes anything and never approves its own
  suggestion. ``suggested_by`` is ``agent:<name>`` -- the agent's OWN name, bound at
  registration, never an argument the model can fill in. It is still only a LABEL:
  this runs in-process, where anything can type any name, so awstorage (>= 0.4.1)
  treats it as an UNVERIFIED identity -- no auto lane, every suggestion goes to an
  owner card -- unless the owner sets ``AWSTORAGE_TRUST_INPROCESS=1`` or installs an
  identity verifier (``awstorage.set_identity_verifier``).
* the live-ids contract (``AWSTORAGE_LIVE_IDS`` env + a live-ids FILE, see
  ``awstorage.sweep.load_live_ids``): while an agent runs, its session scratch dir is
  registered as live so a sweep never reaps a running agent's work. A distinctive
  basename is registered as an id; a GENERIC one (``scratchpad``, ``tmp``, ``temp``,
  ``work``, ``cache``, ``scratch``) is registered by FULL PATH instead (awstorage >=
  0.4.1 matches path ids), so one agent never shields every ``tmp`` on the host. The
  env var covers a sweep this process spawns (plain ids only); the file (default
  ``~/.aither/awstorage/live-ids.txt``, override ``AWSTORAGE_LIVE_IDS_FILE``) covers a
  sweep run elsewhere with ``--live-ids <file>``. File writes hold a CROSS-PROCESS
  lock (an ``O_EXCL`` lockfile beside it), and every write prunes lines whose process
  is gone or that are older than ``AWSTORAGE_LIVE_IDS_TTL_S`` (default 7 days).

awstorage is OPTIONAL: when it is absent, or too old to have ``suggest``, the tool
says so plainly -- it never reports a suggestion that was not filed.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import re
import threading
import time
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Optional

if TYPE_CHECKING:
    from adk.agent import AitherAgent

logger = logging.getLogger("adk.storage_tools")

LIVE_IDS_ENV = "AWSTORAGE_LIVE_IDS"
LIVE_IDS_FILE_ENV = "AWSTORAGE_LIVE_IDS_FILE"
LIVE_IDS_TTL_ENV = "AWSTORAGE_LIVE_IDS_TTL_S"
DEFAULT_LIVE_TTL_S = 7 * 24 * 3600
#: Env naming the running session's scratch dir when the host harness knows it.
SCRATCH_ENV = "ADK_SESSION_SCRATCH_DIR"
SUGGEST_ACTIONS = ("quarantine", "delete")
#: Leaf names too common to register as a bare id: they go in by full path.
GENERIC_LEAVES = frozenset({"scratchpad", "scratch", "tmp", "temp", "work", "cache"})
LOCK_TIMEOUT_S = 10.0
LOCK_STALE_S = 60.0

_LOCK = threading.Lock()
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
_AGENT_RE = re.compile(r"^[A-Za-z0-9._:@-]{1,64}$")
#: awstorage.suggest's own suggested_by alphabet.
_SUGGESTER_BAD = re.compile(r"[^A-Za-z0-9_.:@/-]+")
_TAG_RE = re.compile(r"(\w+)=(\S+)")


# ---------------------------------------------------------------------------
# awstorage (optional brick)
# ---------------------------------------------------------------------------

def _awstorage_suggest():
    """(awstorage.suggest, None) or (None, why). Never raises."""
    try:
        import awstorage  # noqa: PLC0415
    except ImportError:
        return None, ("awstorage not installed: pip install awstorage (or awdk[storage]); "
                      "no suggestion was filed")
    fn = getattr(awstorage, "suggest", None)
    if not callable(fn):
        have = getattr(awstorage, "__version__", "?")
        return None, (f"awstorage {have} has no suggest(); upgrade awstorage. "
                      "No suggestion was filed")
    return fn, None


def suggester_label(agent_name: str) -> str:
    """``agent:<name>`` in awstorage's suggested_by alphabet (<= 128 chars)."""
    name = _SUGGESTER_BAD.sub("-", (agent_name or "").strip()).strip("-") or "unknown"
    return f"agent:{name}"[:128]


def make_suggest_deletion(agent_name: str):
    """Build the ``suggest_deletion`` tool bound to ``agent_name`` (the suggester)."""
    who = suggester_label(agent_name)

    def suggest_deletion(path: str, reason: str, action: str = "quarantine") -> str:
        """Suggest that a file or directory be removed; the OWNER decides.

        Nothing is deleted by this call. awstorage classifies the path and either
        auto-approves it (policy says it is safe and re-fetchable, and the owner has
        opted in to trusting agents), queues it for an owner decision card, refuses
        it, or reports a duplicate suggestion.

        path: absolute path to suggest removing.
        reason: why it can go (what it is, why it is not needed) -- shown to the owner.
        action: "quarantine" (default, reversible) or "delete".
        """
        if not str(path or "").strip():
            return json.dumps({"error": "path is required"})
        if not str(reason or "").strip():
            return json.dumps({"error": "reason is required: the owner reads it"})
        act = str(action or "quarantine").strip().lower()
        if act not in SUGGEST_ACTIONS:
            return json.dumps({"error": f"action must be one of {list(SUGGEST_ACTIONS)}"})
        fn, why = _awstorage_suggest()
        if fn is None:
            return json.dumps({"error": why, "filed": False})
        try:
            out = fn(str(path), reason=str(reason)[:2000], suggested_by=who, action=act)
        except Exception as exc:  # noqa: BLE001 -- a tool returns the failure, it never raises
            return json.dumps({"error": f"awstorage.suggest failed: "
                                        f"{type(exc).__name__}: {exc}", "filed": False})
        return json.dumps(out if isinstance(out, dict) else {"result": out}, default=str)

    return suggest_deletion


# ---------------------------------------------------------------------------
# live-ids contract
# ---------------------------------------------------------------------------

def live_ids_file() -> Path:
    """The shared live-ids file a sweep reads with ``--live-ids``."""
    raw = os.environ.get(LIVE_IDS_FILE_ENV, "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".aither" / "awstorage" / "live-ids.txt"


def _live_ttl() -> float:
    try:
        return max(60.0, float(os.environ.get(LIVE_IDS_TTL_ENV, "") or DEFAULT_LIVE_TTL_S))
    except ValueError:
        return float(DEFAULT_LIVE_TTL_S)


def path_id(p: Any) -> Optional[str]:
    """A full-path live id (absolute, ``/``-separated), or None if unusable in the
    line format (``#`` starts a comment; a newline would split the line)."""
    try:
        s = os.path.abspath(os.path.expanduser(str(p))).replace("\\", "/").rstrip("/")
    except (TypeError, ValueError):
        return None
    if not s or "/" not in s or len(s) > 1024 or any(c in s for c in "#\r\n\0"):
        return None
    return s


def _is_path_id(i: str) -> bool:
    return "/" in i and path_id(i) == i


def _valid_id(i: Any) -> bool:
    return isinstance(i, str) and bool(i) and (bool(_ID_RE.match(i)) or _is_path_id(i))


def scratch_live_ids(scratch_dir: str) -> list[str]:
    """Live ids for a scratch dir.

    A distinctive basename is the id. A GENERIC basename (``scratchpad``, ``tmp``...)
    is registered by its FULL PATH, plus the parent's basename when that is the
    distinctive session id (``.../<session-id>/scratchpad``)."""
    p = Path(str(scratch_dir)).expanduser()
    leaf = p.name
    ids: list[str] = []
    if leaf and leaf.lower() not in GENERIC_LEAVES and _ID_RE.match(leaf):
        ids.append(leaf)
    else:
        full = path_id(p)
        if full:
            ids.append(full)
        parent = p.parent.name
        if parent and parent.lower() not in GENERIC_LEAVES and _ID_RE.match(parent):
            ids.append(parent)
    return ids


def _env_ids() -> list[str]:
    return [t for t in re.split(r"[,;\s]+", os.environ.get(LIVE_IDS_ENV, "")) if t]


def _file_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text("".join(f"{ln}\n" for ln in lines), encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def _file_lock(target: Path, timeout: float = LOCK_TIMEOUT_S) -> Iterator[None]:
    """Exclusive CROSS-PROCESS lock beside ``target`` (O_CREAT|O_EXCL, portable; the
    harness daemon's registry pattern). A lock older than LOCK_STALE_S is a crashed
    holder and is taken over. Raises TimeoutError (an OSError) when it cannot."""
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_name(target.name + ".lock")
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE_S:
                    lock.unlink()
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                raise TimeoutError(f"live-ids lock {lock} held for {timeout}s") from None
            time.sleep(0.05)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()


def _pid_alive(pid: int) -> bool:
    """Is ``pid`` running? Unknown -> True (never prune on a guess). Never
    ``os.kill(pid, 0)`` on Windows: there it TERMINATES the process."""
    if pid == os.getpid():
        return True
    try:
        from adk.decisions.winproc import pid_alive  # noqa: PLC0415
    except Exception:  # noqa: BLE001 -- cannot judge: keep the line
        return True
    try:
        return bool(pid_alive(int(pid)))
    except Exception:  # noqa: BLE001
        return True


def _line_tags(line: str) -> dict[str, str]:
    if "#" not in line:
        return {}
    return dict(_TAG_RE.findall(line.split("#", 1)[1]))


def _prune(lines: list[str], now: float) -> list[str]:
    """Drop registrations whose process is gone or that outlived the TTL. Lines this
    module did not write (no ``pid=`` tag: an owner's manual entry) are kept."""
    ttl = _live_ttl()
    kept = []
    for ln in lines:
        tags = _line_tags(ln)
        pid = tags.get("pid", "")
        if not pid.isdigit():
            kept.append(ln)
            continue
        ts = tags.get("ts", "")
        if ts.isdigit() and now - int(ts) > ttl:
            continue
        if not _pid_alive(int(pid)):
            continue
        kept.append(ln)
    return kept


def register_live_ids(ids: list[str], *, owner: str = "") -> list[str]:
    """Mark ``ids`` live in the env (plain ids) and the shared file. Returns the ids
    registered.

    Each file line is ``<id>  # pid=<pid> ts=<epoch> agent=<name>``: unregister removes
    only this process's lines (a peer's registration of the same id survives), and a
    later write prunes it once the process is gone or the TTL passed. Invalid ids and
    agent names are refused (an id becomes a line in a shared file).
    """
    clean = [i for i in ids if _valid_id(i)]
    if not clean:
        return []
    agent = owner if owner and _AGENT_RE.match(owner) else ("invalid" if owner else "")
    now = time.time()
    tag = f"pid={os.getpid()} ts={int(now)}" + (f" agent={agent}" if agent else "")
    with _LOCK:
        env = _env_ids()
        plain = [i for i in clean if not _is_path_id(i) and i not in env]
        if plain:
            os.environ[LIVE_IDS_ENV] = ",".join(env + plain)
        path = live_ids_file()
        try:
            with _file_lock(path):
                lines = _prune(_file_lines(path), now)
                have = {ln.split("#", 1)[0].strip() for ln in lines
                        if _line_tags(ln).get("pid") == str(os.getpid())}
                lines += [f"{i}  # {tag}" for i in clean if i not in have]
                _write_lines(path, lines)
        except OSError as exc:
            # The env half still holds for a sweep this process spawns; say the file
            # half failed rather than pretend the id is visible fleet-wide.
            logger.warning("awstorage live-ids file %s not updated: %s", path, exc)
    return clean


def unregister_live_ids(ids: list[str]) -> None:
    """Undo :func:`register_live_ids` for THIS process's lines only (and prune)."""
    drop = set(ids)
    if not drop:
        return
    with _LOCK:
        os.environ[LIVE_IDS_ENV] = ",".join(i for i in _env_ids() if i not in drop)
        if not os.environ[LIVE_IDS_ENV]:
            os.environ.pop(LIVE_IDS_ENV, None)
        path = live_ids_file()
        try:
            with _file_lock(path):
                lines = _file_lines(path)
                mine = str(os.getpid())
                kept = [ln for ln in lines
                        if not (ln.split("#", 1)[0].strip() in drop
                                and _line_tags(ln).get("pid") == mine)]
                kept = _prune(kept, time.time())
                if kept != lines:
                    _write_lines(path, kept)
        except OSError as exc:
            logger.warning("awstorage live-ids file %s not cleaned: %s", path, exc)


@contextmanager
def live_session(scratch_dir: Optional[str], *, owner: str = "") -> Iterator[list[str]]:
    """Register a scratch dir as live for the duration of the ``with`` block."""
    ids = register_live_ids(scratch_live_ids(scratch_dir), owner=owner) if scratch_dir else []
    try:
        yield ids
    finally:
        unregister_live_ids(ids)


def _agent_scratch_dir(agent: Any) -> Optional[str]:
    for attr in ("scratch_dir", "scratchpad", "session_scratch_dir"):
        val = getattr(agent, attr, None)
        if val:
            return str(val)
    return os.environ.get(SCRATCH_ENV, "").strip() or None


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def register_storage_tools(agent: "AitherAgent") -> int:
    """Register ``suggest_deletion`` bound to this agent, and keep its scratch live.

    The live registration lasts as long as the agent object (released when it is
    garbage-collected or the process exits).
    """
    name = str(getattr(agent, "name", "") or "unknown")
    agent._tools.register(
        make_suggest_deletion(name), name="suggest_deletion",
        description=("Suggest a file/dir for removal (quarantine by default). The owner "
                     "decides via a decision card; nothing is deleted by this call."))
    scratch = _agent_scratch_dir(agent)
    if scratch:
        ids = register_live_ids(scratch_live_ids(scratch), owner=name)
        if ids:
            try:
                weakref.finalize(agent, unregister_live_ids, ids)
            except TypeError:  # an agent that cannot be weak-referenced: exit only
                atexit.register(unregister_live_ids, ids)
            logger.info("awstorage live ids registered for %s: %s", name, ids)
    return 1
