"""``adk onboard-remote`` -- add a computer you can SSH into, from here, in one command.

The goal: a freshly reset box (a spare desktop, a Linux server) joins
your devices with NOTHING typed on it. Connectivity is the credential: if this
machine can SSH into the box, that is enough. The run is the same product path a
customer uses by hand -- nothing here is a side door:

1. **mint**   a pairing code as the signed-in owner (Identity ``/v1/nodes/pairing/init``;
              the subscription gate and device cap answer HERE, on the owner's screen);
2. **probe**  the box over SSH (Linux or Windows OpenSSH);
3. **install** awdk into a per-user venv (``~/.adk-venv``) -- from PyPI, or a wheel
              this machine uploads (air-gapped boxes, unreleased builds);
4. **pair**   ``adk pair -`` on the box, the code piped over the SSH channel's stdin
              (never on an argv another user could read in ``ps``);
5. **beat**   the heartbeat keeps running after the SSH session ends (the per-user
              unit ``adk pair`` installs, or a detached process where there is no
              systemd user session, e.g. a container);
6. **verify** the node is ``online`` in the owner's device list (Identity ``/v1/nodes``).

After step 6 the box takes the owner's signed commands (``adk.node_commands``: the C2
channel is the node dialling OUT, so no inbound port stays open once SSH is closed)
and advertises its inference server so the household/compute pool can route to it.

Every step reports ``{"step", "ok", "detail"}``; the run never raises. ``plan=True``
prints the steps without touching the box or minting a code.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: What a node class may be (Identity normalizes; keep in step with ``adk pair``).
NODE_CLASSES = ("phone", "laptop", "desktop", "deck", "spark", "sovereign")
_CODE_RE = re.compile(r"^[A-Z0-9]{4,12}$")
_NODE_RE = re.compile(r"^[A-Za-z0-9._:-]{3,128}$")


# ── the scripts that run ON the box ──────────────────────────────────────────


def linux_script(code: str, *, node_class: str, inference_url: str, pip_spec: str,
                 wheel_name: str = "") -> str:
    """The bash program piped to ``bash -s`` on a Linux box.

    The pairing code lives only inside this script, which travels on the SSH
    channel's stdin. ``@@`` lines are the protocol the controller parses.
    """
    pair_args = f"--node-class {shlex.quote(node_class)}"
    if inference_url:
        pair_args += f" --inference-url {shlex.quote(inference_url)}"
    return f"""set -u
CODE={shlex.quote(code)}
VENV="$HOME/.adk-venv"
WHEEL={shlex.quote("/tmp/" + wheel_name if wheel_name else "/nonexistent")}
SUDO=""
if [ "$(id -u)" != 0 ]; then sudo -n true 2>/dev/null && SUDO="sudo -n"; fi
pkg_install() {{
  if command -v apt-get >/dev/null 2>&1; then
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" >/dev/null 2>&1
  elif command -v dnf >/dev/null 2>&1; then $SUDO dnf install -y -q "$@" >/dev/null 2>&1
  elif command -v apk >/dev/null 2>&1; then $SUDO apk add -q "$@" >/dev/null 2>&1
  else return 1; fi
}}
echo "@@os $(uname -s) $(uname -m)"
PY=$(command -v python3 || true)
if [ -z "$PY" ]; then pkg_install python3 python3-venv || true; PY=$(command -v python3 || true); fi
if [ -z "$PY" ]; then
  echo "@@fail install python3 missing and could not be installed (no root/sudo -n)"
  exit 20
fi
echo "@@python $($PY -c 'import sys;print(sys.version.split()[0])')"
if [ ! -x "$VENV/bin/adk" ] || [ -n "${{AITHER_FORCE_REINSTALL:-}}" ] || [ -f "$WHEEL" ]; then
  if [ ! -x "$VENV/bin/pip" ]; then
    if ! "$PY" -m venv "$VENV" >/dev/null 2>&1; then
      pkg_install python3-venv python3-pip || true
      rm -rf "$VENV"
      "$PY" -m venv "$VENV" >/dev/null 2>&1
    fi
  fi
  if [ ! -x "$VENV/bin/pip" ]; then
    echo "@@fail install could not create a python venv"; exit 21
  fi
  "$VENV/bin/pip" install -q --upgrade pip >/dev/null 2>&1
  SPEC={shlex.quote(pip_spec)}
  [ -f "$WHEEL" ] && SPEC="$WHEEL"
  if ! "$VENV/bin/pip" install -q "$SPEC" > /tmp/aither-pip.log 2>&1; then
    echo "@@fail install pip install failed: $(tail -2 /tmp/aither-pip.log | tr '\\n' ' ')"; exit 22
  fi
  rm -f "$WHEEL"
fi
VER_PY='from importlib.metadata import version;print(version("awdk"))'
echo "@@adk $("$VENV/bin/python" -c "$VER_PY" 2>/dev/null)"
if "$VENV/bin/adk" pair --help 2>/dev/null | grep -q -- --inference-url; then
  printf '%s\\n' "$CODE" | "$VENV/bin/adk" pair - {pair_args} 2>&1 | sed 's/^/@@pair /'
else
  echo "@@warn this awdk has no 'adk pair -'; the single-use code goes on argv"
  "$VENV/bin/adk" pair "$CODE" --node-class {shlex.quote(node_class)} 2>&1 | sed 's/^/@@pair /'
fi
NA_PY='import json,os;p=os.path.expanduser("~/.aither/node_auth.json")
print(json.load(open(p)).get("node_id",""))'
NODE=$("$VENV/bin/python" -c "$NA_PY" 2>/dev/null)
echo "@@node_id $NODE"
if systemctl --user is-active --quiet aither-node-beat 2>/dev/null; then
  echo "@@beat systemd-user"
else
  PIDF="$HOME/.aither/node-beat.pid"
  if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
    echo "@@beat already-running"
  else
    mkdir -p "$HOME/.aither/logs"
    LOG="$HOME/.aither/logs/node-beat.log"
    nohup setsid "$VENV/bin/python" -m adk.node_beat >>"$LOG" 2>&1 </dev/null &
    echo $! > "$PIDF"
    echo "@@beat detached"
  fi
fi
"""


def windows_script(code: str, *, node_class: str, inference_url: str, pip_spec: str,
                   wheel_name: str = "") -> str:
    """The PowerShell program piped to ``powershell -Command -`` on Windows OpenSSH.

    Not yet proven against a live Windows box (the dry run proves the Linux path).
    The heartbeat is registered as a per-user logon task AND started now, because a
    process started inside an OpenSSH session dies with the session's job object.
    """
    q = lambda s: "'" + str(s).replace("'", "''") + "'"  # noqa: E731 - PS single quote
    infer = f" --inference-url {q(inference_url)}" if inference_url else ""
    return f"""$ErrorActionPreference = 'Continue'
$code = {q(code)}
$venv = Join-Path $env:USERPROFILE '.adk-venv'
Write-Output "@@os Windows $env:PROCESSOR_ARCHITECTURE"
$py = (Get-Command py -ErrorAction SilentlyContinue)
if (-not $py) {{
  winget install -e --id Python.Python.3.12 --scope user --silent `
    --accept-package-agreements --accept-source-agreements | Out-Null
  $env:Path = [Environment]::GetEnvironmentVariable('Path','User') + ';' + $env:Path
  $py = (Get-Command py -ErrorAction SilentlyContinue)
}}
if (-not $py) {{
  Write-Output '@@fail install python missing and winget could not install it'; exit 20
}}
$wheel = Join-Path $env:USERPROFILE {q(wheel_name or 'none.whl')}
if (-not (Test-Path (Join-Path $venv 'Scripts\\adk.exe')) -or (Test-Path $wheel)) {{
  & py -3 -m venv $venv
  $spec = {q(pip_spec)}
  if (Test-Path $wheel) {{ $spec = $wheel }}
  & (Join-Path $venv 'Scripts\\python.exe') -m pip install -q --upgrade pip | Out-Null
  & (Join-Path $venv 'Scripts\\python.exe') -m pip install -q $spec
  if ($LASTEXITCODE -ne 0) {{ Write-Output '@@fail install pip install failed'; exit 22 }}
  Remove-Item $wheel -ErrorAction SilentlyContinue
}}
$verPy = 'from importlib.metadata import version;print(version(''awdk''))'
Write-Output "@@adk $(& (Join-Path $venv 'Scripts\\python.exe') -c $verPy)"
$adk = Join-Path $venv 'Scripts\\adk.exe'
$code | & $adk pair - --node-class {q(node_class)}{infer} 2>&1 | ForEach-Object {{ "@@pair $_" }}
$naPath = Join-Path $env:USERPROFILE '.aither\\node_auth.json'
$na = Get-Content $naPath -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json
Write-Output "@@node_id $($na.node_id)"
$pyw = Join-Path $venv 'Scripts\\pythonw.exe'
schtasks /Create /F /SC ONLOGON /TN AitherNodeBeat /TR "`"$pyw`" -m adk.node_beat" | Out-Null
schtasks /Run /TN AitherNodeBeat | Out-Null
Write-Output '@@beat scheduled-task'
"""


# ── controller side ──────────────────────────────────────────────────────────


def parse_protocol(output: str) -> Dict[str, Any]:
    """Fold the ``@@`` lines a box printed into one dict."""
    out: Dict[str, Any] = {"pair": [], "warn": []}
    for raw in (output or "").splitlines():
        line = raw.strip()
        if not line.startswith("@@"):
            continue
        tag, _, rest = line[2:].partition(" ")
        if tag in ("pair", "warn"):
            out[tag].append(rest)
        elif tag == "fail":
            out["fail"] = rest
        else:
            out[tag] = rest.strip()
    pair_text = "\n".join(out["pair"])
    out["paired"] = "Paired as node" in pair_text
    return out


def ssh_argv(host: str, user: str, *, port: int = 22, key_path: str = "",
             known_hosts: str = "") -> List[str]:
    """The OpenSSH argv for one non-interactive session (never prompts)."""
    argv = ["ssh", "-p", str(int(port)), "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
            "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=15"]
    if known_hosts:
        argv += ["-o", f"UserKnownHostsFile={known_hosts}"]
    if key_path:
        argv += ["-i", key_path, "-o", "IdentitiesOnly=yes"]
    return argv + [f"{user}@{host}"]


def _run(argv: List[str], *, stdin: str = "", timeout: float = 60) -> Tuple[int, str]:
    try:
        # Bytes, not text mode: on Windows text mode writes CRLF into the remote
        # shell's stdin and bash reads `set -u<CR>` as an invalid option.
        r = subprocess.run(argv, input=stdin.encode("utf-8"), capture_output=True,
                           timeout=timeout)
        return r.returncode, (r.stdout or b"").decode("utf-8", "replace") + (
            r.stderr or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {int(timeout)} s"
    except FileNotFoundError as exc:
        return 127, f"{exc.filename or argv[0]} not found"


def _http(method: str, url: str, bearer: str, body: Optional[dict] = None,
          timeout: float = 30, attempts: int = 3) -> Tuple[int, Any]:
    import httpx

    headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json"}
    last: Tuple[int, Any] = (0, "")
    # Nothing answered (timeout, reset) is retried; an ANSWER never is -- a mint that
    # got a response was either issued or refused, and a retry would mint a second.
    for attempt in range(attempts):
        try:
            with httpx.Client(timeout=timeout) as c:
                r = c.request(method, url, headers=headers, json=body)
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, r.text[:300]
        except Exception as exc:  # noqa: BLE001 - reported as a step failure
            last = (0, f"{type(exc).__name__}: {exc}"[:300])
            if attempt + 1 < attempts:
                time.sleep(2 * (attempt + 1))
    return last


def _session_bearer() -> str:
    """The owner bearer: ``adk login`` / ``$AITHER_NODE_TOKEN``, else the gateway
    session bearer file this host's tools already use."""
    try:
        from adk.devices import resolve_bearer

        tok = resolve_bearer()
        if tok:
            return tok
    except Exception as exc:  # noqa: BLE001 - fall through to the file
        _ = exc  # no adk login on this host: the gateway session-bearer file below
    p = Path.home() / ".aither" / "session-bearer"
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _identity_base() -> str:
    try:
        from adk.devices import enroll_base

        return enroll_base()
    except Exception:  # noqa: BLE001
        return (os.environ.get("AITHER_ENROLL_BASE") or "https://idp.aitherium.com").rstrip("/")


def mint_code(identity: str, bearer: str) -> Tuple[str, str]:
    """``(code, error)`` from Identity ``/v1/nodes/pairing/init`` as the caller."""
    status, body = _http("POST", f"{identity}/v1/nodes/pairing/init", bearer)
    if status == 200 and isinstance(body, dict) and body.get("code"):
        return str(body["code"]), ""
    detail = body.get("detail") if isinstance(body, dict) else body
    return "", f"HTTP {status}: {str(detail)[:200]}"


def wait_online(identity: str, bearer: str, node_id: str, *, timeout_s: float = 120,
                poll_s: float = 5) -> Dict[str, Any]:
    """Poll the owner's device list until ``node_id`` is online (or time runs out)."""
    deadline = time.monotonic() + timeout_s
    last: Dict[str, Any] = {}
    while True:
        status, body = _http("GET", f"{identity}/v1/nodes/{node_id}", bearer)
        if status == 200 and isinstance(body, dict):
            node = body.get("node", body)
            last = {k: node.get(k) for k in ("node_id", "hostname", "status", "node_class",
                                              "inference_ready", "inference_kind",
                                              "inference_url", "last_seen")}
            if str(node.get("status")) == "online":
                return {"online": True, **last}
        else:
            last = {"http": status}
        if time.monotonic() >= deadline:
            return {"online": False, **last}
        time.sleep(poll_s)


def onboard_remote(host: str, user: str, *, port: int = 22, key_path: str = "",
                   node_class: str = "desktop", inference_url: str = "",
                   pip_spec: str = "awdk", code: str = "", bearer: str = "",
                   identity: str = "", windows: Optional[bool] = None,
                   known_hosts: str = "", plan: bool = False,
                   install_timeout_s: float = 900, online_timeout_s: float = 120
                   ) -> Dict[str, Any]:
    """Onboard the box at ``user@host`` into the owner's devices. Never raises.

    Returns ``{"ok", "node_id", "steps": [...], "device": {...}}``. ``ok`` is true
    only when the box is ONLINE in the owner's device list -- a pair that printed
    success but never beat is not onboarded.
    """
    steps: List[Dict[str, Any]] = []

    def step(name: str, ok: bool, detail: Any = "") -> Dict[str, Any]:
        rec = {"step": name, "ok": bool(ok), "detail": detail}
        steps.append(rec)
        return rec

    def done(ok: bool, **extra: Any) -> Dict[str, Any]:
        return {"ok": ok, "host": host, "steps": steps, **extra}

    if not host or not user:
        step("args", False, "host and user are required")
        return done(False)
    if node_class not in NODE_CLASSES:
        step("args", False, f"node_class must be one of {', '.join(NODE_CLASSES)}")
        return done(False)
    if code and not _CODE_RE.match(code.strip().upper()):
        step("args", False, "code is not a pairing code")
        return done(False)
    identity = (identity or _identity_base()).rstrip("/")
    wheel = pip_spec if pip_spec.endswith(".whl") and os.path.isfile(pip_spec) else ""
    if plan:
        for name, what in (
                ("mint", "existing code" if code else f"POST {identity}/v1/nodes/pairing/init"),
                ("probe", f"ssh {user}@{host}:{port}"),
                ("install", "~/.adk-venv <- "
                 + ('upload ' + os.path.basename(wheel) if wheel else pip_spec)),
                ("pair", f"adk pair - --node-class {node_class}"
                         + (f" --inference-url {inference_url}" if inference_url else "")),
                ("beat", "per-user heartbeat survives the SSH session"),
                ("verify", f"GET {identity}/v1/nodes/<node_id> until online")):
            step(name, True, what)
        return done(True, plan=True)

    bearer = bearer or _session_bearer()
    if not bearer:
        step("mint", False, "not signed in here: run `adk login` (or set AITHER_NODE_TOKEN)")
        return done(False)

    base = ssh_argv(host, user, port=port, key_path=key_path, known_hosts=known_hosts)
    rc, out = _run(base + ["uname -s 2>/dev/null || echo WINDOWS"], timeout=40)
    if rc != 0 and "WINDOWS" not in out:
        step("probe", False, out.strip()[-300:] or f"ssh exit {rc}")
        return done(False)
    first = (out.strip().splitlines() or [""])[0].strip()
    is_windows = windows if windows is not None else (first not in ("Linux", "Darwin"))
    step("probe", True, {"os": "Windows" if is_windows else first})

    if wheel:
        # pip needs the real wheel filename. Windows: scp lands relative to the
        # home dir, where the script looks.
        name = os.path.basename(wheel)
        dest = name if is_windows else f"/tmp/{name}"
        scp = ["scp", "-P", str(int(port))] + base[3:-1] + [wheel, f"{user}@{host}:{dest}"]
        rc, out = _run(scp, timeout=180)
        if rc != 0:
            step("upload", False, out.strip()[-300:])
            return done(False)
        step("upload", True, os.path.basename(wheel))

    if not code:
        code, err = mint_code(identity, bearer)
        if not code:
            step("mint", False, err)
            return done(False)
    step("mint", True, "single-use code, 5 min")  # never the code itself

    make = windows_script if is_windows else linux_script
    script = make(code.strip().upper(), node_class=node_class, inference_url=inference_url,
                  pip_spec=pip_spec, wheel_name=os.path.basename(wheel) if wheel else "")
    remote = (["powershell", "-NoProfile", "-NonInteractive", "-Command", "-"]
              if is_windows else ["bash", "-s"])
    rc, out = _run(base + remote, stdin=script, timeout=install_timeout_s)
    proto = parse_protocol(out)
    if proto.get("fail"):
        name, _, why = str(proto["fail"]).partition(" ")
        step(name or "install", False, why)
        return done(False, output_tail=out.strip()[-600:])
    if not proto.get("adk"):
        step("install", False, out.strip()[-400:] or f"remote exit {rc}")
        return done(False)
    step("install", True, {"python": proto.get("python"), "awdk": proto.get("adk"),
                           "os": proto.get("os")})
    node_id = str(proto.get("node_id") or "").strip()
    if not proto["paired"] or not _NODE_RE.match(node_id):
        step("pair", False, " | ".join(proto["pair"])[-400:] or out.strip()[-400:])
        return done(False)
    step("pair", True, {"node_id": node_id, "warnings": proto["warn"]})
    step("beat", bool(proto.get("beat")), proto.get("beat") or "heartbeat not confirmed")

    device = wait_online(identity, bearer, node_id, timeout_s=online_timeout_s)
    step("verify", bool(device.get("online")), device)
    return done(bool(device.get("online")), node_id=node_id, device=device)


def cmd_onboard_remote(args: Any) -> int:
    """CLI: ``adk onboard-remote HOST --user U [--key PATH] [--port N] ...``."""
    res = onboard_remote(
        args.host, args.user, port=args.port, key_path=args.key or "",
        node_class=args.node_class, inference_url=args.inference_url or "",
        pip_spec=args.pip_spec, identity=args.identity or "",
        windows=True if args.windows else None, plan=args.plan,
        online_timeout_s=args.online_timeout)
    if getattr(args, "json", False):
        print(json.dumps(res, indent=2, default=str))
    else:
        for s in res["steps"]:
            mark = "ok " if s["ok"] else "FAIL"
            detail = s["detail"]
            if not isinstance(detail, str):
                detail = json.dumps(detail, default=str)
            print(f"  [{mark}] {s['step']:<8} {detail}")
        if res.get("node_id"):
            print(f"  node: {res['node_id']} ({'online' if res['ok'] else 'NOT online'})")
    return 0 if res["ok"] else 1


def add_parser(sub: Any) -> None:
    p = sub.add_parser(
        "onboard-remote",
        help="Add a computer you can SSH into to your devices (install, pair, verify online)")
    p.add_argument("host", help="Hostname or IP of the box")
    p.add_argument("--user", required=True, help="SSH user on the box")
    p.add_argument("--key", default="", help="SSH private key file (default: ssh agent/config)")
    p.add_argument("--port", type=int, default=22)
    p.add_argument("--node-class", choices=list(NODE_CLASSES), default="desktop")
    p.add_argument("--inference-url", default="",
                   help="Advertise this server on the box (default: probe the usual ports)")
    p.add_argument("--pip-spec", default="awdk",
                   help="pip requirement, or a local .whl to upload (default: awdk from PyPI)")
    p.add_argument("--identity", default="", help="Identity base (default: as adk pair)")
    p.add_argument("--windows", action="store_true", help="Force the Windows path")
    p.add_argument("--online-timeout", type=float, default=120.0)
    p.add_argument("--plan", action="store_true", help="Print the steps; touch nothing")
    p.add_argument("--json", action="store_true")
