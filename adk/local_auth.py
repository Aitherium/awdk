"""The per-user local credential of the awdk agent daemon (``adk.server``, :9001).

Why it exists: the daemon used to trust ANY loopback peer that sent no forwarding
header. On a machine with more than one account -- or one account running something it
should not trust -- that let any local process drive the agent as its owner:
``/cli/execute`` runs any ``adk`` verb, ``/chat`` drives an agent with file and shell
tools, and ``/identity/handoff`` mints a browser ticket for the owner's account.
"Loopback" names a machine, not a person.

So the daemon proves the PERSON the way the harness daemon (:8362) already does: a
secret only that user can read. On start it mints ``~/.aither/daemon-token`` (0600 in a
0700 directory); a client that can read the file is running as the owner. Clients send
it in ``X-Aither-Local-Token`` -- a header of its own, so it is never mistaken for an
account bearer and is only ever sent to a loopback daemon (``headers_for``) whose
listening socket, where the kernel can say, belongs to the same uid -- a port another
account squatted never receives it. A browser page cannot read the file, and the CORS
allowlist does not admit the header.

``AITHER_LOCAL_AUTH``:
  ``required``  every request except the static shell pages and ``/health`` needs the
                token (or the remote ``AITHER_SERVER_API_KEY`` bearer), loopback or not
  ``off``       the previous behaviour: loopback without forwarding headers is trusted
  unset         ``required`` on an offline box (``AITHER_OFFLINE``), ``off`` otherwise --
                online, the hosted surfaces reach the daemon by first-party Origin, and
                changing that is its own decision.

``/mesh/join`` requires the token in EVERY mode; the browser-handoff ticket requires it
offline or when ``required`` -- online it keeps the loopback + first-party Origin + CSRF
gate the hosted sign-in page depends on (``server.py``). WebSocket routes get the same
Origin and token rule before the handshake is accepted.
"""
from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import socket
import stat
import urllib.parse
from pathlib import Path
from typing import Dict, List, Mapping, Optional

HEADER = "X-Aither-Local-Token"
_TRUE = ("1", "true", "yes", "on")


def token_path(home: Optional[Path] = None) -> Path:
    explicit = os.environ.get("AITHER_LOCAL_TOKEN_FILE", "").strip()
    if explicit:
        return Path(explicit)
    return (home or Path.home()) / ".aither" / "daemon-token"


def is_offline(env: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return (env.get("AITHER_OFFLINE") or "").strip().lower() in _TRUE


def mode(env: Optional[Mapping[str, str]] = None) -> str:
    """``required`` or ``off`` (see the module docstring)."""
    env = os.environ if env is None else env
    raw = (env.get("AITHER_LOCAL_AUTH") or "").strip().lower()
    if raw in ("required", "require", "on", "1", "true", "yes"):
        return "required"
    if raw in ("off", "0", "false", "no"):
        return "off"
    return "required" if is_offline(env) else "off"


def read_token(path: Optional[Path] = None) -> Optional[str]:
    """The token, or None when the file is absent, unreadable or empty."""
    path = token_path() if path is None else path
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return value or None


def ensure_token(path: Optional[Path] = None) -> str:
    """Return the token, minting it (0600, directory 0700) when it does not exist yet.

    Created with O_EXCL so two daemons starting at once agree on one value. A file other
    users could read is ROTATED, not merely tightened: whoever read it already has the
    old value, so that value stops being the owner's proof.
    """
    path = token_path() if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path.parent, 0o700)
        try:
            exposed = bool(stat.S_IMODE(path.stat().st_mode) & 0o077)
        except FileNotFoundError:
            exposed = False
        if exposed:
            path.unlink()
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(secrets.token_urlsafe(32) + "\n")
    value = read_token(path)
    if not value:
        raise OSError(f"{path} is empty or unreadable")
    return value


def presented(headers: Mapping[str, str]) -> str:
    """The credential a request presents: the local header, else an Authorization bearer."""
    value = headers.get(HEADER.lower()) or headers.get(HEADER) or ""
    if value:
        return value.strip()
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    return auth[7:].strip() if auth.startswith("Bearer ") else ""


def matches(given: str, expected: Optional[str]) -> bool:
    """Constant-time comparison; an empty side never matches. Compared as UTF-8 bytes,
    so a non-ASCII header is a mismatch (401), never a TypeError (500)."""
    if not given or not expected:
        return False
    return hmac.compare_digest(given.encode("utf-8", "surrogateescape"),
                               str(expected).encode("utf-8", "surrogateescape"))


def is_loopback_url(url: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").strip("[]").lower()
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _decode_addr(hexaddr: str) -> str:
    """A /proc/net/tcp{,6} address as text; an IPv4-mapped IPv6 address as its IPv4."""
    raw = bytes.fromhex(hexaddr)
    if len(raw) == 4:
        return socket.inet_ntop(socket.AF_INET, raw[::-1])
    addr = ipaddress.ip_address(b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4)))
    mapped = getattr(addr, "ipv4_mapped", None)
    return str(mapped) if mapped is not None else str(addr)


def connect_targets(host: str) -> frozenset:
    """Every address a client dialling ``host`` may actually reach. `localhost` is BOTH
    loopbacks: resolvers try ::1 and 127.0.0.1, so a listener on either answers."""
    host = (host or "").strip("[]").lower()
    if host in ("localhost", "localhost.localdomain"):
        return frozenset({"127.0.0.1", "::1"})
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return frozenset()
    mapped = getattr(addr, "ipv4_mapped", None)
    return frozenset({str(mapped) if mapped is not None else str(addr)})


def listener_uids(port: int, targets: frozenset,
                  proc_net: Optional[Path] = None) -> Optional[List[int]]:
    """The uid of EVERY LISTEN socket on ``port`` that could take a connection to one of
    ``targets`` -- one bound to the target itself or to a wildcard (0.0.0.0, ::) -- from
    tcp and tcp6, IPv4-mapped rows included. [] = the kernel's table holds no such
    listener; None = the table could not be read (not Linux)."""
    proc_net = Path(os.environ.get("AITHER_PROC_NET", "/proc/net")) if proc_net is None \
        else proc_net
    v6 = any(":" in t for t in targets)
    v4 = any(":" not in t for t in targets)
    uids: List[int] = []
    seen = False
    for name in ("tcp", "tcp6"):
        try:
            rows = (proc_net / name).read_text(encoding="ascii", errors="replace").splitlines()
        except OSError:
            continue
        seen = True
        for row in rows[1:]:
            cols = row.split()
            if len(cols) < 8 or cols[3] != "0A":
                continue
            try:
                hexaddr, hexport = cols[1].rsplit(":", 1)
                if int(hexport, 16) != port:
                    continue
                addr = _decode_addr(hexaddr)
                uid = int(cols[7])
            except (ValueError, OSError):
                continue
            wildcard = (addr == "0.0.0.0" and v4) or (addr == "::" and (v4 or v6))
            if addr in targets or wildcard:
                uids.append(uid)
    return uids if seen else None


def headers_for(url: str, path: Optional[Path] = None,
                proc_net: Optional[Path] = None, uid: Optional[int] = None) -> Dict[str, str]:
    """The local-token header for a request to ``url`` -- ONLY to loopback, and only when
    EVERY listener the client could reach on that port is THIS user's.

    Fails closed where the kernel can say: a listener of any other uid on any address the
    host resolves to (``localhost`` is 127.0.0.1 AND ::1) withholds the token, and so
    does no listener at all (the owner's daemon is down; whoever binds next is unknown).
    Where the table cannot be read at all (not Linux) the loopback rule alone applies.
    """
    if not is_loopback_url(url):
        return {}
    if uid is None:
        getuid = getattr(os, "getuid", None)
        uid = getuid() if getuid is not None else None
    if uid is not None:
        try:
            parts = urllib.parse.urlsplit(url)
            port = parts.port or (443 if parts.scheme == "https" else 80)
            targets = connect_targets(parts.hostname or "")
        except ValueError:
            return {}
        if not targets:
            return {}
        uids = listener_uids(port, targets, proc_net)
        if uids is not None and (not uids or any(u != uid for u in uids)):
            return {}
    token = read_token(path)
    return {HEADER: token} if token else {}
