"""``adk rc`` — remote control: this machine's sessions, reachable from your phone.

ONE COMMAND, AND IT IS THE WHOLE "ADD DEVICE" FLOW

The session plane already exists on every machine that runs the shell: the harness
daemon on 127.0.0.1:8362 serves `/sessions/unified`, `/sessions/{id}/stream`,
`/sessions/{id}/input` and `/decisions/*`. What has never existed is a verb that
REGISTERS a running daemon with the cloud, so the list is visible from anywhere
but the machine it runs on. `adk rc` is that verb:

    adk login (if needed) -> adk enroll --node-class laptop -> hold the link

and while it holds, the device advertises `harness_url=http://127.0.0.1:8362`
over the same reverse link the inference lane uses.

THE TOKEN IS THE POINT
It advertises a PER-NODE, PATH-SCOPED harness token, never the daemon's root
token. The root token can `POST /sessions`, which spawns a coding agent with
filesystem access on this machine — putting it on a public path would make one
stolen header remote code execution on the owner's laptop. The scoped token
reaches `/sessions*`, `/decisions*` and `/desk/fleet/status` and is refused
everywhere else by the daemon itself, before any handler runs.

The token is minted fresh on every `adk rc` and expires. It is never printed, and
it never leaves this process except into the daemon's own hashed registry.
"""

from __future__ import annotations

__all__ = ["cmd_rc", "default_harness_url", "probe_harness"]

import asyncio
import os
from typing import Tuple

#: Where the session daemon listens. Loopback, always: the daemon is reached
#: THROUGH the link by a process on this machine, never by binding a public port.
DEFAULT_HARNESS_PORT = 8362


def default_harness_url() -> str:
    """The local harness base URL this device advertises."""
    explicit = (os.environ.get("AITHER_HARNESS_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    port = (os.environ.get("AITHER_HARNESS_PORT") or "").strip() or str(DEFAULT_HARNESS_PORT)
    return f"http://127.0.0.1:{port}"


def probe_harness(base: str, token: str = "", timeout: float = 3.0) -> Tuple[bool, str]:
    """Is the session daemon answering, and does the scoped token reach it?

    Returns ``(ready, detail)``. A 200 on ``/sessions/unified`` with the SCOPED
    token is the only thing that counts as ready — probing with the root token,
    or probing an unauthenticated route, would prove the daemon is up while the
    thing the phone will actually do still 403s.
    """
    import json
    import urllib.error
    import urllib.request

    url = f"{base.rstrip('/')}/sessions/unified"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = int(getattr(r, "status", 200) or 200)
            body = r.read(4096).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} from {url}"
    except Exception as e:  # noqa: BLE001 -- a dead daemon is a normal outcome here
        return False, f"{type(e).__name__}: {e}"
    if status != 200:
        return False, f"HTTP {status} from {url}"
    try:
        data = json.loads(body)
        n = len(data.get("sessions") or []) if isinstance(data, dict) else 0
        return True, f"{n} session(s)"
    except ValueError:
        return True, "answered (non-JSON body)"


def _signed_in() -> bool:
    """Is there a real identity on this box? (Never the local-root placeholder.)"""
    try:
        from adk.fleet_enroll import _load_auth_config
    except Exception:  # noqa: BLE001
        return False
    auth = _load_auth_config()
    return bool(
        auth.get("access_token") or auth.get("api_key") or auth.get("token")
        or auth.get("tenant_slug") or (auth.get("user") or {}).get("tenant_slug")
    )


def cmd_rc(args) -> int:
    """Enrol this machine and hold the link so its sessions are reachable."""
    node_class = getattr(args, "node_class", None) or "laptop"
    harness_url = (getattr(args, "harness_url", None) or "").strip() or default_harness_url()
    ttl_days = int(getattr(args, "token_ttl_days", None) or 30)
    once = bool(getattr(args, "once", False))

    # 1. IDENTITY. `adk enroll` refuses without one, and it must: an
    #    unauthenticated enrol binds the device to the tenant "personal" and
    #    looks like it worked.
    if not _signed_in():
        import sys

        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        if getattr(args, "api_key", None) or interactive:
            # ONE command means rc signs you in itself: the device flow on a
            # terminal, the key when one was passed. `cmd_login` reads flags the
            # rc parser never defines (`portal_url` crashed the --api-key path
            # with AttributeError), so it gets its own complete namespace.
            import argparse

            from adk.cli import cmd_login  # local import: cli imports this module

            rc = cmd_login(argparse.Namespace(
                portal_url=None, api_key=getattr(args, "api_key", None),
                email=None, password=None, github=False, no_sync=False,
            ))
            if rc != 0:
                return rc
            if not _signed_in():
                print("x Sign-in finished but no identity was saved; run `adk login`.")
                return 1
        else:
            print("x Not signed in.")
            print()
            print("  adk login                    # browser device flow")
            print("  adk login --api-key <key>    # headless / phone / VM")
            print("  then re-run: adk rc")
            return 1

    # 2. THE SCOPED TOKEN, minted fresh every run. Never the daemon's root token.
    harness_token = ""
    try:
        from adk.harnesses.daemon import SCOPED_LINK_PATHS, mint_scoped_token

        harness_token = mint_scoped_token(
            f"node:{os.environ.get('AITHER_NODE_ID') or 'this-device'}",
            ttl_days=ttl_days,
        )
        scope_note = ", ".join(SCOPED_LINK_PATHS)
    except Exception as e:  # noqa: BLE001
        # LOUD. Falling back to the root token here would be the whole point of
        # this command, inverted.
        print(f"x Could not mint a scoped harness token: {e}")
        print("  Refusing to advertise this machine with the daemon's root token.")
        return 1

    ready, detail = probe_harness(harness_url, harness_token)
    print("Remote control")
    print(f"  Harness:  {harness_url}  ({'ready — ' + detail if ready else 'NOT answering — ' + detail})")
    print(f"  Scope:    {scope_note}  (root token is NOT advertised)")
    if not ready:
        # Not fatal. The daemon may still be starting, and `harness_ready` is a
        # field on the heartbeat precisely so this state is visible rather than
        # guessed. Say it plainly instead of failing the command.
        print("  Note:     start the shell (or `adk-serve`) so the session daemon is up;")
        print("            the heartbeat re-reports harness_ready every 60s.")

    # 3. ENROL + HOLD. The link is what makes the sessions reachable; without it
    #    this is just an enrollment.
    #
    # AITHER_FLEET_ENROLL gates the BOOT-time enrollment path, so that a machine
    # does not phone home just because adk is installed. An operator typing
    # `adk rc` has already decided, exactly as `adk enroll` reasons.
    os.environ.setdefault("AITHER_FLEET_ENROLL", "1")
    from adk.fleet_enroll import active_node_link, enroll_on_boot

    async def _run() -> int:
        result = await enroll_on_boot(
            enable_heartbeat=True,
            inference_url=None,
            node_class=node_class,
            start_link=True,
            harness_url=harness_url,
            harness_token=harness_token,
        )
        if not result.get("enrolled"):
            if result.get("http_status"):
                print(f"x Enrollment refused: HTTP {result['http_status']}")
                print(result.get("body") or "(empty body)")
            else:
                print(f"ERROR: {result.get('error', 'enrollment failed')}")
            return 1
        node_id = result.get("node_id", "?")
        print(f"  Device:   {node_id} ({node_class})")
        print("  Portal:   https://api.aitherium.com/settings/connected-devices")
        print()
        if once:
            return 0
        print("Holding the link. Ctrl-C to stop.")
        link = active_node_link()
        last = None
        try:
            while True:
                await asyncio.sleep(15)
                now = link.reach_kind() if link is not None else "none"
                if now != last:
                    # State CHANGES only. A line every 15s is noise nobody reads,
                    # and the transition is the only thing worth seeing.
                    err = getattr(link, "last_error", "") if link is not None else ""
                    print(f"  reach: {now}" + (f" ({err})" if err and now != "ws" else ""))
                    last = now
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\nStopped. The device stays enrolled; re-run `adk rc` to reconnect.")
            return 0

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
