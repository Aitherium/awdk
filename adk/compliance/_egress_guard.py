"""Process-wide egress guard for the awdk air-gap enforcer (implementation).

The public module is ``adk.compliance.egress_guard``; it re-exports this one.
The split exists so ``python -m adk.compliance.egress_guard`` does not run a
SECOND copy of the guard state as ``__main__`` (``adk/__init__`` imports the
guard before runpy executes the CLI module).

awdk has ~800 outbound call sites across httpx, urllib, requests, http.client
and websockets. Wiring the enforcer into each one is not maintainable, so this
module patches the few choke points every one of them goes through:

  - ``httpx.Client.send`` / ``httpx.AsyncClient.send`` (module-level
    ``httpx.get`` etc. build a Client, so they are covered too)
  - ``urllib.request.OpenerDirector.open`` (``urlopen`` and redirects)
  - ``requests.Session.send`` (every requests call and every redirect hop)
  - backstop: ``socket.create_connection`` and ``socket.socket.connect`` /
    ``connect_ex`` for AF_INET/AF_INET6 (http.client, websockets, raw sockets,
    asyncio selector loops). AF_UNIX is never touched.
  - connectionless UDP: ``socket.socket.sendto`` / ``sendmsg`` with an address.
  - resolver: ``socket.getaddrinfo`` / ``gethostbyname`` / ``gethostbyname_ex``
    / ``gethostbyaddr``. With ``resolve_hostnames`` off (the loopback-only
    default) a non-local name is refused before a DNS query is sent.

Each wrapper asks ``AirGapEnforcer.enforce_destination`` BEFORE calling the
original, so a blocked request never sends a byte. When enforcement is off the
wrappers cost one attribute check and call straight through.

Known limits (defense in depth, not the boundary): an event loop that dials
outside ``socket.socket.connect`` bypasses the socket backstop. Two exist:
Windows' ProactorEventLoop (``_overlapped.ConnectEx``) and uvloop (libuv's
own connect; ``uvicorn[standard]`` pulls uvloop in and ``loop="auto"`` picks
it on Linux). For uvloop the guard closes the gap in-process: installing it
drops a uvloop event-loop policy back to the stdlib default, and
``uvicorn_loop()`` tells ``adk.server`` to run uvicorn with ``loop="asyncio"``
whenever enforcement is on. A caller that builds a uvloop loop by hand is
still outside it. ``os.sendfile``/raw C extensions that open their own sockets
(see AAE001) and child processes (curl, CLIs) are outside any in-process
guard. The network layer (the unit's IPAddressDeny / awwall / a host
firewall) is the real boundary.

CLI::

    python -m adk.compliance.egress_guard --status [--json]
    python -m adk.compliance.egress_guard --probe URL   # 1 blocked, 0 allowed, 2 error
    python -m adk.compliance.egress_guard --probe       # 0 sealed, 1 egress possible, 2 error
    python -m adk.compliance.egress_guard --self-test

``--probe URL`` answers "would THIS request be refused?" (1 = refused, the
G10 evidence command). Bare ``--probe`` answers "is this process sealed?" (the
awdk-on-awnix health contract): it dials TEST-NET-3 203.0.113.1:443 through the
guarded socket layer and exits 0 only when the guard refuses the dial.
"""

from __future__ import annotations

import argparse
import errno
import ipaddress
import json
import logging
import os
import socket
import sys
import threading
from typing import Any, Callable, Dict, Optional

from adk.compliance import air_gap as _air_gap
from adk.compliance.air_gap import AirGapViolation

logger = logging.getLogger("adk.compliance.egress_guard")

_LOCK = threading.RLock()
_ORIGINALS: Dict[str, Any] = {}
_INSTALLED: Dict[str, bool] = {"httpx": False, "urllib": False, "requests": False,
                               "socket": False, "dns": False}
_INET_FAMILIES = (socket.AF_INET, getattr(socket, "AF_INET6", socket.AF_INET))


class EgressBlocked(AirGapViolation, ConnectionRefusedError):
    """Raised by the socket backstop: an AirGapViolation that is also an OSError.

    Socket callers expect ``OSError`` from ``connect``; raising one keeps their
    error handling intact while ``except AirGapViolation`` still sees it.
    """

    def __new__(cls, subsystem: str = "network_egress", detail: str = ""):
        msg = f"Air-gap violation: {subsystem}" + (f" -- {detail}" if detail else "")
        return ConnectionRefusedError.__new__(cls, errno.ECONNREFUSED, msg)

    def __init__(self, subsystem: str = "network_egress", detail: str = ""):
        self.subsystem = subsystem
        self.detail = detail


def _active() -> Optional["_air_gap.AirGapEnforcer"]:
    """The enforcer when it is enforcing, else None. The hot-path check."""
    enf = _air_gap._enforcer
    if enf is None or not enf._enabled:
        return None
    return enf


# ---- wrappers ------------------------------------------------------------


def _url_of(obj: Any) -> str:
    url = getattr(obj, "full_url", None) or getattr(obj, "url", None) or obj
    return str(url)


#: httpx transports that never open a socket: they hand the request to an
#: in-process ASGI/WSGI app. (MockTransport is deliberately NOT here: the guard's
#: own tests use it as the stand-in for "a byte reached a transport".) Judging
#: these by URL host blocked
#: FastAPI's TestClient ("testserver") under strict mode although no byte leaves
#: the process. EXACT classes only (never a subclass or "anything that is not
#: HTTPTransport"): a custom transport may itself dial the network.
_IN_PROCESS_TRANSPORTS = frozenset({
    ("httpx", "ASGITransport"),
    ("httpx", "WSGITransport"),
    ("httpx._transports.asgi", "ASGITransport"),
    ("httpx._transports.wsgi", "WSGITransport"),
    ("starlette.testclient", "_TestClientTransport"),
})


def _in_process_transport(client, request) -> bool:
    """True only when the transport httpx will use for this request is one of the
    exact in-process classes above. Any lookup failure answers False (judge it)."""
    try:
        pick = getattr(client, "_transport_for_url", None)
        transport = pick(request.url) if callable(pick) else getattr(client, "_transport", None)
    except Exception:  # noqa: BLE001 - fail closed: judge the request
        return False
    if transport is None:
        return False
    cls = type(transport)
    return (cls.__module__, cls.__qualname__) in _IN_PROCESS_TRANSPORTS


def _install_httpx() -> bool:
    try:
        import httpx
    except ImportError:
        return False
    if _INSTALLED["httpx"]:
        return True
    orig_sync = httpx.Client.send
    orig_async = httpx.AsyncClient.send

    def send(self, request, *args, **kwargs):
        enf = _active()
        if enf is not None and not _in_process_transport(self, request):
            enf.enforce_destination(str(request.url))
        return orig_sync(self, request, *args, **kwargs)

    async def asend(self, request, *args, **kwargs):
        enf = _active()
        if enf is not None and not _in_process_transport(self, request):
            enf.enforce_destination(str(request.url))
        return await orig_async(self, request, *args, **kwargs)

    send.__wrapped__ = orig_sync  # type: ignore[attr-defined]
    asend.__wrapped__ = orig_async  # type: ignore[attr-defined]
    _ORIGINALS["httpx.Client.send"] = orig_sync
    _ORIGINALS["httpx.AsyncClient.send"] = orig_async
    httpx.Client.send = send  # type: ignore[method-assign]
    httpx.AsyncClient.send = asend  # type: ignore[method-assign]
    _INSTALLED["httpx"] = True
    return True


def _install_urllib() -> bool:
    import urllib.request

    if _INSTALLED["urllib"]:
        return True
    orig = urllib.request.OpenerDirector.open

    def open_(self, fullurl, *args, **kwargs):
        enf = _active()
        if enf is not None:
            url = _url_of(fullurl)
            if "://" in url and not url.lower().startswith(("file:", "data:")):
                enf.enforce_destination(url)
        return orig(self, fullurl, *args, **kwargs)

    open_.__wrapped__ = orig  # type: ignore[attr-defined]
    _ORIGINALS["urllib.OpenerDirector.open"] = orig
    urllib.request.OpenerDirector.open = open_  # type: ignore[method-assign]
    _INSTALLED["urllib"] = True
    return True


def _install_requests() -> bool:
    try:
        import requests
    except ImportError:
        return False
    if _INSTALLED["requests"]:
        return True
    orig = requests.Session.send

    def send(self, request, *args, **kwargs):
        enf = _active()
        if enf is not None:
            enf.enforce_destination(_url_of(request))
        return orig(self, request, *args, **kwargs)

    send.__wrapped__ = orig  # type: ignore[attr-defined]
    _ORIGINALS["requests.Session.send"] = orig
    requests.Session.send = send  # type: ignore[method-assign]
    _INSTALLED["requests"] = True
    return True


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]").split("%")[0])
    except ValueError:
        return False
    return True


def _check_sockaddr(enf: "_air_gap.AirGapEnforcer", address: Any, what: str) -> None:
    """Judge an (host, port[, ...]) sockaddr. Raises EgressBlocked in strict."""
    try:
        host = str(address[0])
    except (TypeError, IndexError, KeyError):
        return
    if _is_ip_literal(host):
        allowed = enf.check_destination_allowed(host)
    else:
        allowed = enf.is_host_allowed(host)  # connect() given a hostname resolves it
    if allowed:
        return
    detail = f"{what} to {host}:{address[1] if len(address) > 1 else '?'}"
    try:
        enf.enforce_destination(host, detail=detail)
    except AirGapViolation as exc:
        raise EgressBlocked("network_egress", exc.detail) from None


def _install_socket() -> bool:
    if _INSTALLED["socket"]:
        return True
    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex
    orig_create = socket.create_connection

    def connect(self, address):
        enf = _active()
        if enf is not None and self.family in _INET_FAMILIES:
            _check_sockaddr(enf, address, "socket connect")
        return orig_connect(self, address)

    def connect_ex(self, address):
        enf = _active()
        if enf is not None and self.family in _INET_FAMILIES:
            try:
                _check_sockaddr(enf, address, "socket connect_ex")
            except EgressBlocked:
                return errno.ECONNREFUSED
        return orig_connect_ex(self, address)

    def create_connection(address, *args, **kwargs):
        enf = _active()
        # An IP literal is judged here; a hostname's lookup is judged by the
        # getaddrinfo wrapper and every resolved IP by socket.connect().
        if enf is not None and address and _is_ip_literal(str(address[0])):
            _check_sockaddr(enf, address, "create_connection")
        return orig_create(address, *args, **kwargs)

    orig_sendto = socket.socket.sendto
    orig_sendmsg = getattr(socket.socket, "sendmsg", None)

    def sendto(self, data, *args):
        # sendto(data, address) / sendto(data, flags, address)
        enf = _active()
        if enf is not None and self.family in _INET_FAMILIES and args:
            _check_sockaddr(enf, args[-1], "socket sendto")
        return orig_sendto(self, data, *args)

    def sendmsg(self, buffers, *args):
        # sendmsg(buffers[, ancdata[, flags[, address]]])
        enf = _active()
        if enf is not None and self.family in _INET_FAMILIES and len(args) >= 3 \
                and args[2] is not None:
            _check_sockaddr(enf, args[2], "socket sendmsg")
        return orig_sendmsg(self, buffers, *args)  # type: ignore[misc]

    _ORIGINALS["socket.socket.connect"] = orig_connect
    _ORIGINALS["socket.socket.connect_ex"] = orig_connect_ex
    _ORIGINALS["socket.create_connection"] = orig_create
    _ORIGINALS["socket.socket.sendto"] = orig_sendto
    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.create_connection = create_connection  # type: ignore[assignment]
    socket.socket.sendto = sendto  # type: ignore[method-assign,assignment]
    if orig_sendmsg is not None:
        _ORIGINALS["socket.socket.sendmsg"] = orig_sendmsg
        socket.socket.sendmsg = sendmsg  # type: ignore[method-assign,assignment]
    _INSTALLED["socket"] = True
    return True


def _judge_lookup(enf: "_air_gap.AirGapEnforcer", name: Any, what: str,
                  reverse: bool = False) -> None:
    """Refuse a resolver call the policy does not permit, before it runs."""
    if name is None:
        return
    host = name.decode("ascii", "replace") if isinstance(name, (bytes, bytearray)) \
        else str(name)
    if not host:
        return
    if reverse and _is_ip_literal(host):
        ip = ipaddress.ip_address(host.strip("[]").split("%")[0])
        permitted = ip.is_loopback or enf.resolves_hostnames  # a PTR query is DNS
    else:
        permitted = enf.dns_permitted(host)
    if permitted:
        return
    detail = f"{what} of {host} (refused before DNS)"
    enf._record_violation("network_egress", detail)
    if enf._mode == _air_gap.EnforcementMode.STRICT:
        raise EgressBlocked("network_egress", detail)


def _install_dns() -> bool:
    if _INSTALLED["dns"]:
        return True
    orig_gai = socket.getaddrinfo
    orig_ghbn = socket.gethostbyname
    orig_ghbn_ex = socket.gethostbyname_ex
    orig_ghba = socket.gethostbyaddr

    def getaddrinfo(host, *args, **kwargs):
        enf = _active()
        if enf is not None:
            _judge_lookup(enf, host, "getaddrinfo")
        return orig_gai(host, *args, **kwargs)

    def gethostbyname(host):
        enf = _active()
        if enf is not None:
            _judge_lookup(enf, host, "gethostbyname")
        return orig_ghbn(host)

    def gethostbyname_ex(host):
        enf = _active()
        if enf is not None:
            _judge_lookup(enf, host, "gethostbyname_ex")
        return orig_ghbn_ex(host)

    def gethostbyaddr(host):
        enf = _active()
        if enf is not None:
            _judge_lookup(enf, host, "gethostbyaddr", reverse=True)
        return orig_ghba(host)

    _ORIGINALS["socket.getaddrinfo"] = orig_gai
    _ORIGINALS["socket.gethostbyname"] = orig_ghbn
    _ORIGINALS["socket.gethostbyname_ex"] = orig_ghbn_ex
    _ORIGINALS["socket.gethostbyaddr"] = orig_ghba
    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
    socket.gethostbyname = gethostbyname  # type: ignore[assignment]
    socket.gethostbyname_ex = gethostbyname_ex  # type: ignore[assignment]
    socket.gethostbyaddr = gethostbyaddr  # type: ignore[assignment]
    _INSTALLED["dns"] = True
    return True


# ---- public API ----------------------------------------------------------

_INSTALLERS: Dict[str, Callable[[], bool]] = {
    "httpx": _install_httpx,
    "urllib": _install_urllib,
    "requests": _install_requests,
    "socket": _install_socket,
    "dns": _install_dns,
}


def _drop_uvloop_policy() -> bool:
    """A uvloop policy dials through libuv, past the socket backstop: reset it.

    Returns True when a uvloop policy was replaced by the stdlib default.
    """
    import asyncio

    try:
        pol = asyncio.get_event_loop_policy()
    except Exception:  # noqa: BLE001 - nothing to judge
        return False
    if type(pol).__module__.split(".")[0] != "uvloop":
        return False
    asyncio.set_event_loop_policy(None)
    logger.warning("[egress_guard] uvloop event-loop policy replaced by the stdlib "
                   "default: uvloop connects bypass the socket backstop")
    return True


def uvicorn_loop() -> str:
    """The uvicorn ``loop=`` setting that keeps asyncio connects guarded.

    ``"asyncio"`` while enforcement is on or the socket backstop is installed
    (uvloop would dial past it); ``"auto"`` otherwise, so online behaviour and
    performance are unchanged.
    """
    enf = _air_gap._enforcer
    if _INSTALLED["socket"] or (enf is not None and enf._enabled):
        return "asyncio"
    return "auto"


def install_egress_guard() -> Dict[str, bool]:
    """Patch every choke point. Idempotent. Returns {lib: installed}."""
    with _LOCK:
        for name, fn in _INSTALLERS.items():
            try:
                fn()
            except Exception as e:  # one library failing must not stop the others
                logger.warning("[egress_guard] %s patch failed: %s", name, e)
        try:
            _drop_uvloop_policy()
        except Exception as e:  # noqa: BLE001
            logger.warning("[egress_guard] uvloop policy reset failed: %s", e)
        return dict(_INSTALLED)


def uninstall_egress_guard() -> Dict[str, bool]:
    """Restore every patched callable (tests / self-test)."""
    with _LOCK:
        if _INSTALLED["httpx"]:
            import httpx
            httpx.Client.send = _ORIGINALS.pop("httpx.Client.send")  # type: ignore[method-assign]
            httpx.AsyncClient.send = _ORIGINALS.pop(  # type: ignore[method-assign]
                "httpx.AsyncClient.send")
            _INSTALLED["httpx"] = False
        if _INSTALLED["urllib"]:
            import urllib.request
            urllib.request.OpenerDirector.open = _ORIGINALS.pop(  # type: ignore[method-assign]
                "urllib.OpenerDirector.open")
            _INSTALLED["urllib"] = False
        if _INSTALLED["requests"]:
            import requests
            requests.Session.send = _ORIGINALS.pop(  # type: ignore[method-assign]
                "requests.Session.send")
            _INSTALLED["requests"] = False
        if _INSTALLED["socket"]:
            socket.socket.connect = _ORIGINALS.pop(  # type: ignore[method-assign]
                "socket.socket.connect")
            socket.socket.connect_ex = _ORIGINALS.pop(  # type: ignore[method-assign]
                "socket.socket.connect_ex")
            socket.create_connection = _ORIGINALS.pop(  # type: ignore[assignment]
                "socket.create_connection")
            socket.socket.sendto = _ORIGINALS.pop(  # type: ignore[method-assign]
                "socket.socket.sendto")
            if "socket.socket.sendmsg" in _ORIGINALS:
                socket.socket.sendmsg = _ORIGINALS.pop(  # type: ignore[method-assign]
                    "socket.socket.sendmsg")
            _INSTALLED["socket"] = False
        if _INSTALLED["dns"]:
            for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex",
                         "gethostbyaddr"):
                setattr(socket, name, _ORIGINALS.pop("socket." + name))
            _INSTALLED["dns"] = False
        return dict(_INSTALLED)


def _config_present() -> bool:
    """Could enforcement be on? (env override set, or any config file exists)."""
    env = os.environ.get("AITHER_AIR_GAP")
    if env is not None and env.strip():
        return True
    path = _air_gap.AirGapEnforcer._default_config_path()
    return bool(path is not None and path.exists())


def autoinstall() -> bool:
    """Install the guard only if enforcement is on. Never raises.

    Called from ``adk/__init__``. With no air-gap config and no
    ``AITHER_AIR_GAP`` nothing is patched, so non-air-gap users pay nothing.
    """
    try:
        if _air_gap._enforcer is None and not _config_present():
            return False  # no env, no config file: never build the singleton at import
        enf = _air_gap.get_air_gap_enforcer()
        if not enf.is_enforced():
            return False
        install_egress_guard()
        return True
    except Exception as e:
        logger.debug("[egress_guard] autoinstall skipped: %s", e)
        return False


def egress_guard_status() -> Dict[str, Any]:
    enf = _air_gap._enforcer
    return {
        "installed": dict(_INSTALLED),
        "enforced": bool(enf is not None and enf._enabled),
        "mode": enf.get_mode() if enf is not None else "disabled",
        "allowed_subnets": enf.allowed_subnets if enf is not None else [],
        "violations": len(enf._violations) if enf is not None else 0,
        "config_path": str(enf.config_path) if enf is not None else None,
        "system_floor": bool(enf is not None and enf.system_floor_enforced),
        "config_error": enf.config_error if enf is not None else None,
        "resolve_hostnames": bool(enf is not None and enf._enabled
                                  and enf.resolves_hostnames),
    }


# Aliases for the awdk-on-awnix contract.
install = install_egress_guard
uninstall = uninstall_egress_guard
status = egress_guard_status


def install_if_enforced(enforcer: Optional["_air_gap.AirGapEnforcer"] = None) -> bool:
    if enforcer is not None and _air_gap._enforcer is None:
        _air_gap.set_enforcer(enforcer)
    return autoinstall()


# ---- CLI -----------------------------------------------------------------


def _self_test() -> int:
    """Hermetic: strict + loopback-only enforcer, a local listener, a TEST-NET dial."""
    import tempfile

    failures = []
    saved_env = {k: os.environ.get(k) for k in (
        "AITHER_DATA_DIR", "AITHER_CLOUD_MODE", "AITHER_LLM_OFFLINE_MODE",
        "AITHER_PHONEHOME_DISABLED", "AITHER_AIR_GAP", "AITHER_AIR_GAP_CONFIG")}
    was_installed = any(_INSTALLED.values())
    tmp = tempfile.mkdtemp(prefix="egress-guard-selftest-")
    os.environ["AITHER_DATA_DIR"] = tmp
    os.environ.pop("AITHER_AIR_GAP", None)
    os.environ["AITHER_AIR_GAP_CONFIG"] = os.path.join(tmp, "absent.yaml")
    enf = _air_gap.AirGapEnforcer(enabled=True, mode="strict", system_floor=False)
    prev = _air_gap.set_enforcer(enf)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        install_egress_guard()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        # 1. loopback allowed
        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            c.settimeout(2)
            c.connect(("127.0.0.1", port))
        except OSError as e:
            failures.append(f"loopback connect refused: {e}")
        finally:
            c.close()
        # 2. TEST-NET-3 blocked before any SYN
        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            c.settimeout(2)
            c.connect(("203.0.113.1", 443))
            failures.append("203.0.113.1:443 was NOT blocked")
        except EgressBlocked:
            pass
        except OSError as e:
            failures.append(f"203.0.113.1:443 failed with a network error, not the guard: {e}")
        finally:
            c.close()
        # 3. URL decision, refused before DNS (loopback-only => no resolution)
        try:
            enf.enforce_destination("https://api.anthropic.com/v1/messages")
            failures.append("api.anthropic.com was NOT blocked")
        except AirGapViolation as e:
            if "before DNS" not in e.detail:
                failures.append(f"api.anthropic.com refusal did not skip DNS: {e.detail}")
        # 3b. UDP sendto and a resolver call are refused too
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            u.sendto(b"x", ("203.0.113.1", 53))
            failures.append("UDP sendto 203.0.113.1:53 was NOT blocked")
        except EgressBlocked:
            pass
        finally:
            u.close()
        try:
            socket.getaddrinfo(SEAL_PROBE_NAME, 443)
            failures.append(f"getaddrinfo({SEAL_PROBE_NAME}) was NOT blocked")
        except EgressBlocked:
            pass
        except OSError as e:
            failures.append(f"getaddrinfo reached the resolver: {e}")
        # 4. the violations reached the audit log
        audit = os.path.join(tmp, "compliance", "audit.jsonl")
        rows = []
        if os.path.exists(audit):
            with open(audit, encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh if line.strip()]
        egress_rows = [r for r in rows if r["event"]["action"] == "air_gap_violation"
                       and r["event"]["metadata"].get("subsystem") == "network_egress"]
        if len(egress_rows) < 2:
            failures.append(f"expected >=2 network_egress audit rows, got {len(egress_rows)}")
        # 5. disabled => inert
        enf.disable()
        try:
            enf.enforce_destination("https://api.anthropic.com")
        except AirGapViolation:
            failures.append("disabled enforcer still blocked")
    finally:
        srv.close()
        if not was_installed:
            uninstall_egress_guard()
        _air_gap.set_enforcer(prev)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    for f in failures:
        print(f"SELF-TEST FAIL: {f}")
    print("SELF-TEST " + ("FAIL" if failures else "PASS"))
    return 1 if failures else 0


SEAL_PROBE_ADDR = ("203.0.113.1", 443)  # TEST-NET-3: never routed, never answered
SEAL_PROBE_UDP = ("203.0.113.1", 53)
SEAL_PROBE_NAME = "airgap-seal-probe.example"  # RFC 2606: never a real host


def _probe_one(kind: str, fn: Callable[[], Any]) -> Dict[str, str]:
    try:
        fn()
        return {"check": kind, "verdict": "egress-possible", "detail": "not refused"}
    except EgressBlocked as e:
        return {"check": kind, "verdict": "blocked", "detail": e.detail}
    except OSError as e:  # the guard let it out; the network/resolver answered
        return {"check": kind, "verdict": "egress-possible", "detail": f"guard let it out: {e}"}
    except Exception as e:
        return {"check": kind, "verdict": "error", "detail": str(e)}


def _probe_sealed(enf: "_air_gap.AirGapEnforcer", as_json: bool) -> int:
    """0 = the guard refused every egress path tried; 1 = egress possible; 2 = error.

    Tries a TCP dial and a UDP sendto to TEST-NET-3 and, when hostname
    resolution is off, a DNS lookup of an RFC 2606 name.
    """
    out: Dict[str, Any] = {"target": "%s:%d" % SEAL_PROBE_ADDR, "mode": enf.get_mode(),
                           "installed": dict(_INSTALLED),
                           "system_floor": enf.system_floor_enforced,
                           "config_error": enf.config_error}
    missing = [k for k in ("socket", "dns") if not _INSTALLED[k]]
    if not enf.is_enforced() or missing:
        out["verdict"] = "egress-possible"
        out["detail"] = ("air gap not enforced" if not enf.is_enforced()
                         else "guard absent: " + ",".join(missing))
        rc = 1
    elif enf.get_mode() != "strict":
        out["verdict"], out["detail"], rc = "egress-possible", "audit mode logs but allows", 1
    else:
        def tcp() -> None:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1.0)
            try:
                s.connect(SEAL_PROBE_ADDR)
            finally:
                s.close()

        def udp() -> None:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.sendto(b"probe", SEAL_PROBE_UDP)
            finally:
                s.close()

        checks = [_probe_one("tcp", tcp), _probe_one("udp", udp)]
        if enf.resolves_hostnames:
            checks.append({"check": "dns", "verdict": "permitted",
                           "detail": "resolve_hostnames is on: DNS queries reach the resolver"})
        else:
            checks.append(_probe_one("dns", lambda: socket.getaddrinfo(SEAL_PROBE_NAME, 443)))
        out["checks"] = checks
        verdicts = {c["verdict"] for c in checks}
        if "error" in verdicts:
            out["verdict"], rc = "error", 2
        elif "egress-possible" in verdicts:
            out["verdict"], rc = "egress-possible", 1
        else:
            out["verdict"], rc = "blocked", 0
        out["detail"] = "; ".join(f"{c['check']}={c['verdict']}: {c['detail']}" for c in checks)
    if as_json:
        print(json.dumps(out, sort_keys=True))
    else:
        print(f"{out['verdict']}: {out['target']} (mode={out['mode']}) -- {out['detail']}")
    return rc


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m adk.compliance.egress_guard",
                                 description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true")
    g.add_argument("--probe", metavar="URL", nargs="?", const="")
    g.add_argument("--self-test", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    try:
        enf = _air_gap.get_air_gap_enforcer()
        if enf.is_enforced():
            install_egress_guard()
    except Exception as e:
        print(f"could-not-judge: {e}", file=sys.stderr)
        return 2
    if args.status:
        st = egress_guard_status()
        if args.json:
            print(json.dumps(st, indent=2, sort_keys=True))
        else:
            print(f"enforced={st['enforced']} mode={st['mode']} "
                  f"allowed_subnets={st['allowed_subnets']} "
                  f"installed={','.join(k for k, v in st['installed'].items() if v) or 'none'}")
        return 0
    if args.probe == "":
        return _probe_sealed(enf, args.json)
    url = args.probe
    verdict: Dict[str, Any] = {"url": url, "mode": enf.get_mode()}
    try:
        enf.enforce_destination(url)
        verdict["verdict"] = "allowed"
        rc = 0
    except AirGapViolation as e:
        verdict["verdict"] = "blocked"
        verdict["detail"] = e.detail
        rc = 1
    except Exception as e:
        verdict["verdict"] = "error"
        verdict["detail"] = str(e)
        rc = 2
    if args.json:
        print(json.dumps(verdict, sort_keys=True))
    else:
        print(f"{verdict['verdict']}: {url} (mode={verdict['mode']})")
    return rc
