"""An owner-approved, scoped local credential for the Awconnect browser extension.

With ``AITHER_LOCAL_AUTH=required`` (the default on an offline box) every daemon
route wants ``~/.aither/daemon-token``, which no browser can read -- so the
extension's Local mode answered 401 on ``/chat/stream`` and ``/mcp``. The fix is
not to weaken the gate, and not to paste the owner's token into the browser. It
is a pairing the OWNER approves out of band:

1. The extension asks for a pairing (``POST /local/extension-pair/start``).
   Only an EXACT allowlisted ``chrome-extension://<id>`` Origin (adk.extension_id
   holds the first-party ids) on a loopback ``Host`` is answered, and nothing is
   granted yet: it receives a ``pair_id`` and a six-digit code.
2. The owner approves where the extension cannot reach:
   ``adk awconnect pair approve <code>``. That call must present
   ``~/.aither/daemon-token`` -- a file only the owner's own user can read.
3. The extension collects its token ONCE (``POST /local/extension-pair/poll``)
   and sends it as ``Authorization: Bearer`` on the few local routes Local mode
   uses. The token opens ONLY those routes, expires, and is stored HASHED.

Threat model -- what each attacker can and cannot do with this:

* A web page on any site cannot set ``Origin: chrome-extension://...`` (the
  browser sets that header and page script cannot forge it), so it can neither
  start nor poll a pairing, and the token lives in the extension's own storage,
  not anywhere a page can read.
* Another extension's Origin is not an allowlisted extension origin, so its
  start/poll answers 403 and it can never mint.
* A NON-browser local process CAN forge the Origin header. It still cannot
  approve (the daemon-token is readable only by the owner's user) and cannot
  hold a token (only sha256 is on disk, and it expires), so it stays at 401.
  ``local_auth``'s rule survives: loopback names a machine, not a person.
* A request that carries ANY Origin must carry an allowlisted extension origin:
  the token is refused from every other origin, so even a leaked token is
  useless to a first-party web page that the CSRF guard would otherwise admit.
* The pairing CODE alone is no credential -- approval needs the owner's local
  token -- and the CLI prints the requesting origin, so an approval the user did
  not initiate is visible and refusable.

The token never opens ``/cli/execute``, session control, the browser-pair door
or the mesh routes: it is bound to :data:`SCOPE_PATHS`, lives
:data:`TOKEN_TTL_S` seconds, and ``adk awconnect pair revoke`` clears it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

from adk.extension_id import allowed_extension_origins
from adk.harnesses.pairing import PAIR_TTL_S, PairingBook, is_loopback_host

__all__ = ["MAX_TOKENS", "PAIR_TTL_S", "SCOPE", "SCOPE_PATHS", "TOKEN_PREFIX",
           "TOKEN_TTL_S", "PairingBook", "is_loopback_host", "issue", "path_allowed",
           "revoke_all", "store_path", "token_allows"]

logger = logging.getLogger("adk.extension_pair")

#: Tokens are prefixed so a value in a log or a header is recognisably this
#: credential and never mistaken for a session bearer.
TOKEN_PREFIX = "aep_"
SCOPE = "extension"
#: 30 days. Re-pairing needs the owner at a terminal, and the token is no
#: stronger than the surfaces it opens -- the same user can already read
#: ``~/.aither/daemon-token`` and ``~/.aither/auth.json`` directly -- so a
#: shorter clock would buy monthly friction, not isolation.
TOKEN_TTL_S = 30 * 86400
#: Live tokens kept. A re-pairing loop evicts the OLDEST, never the new one.
MAX_TOKENS = 64

#: What a paired extension may reach. ``"METHOD /prefix"`` matches THAT method
#: on the exact path or one under it (``/agents/<name>/...`` for a GET), so the
#: chat lane, the agent list, the plain-completion fallback, the tools panel's
#: MCP session and the identity hand-off are open -- and ``/cli/execute``,
#: ``/x-session/import``, ``/local/browser-pair``, ``/mesh`` and every other
#: route are not. No method, no match: fail closed.
SCOPE_PATHS: Tuple[str, ...] = (
    "GET /agents",
    "POST /chat/stream",
    "POST /v1/chat/completions",
    "POST /mcp",
    "GET /identity/whoami",
    "POST /identity/handoff",
)

_lock = threading.Lock()


def store_path() -> Path:
    """Where paired-token digests live (beside the extension allowlist)."""
    base = os.environ.get("AITHER_HOME")
    home = Path(base) if base else Path.home() / ".aither"
    return home / "awconnect" / "paired_tokens.json"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def path_allowed(path: str, method: str) -> bool:
    """Does the scope open ``method path``? An unknown path is refused."""
    if not method:
        return False
    method = method.upper()
    for entry in SCOPE_PATHS:
        verb, _, prefix = entry.partition(" ")
        if method == verb and (path == prefix or path.startswith(prefix + "/")):
            return True
    return False


@contextmanager
def _file_lock(target: Path, timeout: float = 5.0) -> Iterator[None]:
    """An exclusive lock beside the store (O_CREAT|O_EXCL, portable). Two daemons
    sharing one home must not read-modify-write this file concurrently; a lock
    older than 60 s is a crashed holder and is taken over."""
    lock = target.with_name(target.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 60:
                    lock.unlink()
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                raise TimeoutError(f"paired-token store lock {lock} held for {timeout}s")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lock.unlink()
        except OSError as exc:
            logger.warning("could not release paired-token store lock %s: %s", lock, exc)


def _load(path: Optional[Path] = None, now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    """``digest -> {origin, minted_at, expires_at}`` for LIVE entries. An
    unreadable or malformed file is empty: tokens are re-paired, never trusted."""
    t = time.time() if now is None else now
    try:
        data = json.loads((path or store_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(rows, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for digest, rec in rows.items():
        if not isinstance(rec, dict):
            continue
        try:
            if float(rec.get("expires_at") or 0) > t:
                out[str(digest)] = rec
        except (TypeError, ValueError):
            continue
    return out


def _save(rows: Mapping[str, Dict[str, Any]], path: Optional[Path] = None) -> None:
    target = path or store_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps({"version": 1, "tokens": dict(rows)}, indent=1),
                   encoding="utf-8", newline="\n")
    os.replace(tmp, target)


def issue(origin: str, now: Optional[float] = None) -> Tuple[str, int]:
    """Mint a token for a pairing the OWNER approved. The plaintext is returned
    ONCE; only sha256 is kept. Returns ``(token, ttl_seconds)``."""
    t = time.time() if now is None else now
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    with _lock, _file_lock(store_path()):
        rows = _load(now=t)
        rows[_digest(token)] = {"origin": origin, "minted_at": t, "expires_at": t + TOKEN_TTL_S}
        while len(rows) > MAX_TOKENS:
            oldest = min(rows, key=lambda k: float(rows[k].get("minted_at") or 0))
            rows.pop(oldest, None)
        _save(rows)
    return token, TOKEN_TTL_S


def token_allows(token: str, path: str, method: str, origin: Optional[str] = None,
                 now: Optional[float] = None) -> bool:
    """May this bearer make ``method path`` -- and, when the request carried an
    Origin, only from an allowlisted extension origin?"""
    if not token or not token.startswith(TOKEN_PREFIX) or not path_allowed(path, method):
        return False
    if origin and origin not in allowed_extension_origins():
        return False
    t = time.time() if now is None else now
    rec = _load(now=t).get(_digest(token))
    try:
        return bool(rec) and float(rec.get("expires_at") or 0) > t
    except (TypeError, ValueError):
        return False


def revoke_all() -> int:
    """Drop every paired token. Returns how many live ones there were."""
    with _lock, _file_lock(store_path()):
        count = len(_load())
        _save({})
    return count
