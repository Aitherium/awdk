"""``adk devices`` — the devices enrolled in YOUR workspace, from one registry.

``list`` / ``status <id>`` / ``rm <id>`` over ``{identity}/v1/nodes`` with the
bearer ``adk login`` saved. This is the CLI twin of the portal's connected-devices
page and reads the same store (AitherDirectory via AitherIdentity), so what
``adk enroll`` wrote is what this lists.

Two rules, both about honesty:

* A refusal is printed VERBATIM and exits non-zero. ``402`` carries
  ``subscription_required``, ``403`` carries ``device_quota_exceeded``; the body is
  the product's own explanation and the upgrade path, so it is not summarised,
  truncated or translated.
* No token means no call. The local root placeholder is not an identity, and a
  call made with it would 401 in a way that reads as "the service is down".
"""

from __future__ import annotations

__all__ = [
    "cmd_devices", "enroll_base", "resolve_bearer", "request_nodes", "setup_link",
    "DevicesError",
]

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from adk import fleet_enroll

_DEFAULT_TIMEOUT = 15.0
_ROOT_PLACEHOLDER = "aither_root_local"


class DevicesError(RuntimeError):
    """The request could not be made at all (no token, no network)."""


def enroll_base() -> str:
    """The Identity base that owns ``/v1/nodes`` — same resolution as enrollment."""
    return fleet_enroll.enroll_base_url()


def resolve_bearer() -> str:
    """The bearer to present, or ``""``.

    Order: ``$AITHER_NODE_TOKEN`` (headless / phone), the active ``adk login``
    profile, then the ``api_key`` ``adk login --api-key`` saved to config.json.
    The local root placeholder never counts.
    """
    env_token = (os.environ.get("AITHER_NODE_TOKEN") or "").strip()
    if env_token:
        return env_token
    auth = fleet_enroll._load_auth_config()
    token = auth.get("access_token") or (auth.get("user") or {}).get("api_key") or ""
    if token and token != _ROOT_PLACEHOLDER:
        return str(token)
    try:
        from adk.config import load_saved_config

        saved = load_saved_config() or {}
    except Exception:  # config is optional; its absence is "no token", not an error
        saved = {}
    token = saved.get("api_key") or ""
    if token and token != _ROOT_PLACEHOLDER:
        return str(token)
    return ""


def _send(method: str, url: str, headers: Dict[str, str], timeout: float,
          body: Optional[Dict[str, Any]] = None) -> Tuple[int, str]:
    """One HTTP exchange → ``(status, body_text)``. Tests monkeypatch this."""
    import httpx

    from adk._tls import tls_verify

    with httpx.Client(timeout=timeout, verify=tls_verify()) as client:
        resp = client.request(method, url, headers=headers, json=body)
    return resp.status_code, resp.text


def request_nodes(
    method: str,
    path: str = "",
    *,
    base: Optional[str] = None,
    token: Optional[str] = None,
    timeout: float = _DEFAULT_TIMEOUT,
    body: Optional[Dict[str, Any]] = None,
) -> Tuple[int, str, Any, str]:
    """Call ``{base}/v1/nodes{path}``.

    Returns ``(status, body_text, parsed_json_or_None, url)``.

    Raises:
        DevicesError: no bearer is available, or the request never completed.
    """
    bearer = token if token is not None else resolve_bearer()
    if not bearer:
        raise DevicesError(
            "Not signed in — no bearer to present.\n"
            "  adk login                     # browser flow\n"
            "  adk login --api-key <key>     # headless / phone / VM"
        )
    root = (base or enroll_base()).rstrip("/")
    url = f"{root}/v1/nodes{path}"
    headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json"}
    try:
        status, text = (_send(method, url, headers, timeout, body) if body is not None
                        else _send(method, url, headers, timeout))
    except Exception as e:  # httpx transport errors have many types; name the url
        raise DevicesError(f"{method} {url} failed: {e}") from e
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        parsed = None
    return status, text, parsed, url


def _print_refusal(method: str, url: str, status: int, body: str) -> None:
    """The server's answer, verbatim. 402/403 bodies are the product speaking."""
    print(f"x HTTP {status} from {method} {url}")
    print(body if body else "(empty body)")


def _local_node_id() -> str:
    return str(fleet_enroll._load_node_auth().get("node_id") or "")


def _fmt_inference(node: Dict[str, Any]) -> str:
    kind = node.get("inference_kind") or ("ollama" if node.get("ollama_available")
                                          else "vllm" if node.get("vllm_available") else "-")
    ready = "ready" if node.get("inference_ready") else "not-ready"
    models = node.get("available_models") or []
    first = f" {models[0]}" if models else ""
    return f"{kind} {ready}{first}"


def _print_table(nodes: List[Dict[str, Any]], mine: str) -> None:
    cols = ("node_id", "hostname", "class", "status", "last_seen", "inference", "public_url")
    rows = []
    for n in nodes:
        rows.append((
            str(n.get("node_id", "-")) + (" *" if n.get("node_id") == mine else ""),
            str(n.get("hostname", "-")),
            str(n.get("node_class", "-")),
            str(n.get("status", "-")),
            str(n.get("last_seen", "-"))[:19],
            _fmt_inference(n),
            str(n.get("public_url") or "-"),
        ))
    widths = [max(len(c), *(len(r[i]) for r in rows)) if rows else len(c)
              for i, c in enumerate(cols)]
    print("  ".join(c.ljust(widths[i]) for i, c in enumerate(cols)))
    for r in rows:
        print("  ".join(v.ljust(widths[i]) for i, v in enumerate(r)))
    if mine:
        print()
        print("* this device")


def _list(args) -> int:
    status, text, parsed, url = request_nodes("GET", "")
    if status != 200:
        _print_refusal("GET", url, status, text)
        return 1
    if getattr(args, "json", False):
        print(text)
        return 0
    nodes = (parsed or {}).get("endpoints", []) if isinstance(parsed, dict) else []
    if not nodes:
        print("No devices enrolled in this workspace.")
        print("  adk enroll --inference-url auto   # enrol this one")
        return 0
    _print_table(nodes, _local_node_id())
    print()
    print(f"{len(nodes)} device(s); online {(parsed or {}).get('online', '?')}, "
          f"inference-ready {(parsed or {}).get('inference_ready', '?')}")
    return 0


def _status(args) -> int:
    node_id = getattr(args, "node_id", None) or _local_node_id()
    if not node_id:
        print("x No node id given and this device is not enrolled.")
        print("  adk devices status <node_id>   # another device")
        print("  adk enroll --inference-url auto   # enrol this one first")
        return 1
    status, text, parsed, url = request_nodes("GET", f"/{node_id}")
    if status != 200:
        _print_refusal("GET", url, status, text)
        return 1
    if getattr(args, "json", False) or not isinstance(parsed, dict):
        print(text)
        return 0
    n = parsed
    mine = " (this device)" if node_id == _local_node_id() else ""
    print(f"Device {n.get('node_id', node_id)}{mine}")
    print(f"  hostname:   {n.get('hostname', '-')}")
    print(f"  class:      {n.get('node_class', '-')}")
    print(f"  status:     {n.get('status', '-')}")
    print(f"  last seen:  {n.get('last_seen', '-')}")
    print(f"  inference:  {_fmt_inference(n)}")
    if n.get("inference_url"):
        print(f"  inference_url: {n['inference_url']}")
    print(f"  public_url: {n.get('public_url') or '-'}")
    if n.get("gpu_name"):
        print(f"  gpu:        {n['gpu_name']} ({n.get('gpu_vram_mb', 0)} MB)")
    models = n.get("available_models") or []
    if models:
        print(f"  models:     {', '.join(str(m) for m in models[:8])}")
    return 0


def _rm(args) -> int:
    node_id = getattr(args, "node_id", None) or ""
    if not node_id:
        print("x adk devices rm needs a node id (see: adk devices list)")
        return 2
    status, text, _parsed, url = request_nodes("DELETE", f"/{node_id}")
    if status not in (200, 204):
        _print_refusal("DELETE", url, status, text)
        return 1
    print(f"Removed {node_id}")
    if node_id == _local_node_id():
        # This device's own record is gone server-side; a stale node_auth.json
        # would make the next `adk enroll` say "already enrolled" forever.
        try:
            fleet_enroll._NODE_AUTH_FILE.unlink()
            print("  cleared local enrollment (node_auth.json); re-enrol with: adk enroll")
        except OSError as e:
            print(f"  (could not clear node_auth.json: {e})")
    return 0


def _parse_kv(pairs: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for p in pairs or []:
        k, sep, v = str(p).partition("=")
        if not sep or not k:
            raise DevicesError(f"--arg must be key=value, got {p!r}")
        out[k] = v
    return out


def _command(args, *, sleep=None, clock=None) -> int:
    """``adk devices command <node_id> <verb> [--arg k=v] [--wait]``.

    Queues ONE allow-listed, signed, audited command through Identity (the same
    channel Aither Control uses) and, with --wait, reads the device's signed result
    back from the command history -- the audit trail is the proof, not this process.
    The verb language is the server's: an unknown verb or argument is its 422.
    """
    import time as _time

    sleep = sleep or _time.sleep
    clock = clock or _time.monotonic
    node_id = getattr(args, "node_id", "") or ""
    verb = getattr(args, "verb", "") or ""
    cmd_args = _parse_kv(getattr(args, "arg", None) or [])
    status, text, parsed, url = request_nodes(
        "POST", f"/{node_id}/commands", body={"verb": verb, "args": cmd_args})
    if status != 200 or not isinstance(parsed, dict):
        _print_refusal("POST", url, status, text)
        return 1
    rec = parsed.get("command") or {}
    cmd_id = str(rec.get("id") or "")
    if not getattr(args, "wait", False):
        print(json.dumps(rec) if getattr(args, "json", False) else f"queued {cmd_id} ({verb})")
        return 0
    deadline = clock() + float(getattr(args, "timeout", 300) or 300)
    while True:
        status, text, parsed, url = request_nodes("GET", f"/{node_id}/commands")
        if status == 200 and isinstance(parsed, dict):
            for c in parsed.get("commands") or []:
                if c.get("id") == cmd_id and c.get("status") in ("done", "failed"):
                    if getattr(args, "json", False):
                        print(json.dumps(c))
                    else:
                        print(f"{cmd_id} {verb}: {c.get('status')}")
                        print(f"  output: {str(c.get('output') or '')[:600]}")
                    return 0 if c.get("ok") else 1
        if clock() >= deadline:
            print(f"x {cmd_id} ({verb}) not reported within the timeout; it is still in "
                  f"the device's history: adk devices command-log {node_id}")
            return 3
        sleep(10)


def _command_log(args) -> int:
    node_id = getattr(args, "node_id", "") or ""
    status, text, parsed, url = request_nodes("GET", f"/{node_id}/commands")
    if status != 200 or not isinstance(parsed, dict):
        _print_refusal("GET", url, status, text)
        return 1
    if getattr(args, "json", False):
        print(text)
        return 0
    for c in parsed.get("commands") or []:
        print(f"{c.get('id')}  {c.get('verb'):<20} {c.get('status'):<9} by {c.get('issued_by')}")
    return 0


#: Where a new device without adk gets its installer (the portal's Setup app).
DEFAULT_PORTAL = "https://aitherium.com"
_PAIR_POLL_S = 3.0


def setup_link() -> str:
    """The installer link the QR carries: the portal's Setup app (``$AITHER_PORTAL_URL``)."""
    portal = (os.environ.get("AITHER_PORTAL_URL") or "").strip() or DEFAULT_PORTAL
    return f"{portal.rstrip('/')}/?app=setup"


def _add(args, *, sleep=None, clock=None) -> int:
    """``adk devices add [--no-wait] [--timeout S]`` -- the CLI twin of the portal's
    "Pair a machine with a code".

    Mints a short-lived, single-use pairing code bound to the signed-in caller
    (Identity ``/v1/nodes/pairing/init``: the subscription and device-cap gates run
    there, while the person who can fix them is looking), prints it with the command
    the new device runs and a QR of the installer link, then waits until that device
    has confirmed (``/pairing/status/<code>``).

    Returns:
        0 paired (or ``--no-wait``), 1 refused, 3 the code expired unused.
    """
    import time as _time

    from adk.term_qr import print_qr

    sleep = sleep or _time.sleep
    clock = clock or _time.monotonic
    status, text, parsed, url = request_nodes("POST", "/pairing/init", timeout=30.0)
    if status != 200 or not isinstance(parsed, dict):
        _print_refusal("POST", url, status, text)
        return 1
    code = str(parsed.get("code") or "")
    if not code:
        print(f"x {url} answered without a pairing code")
        return 1
    ttl = int(parsed.get("expires_in") or 300)
    link = setup_link()
    print("Add a device")
    print(f"  Code:     {code}   (single use, lives {max(1, ttl // 60)} min)")
    print()
    print("  On the new device (laptop, desktop, Steam Deck, phone with a terminal):")
    print(f"    adk pair {code}")
    print()
    print("  No adk there yet? Scan this to open the installer, then run the line above:")
    print(f"    {link}")
    print_qr(link, print, indent="    ")
    print()
    print("  No code at all: run `adk pair --join` on the new device and scan ITS QR")
    print("  with a phone signed in to this account.")
    if getattr(args, "no_wait", False):
        return 0
    print()
    print("Waiting for the device ... (Ctrl-C stops waiting; the code stays valid)")
    deadline = clock() + float(getattr(args, "timeout", None) or ttl + 30)
    try:
        while clock() < deadline:
            status, _text, parsed, _url = request_nodes("GET", f"/pairing/status/{code}")
            if status == 200 and isinstance(parsed, dict) and parsed.get("confirmed"):
                print(f"  paired: {parsed.get('node_id') or '(node id pending)'}")
                print("  adk devices list   # it is in your workspace now")
                return 0
            sleep(_PAIR_POLL_S)
    except KeyboardInterrupt:
        print()
        print("Stopped waiting. `adk devices list` shows it once it pairs.")
        return 0
    print("x Nobody used the code in time; run `adk devices add` again.")
    return 3


def cmd_devices(args) -> int:
    """Dispatch ``adk devices <list|add|status|rm|command|command-log>`` (bare = list).

    Returns the exit code.
    """
    sub = getattr(args, "devices_command", None) or "list"
    handlers = {"list": _list, "add": _add, "status": _status, "rm": _rm,
                "command": _command, "command-log": _command_log}
    if sub not in handlers:
        print("usage: adk devices {list,add,status,rm,command,command-log} ...")
        return 2
    try:
        return handlers[sub](args)
    except DevicesError as e:
        print(f"x {e}")
        return 1
