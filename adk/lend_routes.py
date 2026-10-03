"""Lend context: the daemon's operator API over the local KV-holder relay.

`adk kvholder phone` runs a relay on this machine; phones, laptops, containers and CI runners
dial into it and hold the old part of a model's KV cache. These routes let a first-party page
(the Living OS, Forge, the desk) drive that relay without a terminal:

    GET  /kvholder/status   is a relay running here, how it is reached, who is attached
    POST /kvholder/relay    {"action": "start"|"stop", "via": "local"|"lan"|"tunnel"|"usb"}
    POST /kvholder/join     {"ttl_s", "max_mb", "store"} -> one join token + the ways to use it

Every route opens with the daemon's browser guard (loopback peer + first-party Origin + the
AITHER_BROWSER_HANDOFF kill switch), so only the person at this machine, on a page we serve,
can reach it. The relay's MASTER token never leaves this process: the page gets join tokens,
which work once and expire. A join token is a secret: it is returned to the caller and never
logged here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger("adk.lend")

VIAS = ("local", "lan", "tunnel", "usb")
STORES = ("f32", "wire", "tq4")
HOLDER_IMAGE = "python:3.11-slim"
# What a container holder needs above the bytes it lends: the interpreter, numpy, the socket.
CONTAINER_OVERHEAD_MB = 384

# The relay this daemon started (None when the relay was started from a terminal).
_managed: dict[str, Any] = {"proc": None, "via": ""}


def _state_path() -> Path:
    # Same file and override as adk.kvholder_net.state_path, read without importing the relay.
    return Path(
        os.environ.get("AITHER_KVHOLDER_STATE")
        or Path.home() / ".aither" / "kvholder" / "relay.json"
    )


def read_state() -> dict | None:
    try:
        st = json.loads(_state_path().read_text(encoding="utf-8"))
        return st if isinstance(st, dict) and st.get("web_port") else None
    except (OSError, ValueError):
        return None


def _relay_get(port: int, path: str, token: str = "", timeout: float = 4.0) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{int(port)}{path}")
    if token:
        req.add_header("X-KV-Token", token)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # loopback plain http: the relay
        return json.loads(r.read())


def _via_of(st: dict) -> str:
    if _managed.get("proc") is not None and _managed["proc"].poll() is None:
        return _managed["via"]
    if st.get("public"):
        return "tunnel"
    if st.get("lan"):
        return "lan"
    return "local"


def page_base(st: dict, via: str) -> str:
    """Where a phone opens the holder page (no token)."""
    if via == "tunnel" and st.get("public"):
        return str(st["public"]).rstrip("/")
    if via == "lan" and st.get("lan"):
        return str(st["lan"]).rstrip("/")
    if via == "usb":
        return f"http://localhost:{int(st['web_port'])}"  # the phone's localhost, adb-reversed
    return f"http://127.0.0.1:{int(st['web_port'])}"


def relay_ws(st: dict, via: str) -> str:
    """The URL a Python or container holder dials."""
    base = page_base(st, "local" if via == "usb" else via)
    u = urllib.parse.urlsplit(base)
    return f"{'wss' if u.scheme == 'https' else 'ws'}://{u.netloc}/holder"


def holder_plan(relay: str, join: str, max_mb: int, store: str = "f32") -> dict:
    """Every way to put one holder on the relay with one join token.

    `container` is the contract a sandbox or a deployer follows: the image, the env, the
    memory cap (lent bytes plus the interpreter) and the ONLY host it needs to reach.
    """
    serve = f'adk kvholder serve --connect "$RELAY" --token "$JOIN" --max-mb {int(max_mb)}'
    if store != "f32":
        serve += f" --store {store}"
    egress = urllib.parse.urlsplit(relay.replace("wss://", "https://").replace("ws://", "http://"))
    return {
        "command": serve.replace('"$RELAY"', relay).replace('"$JOIN"', join),
        "container": {
            "image": HOLDER_IMAGE,
            "env": {"RELAY": relay, "JOIN": join, "MAX_MB": str(int(max_mb))},
            "memory_mb": int(max_mb) + CONTAINER_OVERHEAD_MB,
            "egress": [
                f"{egress.hostname}:{egress.port or (443 if egress.scheme == 'https' else 80)}"
            ],
            "cmd": ["sh", "-c", f"pip install -q awdk numpy && exec {serve}"],
        },
    }


def qr_data_uri(url: str) -> str:
    """The phone link as an SVG QR data URI for the page to show; '' when qrcode is absent."""
    try:
        import base64

        import qrcode
        import qrcode.image.svg

        svg = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage).to_string()
        return "data:image/svg+xml;base64," + base64.b64encode(svg).decode()
    except Exception:  # noqa: BLE001 - a QR is a convenience; the link is the essential part
        return ""


def status() -> dict:
    """The relay as the page sees it: never the master token."""
    st = read_state()
    proc = _managed.get("proc")
    managed = proc is not None and proc.poll() is None
    if not st:
        return {"running": False, "managed": managed}
    try:
        rs = _relay_get(st["web_port"], "/status")
    except OSError:
        return {"running": False, "managed": managed, "stale_state": True}
    via = _via_of(st)
    return {
        "running": True,
        "managed": managed,
        "via": via,
        "page": page_base(st, via),
        "relay": relay_ws(st, via),
        "engine_port": st.get("engine_port"),
        "attached": bool(rs.get("attached")),
        "holders": rs.get("holders") or [],
        "held": rs.get("held", 0),
        "calls": rs.get("calls", 0),
        "broken": rs.get("broken"),
    }


def _popen_kwargs() -> dict:
    kw: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL}
    if os.name == "nt":
        # A console child of a windowless daemon opens a terminal tab per spawn.
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kw["start_new_session"] = True
    return kw


def start(via: str, web_port: int = 0, port: int = 0) -> dict:
    if via not in VIAS:
        raise HTTPException(status_code=400, detail=f"via must be one of {', '.join(VIAS)}")
    if status().get("running"):
        raise HTTPException(status_code=409, detail="a relay is already running on this machine")
    log = _state_path().with_name("relay.err.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    # stdout carries the phone URL with the master token in it: it goes nowhere.
    with open(log, "w", encoding="utf-8") as err:
        proc = subprocess.Popen(
            [sys.executable, "-m", "adk", "kvholder", "phone", "--via", via]
            + (["--web-port", str(int(web_port))] if web_port else [])
            + (["--port", str(int(port))] if port else []),
            stderr=err,
            **_popen_kwargs(),
        )
    _managed.update(proc=proc, via=via)
    deadline = time.time() + (120.0 if via == "tunnel" else 25.0)
    while time.time() < deadline:
        if proc.poll() is not None:
            _managed.update(proc=None, via="")
            tail = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-3:]
            raise HTTPException(status_code=502, detail="relay exited: " + " | ".join(tail))
        st = read_state()
        if st and st.get("pid") == proc.pid:
            s = status()
            if s.get("running"):
                return s
        time.sleep(0.25)
    raise HTTPException(status_code=504, detail="relay did not come up in time; see relay.err.log")


def stop() -> dict:
    proc = _managed.get("proc")
    if proc is None or proc.poll() is not None:
        if status().get("running"):
            raise HTTPException(
                status_code=409, detail="this relay was started from a terminal; stop it there"
            )
        return {"running": False, "managed": False}
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    _managed.update(proc=None, via="")
    try:
        _state_path().unlink(missing_ok=True)
    except OSError as e:
        logger.warning("kvholder: could not remove the relay state file: %s", e)
    return {"running": False, "managed": False}


def join(ttl_s: float, max_mb: int, store: str) -> dict:
    if store not in STORES:
        raise HTTPException(status_code=400, detail=f"store must be one of {', '.join(STORES)}")
    st = read_state()
    if not st or not status().get("running"):
        raise HTTPException(status_code=409, detail="no relay running here; start one first")
    ttl = min(max(float(ttl_s), 30.0), 86400.0)
    max_mb = min(max(int(max_mb), 64), 1 << 20)
    try:
        minted = _relay_get(st["web_port"], f"/join?ttl={int(ttl)}", token=str(st["token"]))
    except OSError as e:
        raise HTTPException(status_code=502, detail=f"the relay did not mint a token ({e})")
    tok = str(minted["token"])
    via = _via_of(st)
    page = page_base(st, via) + f"/#t={tok}" + ("&store=tq4" if store == "tq4" else "")
    out = {"expires": minted.get("expires"), "token": tok, "page": page, "qr": qr_data_uri(page)}
    out.update(holder_plan(relay_ws(st, via), tok, max_mb, store))
    if via == "lan":
        out["note"] = "plain http on a LAN IP: the phone page runs the CPU holder"
    return out


def create_lend_router(guard: Callable[[Request], str]) -> APIRouter:
    router = APIRouter(prefix="/kvholder", tags=["kvholder"])

    @router.get("/status")
    async def _status(request: Request) -> dict:
        guard(request)
        return await asyncio.to_thread(status)

    @router.post("/relay")
    async def _relay(request: Request) -> dict:
        guard(request)
        body = await _json(request)
        action = str(body.get("action") or "")
        if action == "start":
            try:
                ports = int(body.get("web_port") or 0), int(body.get("port") or 0)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="ports must be numbers")
            return await asyncio.to_thread(start, str(body.get("via") or "local"), *ports)
        if action == "stop":
            return await asyncio.to_thread(stop)
        raise HTTPException(status_code=400, detail="action must be start or stop")

    @router.post("/join")
    async def _join(request: Request) -> dict:
        guard(request)
        body = await _json(request)
        try:
            ttl = float(body.get("ttl_s", 900))
            max_mb = int(body.get("max_mb", 4096))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="ttl_s and max_mb must be numbers")
        out = await asyncio.to_thread(join, ttl, max_mb, str(body.get("store") or "f32"))
        logger.info("kvholder: minted a join token (ttl %ss, %s MB)", int(ttl), max_mb)
        return out

    # ── The workspace swarm (adk kvholder workspace serve, kvholder_workspace.py) ──
    # Read and toggled through THAT module's own status file and Grants store, the
    # same ones awsh and awdesk use -- this is a door to it, not a second copy.
    # Behind the same guard, so with AITHER_LOCAL_AUTH=required only a page that
    # paired as the owner (adk/browser_grant.py) reaches it.
    @router.get("/workspace")
    async def _workspace(request: Request) -> dict:
        guard(request)
        from adk import kvholder_workspace

        st = await asyncio.to_thread(kvholder_workspace.read_status)
        if st is None:
            return {"running": False}
        return {"running": True, **st}

    @router.post("/workspace/grant")
    async def _workspace_grant(request: Request) -> dict:
        guard(request)
        from adk import kvholder_workspace

        body = await _json(request)
        did = str(body.get("device_id") or "").strip()
        if not kvholder_workspace._DEVICE_ID.match(did):
            raise HTTPException(status_code=400, detail="device_id is required")
        lend = body.get("lend") is True
        await asyncio.to_thread(kvholder_workspace.Grants().set, did, lend)
        logger.info("kvholder workspace: %s lend=%s (from the owner's page)", did, lend)
        return {"device_id": did, "lend": lend,
                "note": "applied at the relay's next sweep (5 s)"}

    return router


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
