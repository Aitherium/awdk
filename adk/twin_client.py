"""`adk agent twin deploy|list|remove` -- the scaffolded agent as a SELF-HOSTED twin.

A self-hosted twin is the agent in this project, served by `adk serve` on the
owner's own machine and model, registered with the platform so chat, runs and
approvals route to it. Nothing is created at a model provider: no hosted agent,
no session, no provider key leaves the machine.

The route is the one the platform already serves for this:
``POST /v1/agent/agent-endpoints`` with ``provider: "self_hosted"`` and
``bind: true`` (Genesis ``routers/agent_binding.py::register_agent_endpoint``).
``POST /v1/agent/managed/deploy`` is NOT used: it creates a hosted agent in an
Anthropic account, which is a different twin and costs money.

``--dry-run`` builds the same request, prints it with the callback bearer
redacted, says what is still missing, and sends nothing.

``deploy --self-host`` is the whole path in one command: it also STARTS the agent as
a local service (``adk.twin_selfhost``) and needs no public URL -- the twin is
registered on this machine's own mesh address, or with ``--reach local`` it runs on
loopback and is listed from this machine's record only. ``remove`` undoes it.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

TWIN_VERBS = ("deploy", "list", "rebind", "doors", "remove")
REGISTER_PATH = "/agent-endpoints"
HOSTED_PATH = "/managed/deploy"
REBIND_PATH = "/managed/twins/rebind-tools"
QUOTE_PATH = "/managed/twins/dedicated-quote"
DOORS_PATH = "/managed/twins/doors"
#: The plan feature the register route is gated by (config/tier_features.yaml). It is
#: on the free tier: self-hosting a twin needs no paid plan.
REQUIRED_FEATURE = "self_hosted_fleet"
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")
_PRIVATE_HOSTS = ("localhost", "127.", "0.0.0.0", "10.", "192.168.", "::1")
_UNSET: Any = object()
#: Stands in the plan for a callback bearer that is only read or minted at deploy.
PENDING_TOKEN = "<set at deploy>"


def add_parser(agent_sub: Any) -> None:
    """Register ``adk agent twin ...``."""
    twin = agent_sub.add_parser(
        "twin", help="Deploy this project's agent as a self-hosted twin: deploy|list")
    tsub = twin.add_subparsers(dest="twin_command")
    dep = tsub.add_parser(
        "deploy", help="Register this project's agent as a self-hosted twin (--dry-run = plan only)")
    dep.add_argument("--project", default=".", help="Project directory (default: current)")
    dep.add_argument("--name", default="", help="Twin name (default: identity in config.yaml)")
    dep.add_argument("--invoke-url", dest="invoke_url", default="",
                     help="Public https URL where this agent's `adk serve` answers")
    dep.add_argument("--reach", default="", choices=["", "tunnel", "mesh", "local"],
                     help="tunnel = a public URL (default without --self-host); mesh = this "
                          "machine's own mesh address, no public URL (default with "
                          "--self-host); local = loopback only, not registered")
    dep.add_argument("--self-host", dest="self_host", action="store_true",
                     help="Also START this project's agent here as a local service, then "
                          "register it. No public URL and no key to set")
    dep.add_argument("--port", type=int, default=0,
                     help="With --self-host: port to serve on (default: config.yaml, "
                          "stepping to the next free one)")
    dep.add_argument("--no-bind", dest="no_bind", action="store_true",
                     help="Register the endpoint only; do not add it to your account's "
                          "chat-routable agents")
    dep.add_argument("--model", default="", help="Model your agent answers on (informational)")
    dep.add_argument("--token-env", dest="token_env", default="AITHER_SERVER_API_KEY",
                     help="Env var holding the bearer the platform presents to your agent")
    dep.add_argument("--dry-run", dest="dry_run", action="store_true",
                     help="Show the plan and the request; send nothing, cost nothing")
    dep.add_argument("--hosted", action="store_true",
                     help="Instead of self-hosting, ask Aitherium to host your bound agent "
                          "on its own compute (one free per account, daily turn limit)")
    dep.add_argument("--anthropic", action="store_true",
                     help="Host your bound agent at Anthropic on YOUR Anthropic key "
                          "(store it first: adk agent managed byok). Anthropic bills you")
    dep.add_argument("--dedicated", action="store_true",
                     help="Show the plan and hourly rental cost of a GPU of your own for "
                          "this twin (no turn limit). Quotes only: this never rents")
    dep.add_argument("--json", dest="json_output", action="store_true")
    ls = tsub.add_parser("list", help="List your self-hosted twin endpoints")
    ls.add_argument("--json", dest="json_output", action="store_true")
    rm = tsub.add_parser(
        "remove", help="Stop a twin's local service and remove its registration")
    rm.add_argument("name", help="Twin name")
    rm.add_argument("--json", dest="json_output", action="store_true")
    rb = tsub.add_parser(
        "rebind", help="Record each twin's pack tool whitelist where none is recorded "
                       "(shows the plan; --apply writes it)")
    rb.add_argument("--apply", action="store_true", help="Write the plan (default: show only)")
    rb.add_argument("--json", dest="json_output", action="store_true")
    dr = tsub.add_parser(
        "doors", help="Every place a twin's model can run: cost, limits, what works today")
    dr.add_argument("--json", dest="json_output", action="store_true")


def read_project(project: str) -> dict[str, Any]:
    """``{dir, has_agent, identity, model, port}`` from a scaffolded project.

    Reads only ``config.yaml`` keys the scaffold writes; never imports ``agent.py``.
    """
    root = Path(project or ".").resolve()
    out: dict[str, Any] = {"dir": str(root), "has_agent": (root / "agent.py").is_file(),
                           "identity": "", "model": "", "port": 8080}
    cfg = root / "config.yaml"
    if cfg.is_file():
        try:
            import yaml

            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 - an unreadable config is "no config"
            data = {}
        if isinstance(data, dict):
            out["identity"] = str(data.get("identity") or data.get("name") or "").strip()
            out["model"] = str(data.get("model") or "").strip()
            try:
                out["port"] = int(data.get("port") or 8080)
            except (TypeError, ValueError):
                pass
    return out


def _is_public_https(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and bool(host) and not host.startswith(_PRIVATE_HOSTS)


def build_plan(args: Any, *, env: Optional[dict] = None, base: str = "",
               credential_source: str = "") -> dict[str, Any]:
    """The deploy plan: the exact request plus every check that must pass first.

    Pure: reads the project directory and ``env``, calls nothing. ``ready`` is True
    only when every check passes; ``deploy`` refuses to send a plan that is not ready.
    """
    env = os.environ if env is None else env
    project = read_project(getattr(args, "project", ".") or ".")
    name = (getattr(args, "name", "") or project["identity"]).strip()
    invoke_url = (getattr(args, "invoke_url", "") or "").strip().rstrip("/")
    reach = (getattr(args, "reach", "") or "tunnel").strip()
    model = (getattr(args, "model", "") or project["model"]).strip()
    token_env = (getattr(args, "token_env", "") or "").strip()
    token = (env.get(token_env) or "").strip() if token_env else ""

    url_ok = (bool(invoke_url) and reach != "local"
              and (reach == "mesh" or _is_public_https(invoke_url)))
    if reach == "local":
        url_detail = "reach=local is a twin this command starts itself: add --self-host"
    elif invoke_url:
        url_detail = invoke_url if url_ok else f"{invoke_url} is not a public https URL"
    else:
        url_detail = ("missing: this form registers a server you already run and exposed, "
                      "so it needs that server's public https URL (--invoke-url)")
    checks = [
        _check("project", bool(project["has_agent"]),
               project["dir"] if project["has_agent"] else f"no agent.py in {project['dir']}",
               fix=["adk init <name>    # scaffold an agent, then run this in its directory"]),
        _check("name", bool(_NAME_RE.match(name)),
               name or "no twin name", fix=["pass --name, or set identity in config.yaml"]),
        _check("invoke_url", url_ok, url_detail,
               fix=["adk agent twin deploy --self-host    "
                    "# starts the agent here and registers it; no public URL"]),
        _check("sign-in", bool(credential_source),
               credential_source or "not signed in", fix=["adk login"]),
        # Optional to the route, so it never blocks a deploy; shown as a note.
        _check("callback bearer", bool(token),
               f"${token_env} is set" if token else (
                   f"${token_env or 'AITHER_SERVER_API_KEY'} is not set: your agent's "
                   "server would answer the platform without a bearer check"),
               required=False),
    ]
    body: dict[str, Any] = {"name": name, "invoke_url": invoke_url, "provider": "self_hosted",
                            "reach": reach, "agent_type": "adk-agent",
                            "bind": getattr(args, "no_bind", False) is not True}
    if model:
        body["model"] = model
    if token:
        body["token"] = token
    return {
        "twin": {"name": name, "kind": "self_hosted", "model": model or "(your agent's own)",
                 "serves_from": project["dir"], "local_port": project["port"]},
        "request": {"method": "POST", "url": f"{base}/v1/agent{REGISTER_PATH}", "body": body},
        "cost": {
            "this_command_usd": 0,
            "provider_agents_created": 0,
            "model_spend": "none here: the twin answers on your own model and keys",
            "plan": ("no paid plan needed: a twin you host yourself is free, with no "
                     "count or turn limit"),
        },
        "checks": checks,
        "ready": _ready(checks),
    }


def build_anthropic_plan(*, base: str = "", credential_source: str = "") -> dict[str, Any]:
    """The plan for ``--anthropic``: your bound agent as a managed agent at Anthropic.

    It is created in YOUR Anthropic account with the key you stored
    (``adk agent managed byok``); the server refuses the deploy without one. Running it
    for real creates an agent at Anthropic, and Anthropic bills you for its use.
    """
    checks = [{"check": "sign-in", "ok": bool(credential_source),
               "detail": credential_source or "not signed in: run `adk login`"}]
    return {
        "twin": {"name": "(the agent bound to your account)", "kind": "hosted",
                 "model": "a Claude model, at Anthropic", "serves_from": "your Anthropic account",
                 "local_port": None},
        "request": {"method": "POST", "url": f"{base}/v1/agent{HOSTED_PATH}",
                    "body": {"host": "anthropic"}},
        "cost": {
            "this_command_usd": 0,
            "provider_agents_created": 1,
            "model_spend": "billed to you by Anthropic, on your own key; nothing to Aitherium",
            "plan": "not counted and not turn-limited by Aitherium",
        },
        "checks": checks,
        "ready": bool(credential_source),
    }


def _check(name: str, ok: bool, detail: str, *, fix: Optional[list] = None,
           required: bool = True) -> dict[str, Any]:
    """One plan check. ``fix`` = the commands that make a failing check pass; a check
    that is not ``required`` is a note and never makes the plan unready."""
    return {"check": name, "ok": bool(ok), "detail": detail, "required": required,
            "fix": [] if ok else list(fix or [])}


def _ready(checks: list) -> bool:
    return all(c["ok"] for c in checks if c.get("required", True))


def missing(plan: dict[str, Any]) -> list[str]:
    """One line per missing prerequisite, each with the command that fixes it (and
    the alternative, when there is one). Empty when the plan is ready."""
    out = []
    for c in plan.get("checks") or []:
        if c["ok"] or not c.get("required", True):
            continue
        fixes = [f.split("    #")[0].strip() for f in c.get("fix") or []]
        line = f"{c['check']}: {c['detail']}"
        if fixes:
            line += f" -- fix: `{fixes[0]}`" + "".join(f", or `{f}`" for f in fixes[1:])
        out.append(line)
    return out


def build_selfhost_plan(args: Any, *, env: Optional[dict] = None, base: str = "",
                        credential_source: str = "", mesh_ip: Optional[str] = None,
                        port_owner: Any = None, backend: Any = None,
                        state: Any = _UNSET, alive: Any = None) -> dict[str, Any]:
    """The plan for ``deploy --self-host``: start the agent here, then register it.

    Same shape as :func:`build_plan` plus ``service`` (what will run on this machine)
    and ``reach``. Writes nothing and sends nothing to the platform; it reads the
    project, this machine's twin record, its mesh address, the port and the local
    model. Every probe is injectable so the logic is testable without a machine.
    """
    from adk import twin_selfhost as sh

    env = os.environ if env is None else env
    project = read_project(getattr(args, "project", ".") or ".")
    name = (getattr(args, "name", "") or project["identity"]).strip()
    name_ok = bool(_NAME_RE.match(name))
    model = (getattr(args, "model", "") or project["model"]).strip()
    invoke_url = (getattr(args, "invoke_url", "") or "").strip().rstrip("/")
    if mesh_ip is None:
        mesh_ip = sh.overlay_ip(env)
    reach = sh.choose_reach(getattr(args, "reach", "") or "", invoke_url, mesh_ip)
    if reach["reach"] == "tunnel" and invoke_url and not _is_public_https(invoke_url):
        reach.update(ok=False, detail=f"{invoke_url} is not a public https URL",
                     fix=["adk agent twin deploy --self-host --reach mesh    "
                          "# no public URL: your private mesh"])

    if state is _UNSET:
        state = sh.read_state(name) if name_ok else None
    is_running = sh.running(state, alive=alive)
    want_port = int(getattr(args, "port", 0) or 0)
    port_err = ""
    if is_running:
        port = int(state.get("port") or 0)
        port_detail = f":{port} (already running, pid {state.get('pid')})"
    else:
        if port_owner is None:
            port_owner = sh.port_owner
        port, port_note, port_err = sh.pick_port(
            want_port or int(project["port"]), explicit=bool(want_port), owner=port_owner)
        port_detail = port_err or (f":{port}" + (f" ({port_note})" if port_note else ""))

    have_model, model_desc = (backend or _local_backend)()

    registers = bool(reach["platform"])
    if reach["reach"] == "mesh":
        url = f"http://{reach['url_host']}:{port}" if reach["url_host"] else ""
    else:
        url = invoke_url if reach["reach"] == "tunnel" else ""
    bearer_src = ""
    if registers:
        bearer_src = ("$AITHER_SERVER_API_KEY" if (env.get("AITHER_SERVER_API_KEY") or "").strip()
                      else "stored for this twin" if name_ok and sh.stored_bearer(name)
                      else "minted on deploy, stored owner-only, never printed")

    checks = [
        _check("project", bool(project["has_agent"]),
               project["dir"] if project["has_agent"] else f"no agent.py in {project['dir']}",
               fix=["adk init <name>    # scaffold an agent, then run this in its directory"]),
        _check("name", name_ok, name or "no twin name",
               fix=["pass --name, or set identity in config.yaml"]),
        _check("model", bool(have_model), model_desc or "no model answers on this machine",
               fix=["adk setup    # install or point at a local model"]),
        _check("port", not port_err, port_detail,
               fix=["pass a free --port, or stop what is serving it"]),
        _check("reach", bool(reach["ok"]), reach["detail"], fix=reach["fix"]),
        _check("sign-in", bool(credential_source) or not registers,
               credential_source or ("not needed: nothing is registered" if not registers
                                     else "not signed in"),
               fix=["adk login"]),
        _check("callback bearer", True,
               bearer_src or "not needed: the agent listens on loopback only",
               required=False),
    ]
    request = None
    if registers:
        body: dict[str, Any] = {"name": name, "invoke_url": url, "provider": "self_hosted",
                                "reach": reach["reach"], "agent_type": "adk-agent",
                                "bind": getattr(args, "no_bind", False) is not True}
        if model:
            body["model"] = model
        body["token"] = PENDING_TOKEN
        request = {"method": "POST", "url": f"{base}/v1/agent{REGISTER_PATH}", "body": body}
    return {
        "twin": {"name": name, "kind": "self_hosted", "model": model or "(your agent's own)",
                 "serves_from": project["dir"], "local_port": port},
        "service": {
            "action": "keep (already running)" if is_running else "start",
            "command": " ".join(["adk", *sh.serve_argv(port, reach["bind_host"], model)[3:]]),
            "cwd": project["dir"], "bind": reach["bind_host"], "port": port, "model": model,
            "chat_url": f"http://127.0.0.1:{port}",
            "running": is_running, "pid": state.get("pid") if is_running else None,
        },
        "reach": {"mode": reach["reach"], "registered_with_platform": registers,
                  "public_url_needed": reach["reach"] == "tunnel", "invoke_url": url},
        "request": request,
        "cost": {
            "this_command_usd": 0,
            "provider_agents_created": 0,
            "model_spend": "none here: the twin answers on your own model and keys",
            "plan": ("no paid plan needed: a twin you host yourself is free, with no "
                     "count or turn limit"),
        },
        "checks": checks,
        "ready": _ready(checks),
    }


def _local_backend() -> tuple[bool, str]:
    """``(a model answers here, which one)`` -- the same probe `adk up` runs."""
    try:
        from adk.shell_launcher import _preflight_check

        ok, desc = _preflight_check()
        return bool(ok), str(desc or "")
    except Exception as exc:  # noqa: BLE001 - an unreadable probe is "no model found"
        return False, f"could not probe for a local model ({type(exc).__name__})"


def build_hosted_plan(*, base: str = "", credential_source: str = "") -> dict[str, Any]:
    """The plan for ``--hosted``: Aitherium hosts the agent bound to your account.

    Same shape as :func:`build_plan`. Nothing is created at a model provider: the
    twin answers on the platform's own model. The server decides the rest (the
    hosted-twin allowance, whether its twin server carries your agent) and says so.
    """
    checks = [{"check": "sign-in", "ok": bool(credential_source),
               "detail": credential_source or "not signed in: run `adk login`"}]
    return {
        "twin": {"name": "(the agent bound to your account)", "kind": "hosted",
                 "model": "Aitherium's own", "serves_from": "Aitherium's twin server",
                 "local_port": None},
        "request": {"method": "POST", "url": f"{base}/v1/agent{HOSTED_PATH}",
                    "body": {"host": "platform"}},
        "cost": {
            "this_command_usd": 0,
            "provider_agents_created": 0,
            "model_spend": "none: the twin answers on Aitherium's own model",
            "plan": ("your first hosted twin is free with a daily turn limit; each "
                     "extra hosted twin is paid; a twin you host yourself (drop "
                     "--hosted) is free with no limit"),
        },
        "checks": checks,
        "ready": bool(credential_source),
    }


def redacted(plan: dict[str, Any]) -> dict[str, Any]:
    """The plan safe to print: the callback bearer is never shown."""
    out = json.loads(json.dumps(plan))
    if ((out.get("request") or {}).get("body") or {}).get("token"):
        out["request"]["body"]["token"] = "<redacted>"
    return out


def _print_plan(plan: dict[str, Any], *, dry_run: bool) -> None:
    twin = plan["twin"]
    label = "Hosted twin" if twin.get("kind") == "hosted" else "Self-hosted twin"
    print(f"{label} '{twin['name'] or '?'}' ({twin['model']})")
    print(f"  from    {twin['serves_from']}")
    svc = plan.get("service")
    if svc:
        print(f"  service {svc['action']}: `{svc['command']}` on {svc['bind']}:{svc['port']} "
              f"(chat {svc['chat_url']})")
    if plan.get("request"):
        print(f"  request {plan['request']['method']} {plan['request']['url']}")
    else:
        print("  request none: reach=local registers nothing with the platform")
    for c in plan["checks"]:
        # A check that is not required is a note: it never reads as something to fix.
        mark = "ok" if c["ok"] else ("--" if c.get("required", True) else "i ")
        print(f"  [{mark}] {c['check']}: {c['detail']}")
    cost = plan["cost"]
    print(f"  cost    ${cost['this_command_usd']} here, {cost['provider_agents_created']} "
          f"provider agents created; {cost['model_spend']}")
    print(f"  plan    {cost['plan']}")
    if dry_run:
        print("dry run: nothing was started and nothing was sent."
              + (" The plan is ready." if plan["ready"] else ""))
    if not plan["ready"]:
        print("Not ready:")
        for line in missing(plan):
            print(f"  {line}")


def agent_base(env: Optional[dict] = None) -> str:
    """The base that serves Genesis ``/v1/agent``.

    ``adk.control_plane.genesis_api_base`` takes ``AITHER_API_URL`` as a direct Genesis
    base. Other parts of the kit read the same variable as the MCP gateway, and a
    machine set up that way (measured 2026-11-12: ``AITHER_API_URL=https://mcp.aitherium.com``)
    sent every twin verb to a host that has no ``/v1/agent`` and answered a bare
    ``404 Not Found``. An override that names the MCP gateway's own host is therefore
    not a Genesis base, and the portal's proxy is used. Any other override is used as
    given: it is never replaced by a different host.
    """
    from adk.control_plane import (GENESIS_PROXY_PREFIX, control_plane_url,
                                   genesis_api_base, genesis_override)

    env = os.environ if env is None else env
    override = genesis_override()
    if not override:
        return genesis_api_base()
    mcp_hosts = {"mcp.aitherium.com"}
    for var in ("AITHER_MCP_URL", "AITHER_MCP_GATEWAY_URL"):
        host = urlsplit((env.get(var) or "").strip()).hostname
        if host:
            mcp_hosts.add(host.lower())
    if (urlsplit(override).hostname or "").lower() in mcp_hosts:
        return control_plane_url() + GENESIS_PROXY_PREFIX
    return override


def run(args: Any) -> int:
    """Execute ``adk agent twin deploy|list|rebind|doors|remove``. Returns an exit code."""
    import httpx

    from adk.agent_binding_client import bearer

    sub = getattr(args, "twin_command", None)
    if sub not in TWIN_VERBS:
        print("usage: adk agent twin {deploy|list|rebind|doors|remove}", file=sys.stderr)
        return 1
    token, source = bearer()
    base = agent_base()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    as_json = bool(getattr(args, "json_output", False))

    if sub == "list":
        return _list(base, headers)

    if sub == "remove":
        return _remove(args, base, headers)

    if sub == "doors":
        return _doors(base, headers, as_json)

    if sub == "rebind":
        url = f"{base}/v1/agent{REBIND_PATH}"
        try:
            r = httpx.request("POST", url, json={"apply": bool(getattr(args, "apply", False))},
                              headers=headers, timeout=120.0)
        except Exception as exc:  # noqa: BLE001
            print(f"✗ control plane unreachable ({base}): {exc}", file=sys.stderr)
            return 1
        return _report(r, "POST", url)

    if getattr(args, "dedicated", False):
        return _dedicated(base, headers, as_json)

    self_host = bool(getattr(args, "self_host", False) is True)
    if getattr(args, "anthropic", False):
        plan = build_anthropic_plan(base=base, credential_source=source)
        self_host = False
    elif getattr(args, "hosted", False):
        plan = build_hosted_plan(base=base, credential_source=source)
    elif self_host:
        plan = build_selfhost_plan(args, base=base, credential_source=source)
    else:
        plan = build_plan(args, base=base, credential_source=source)
    dry = bool(getattr(args, "dry_run", False))
    if as_json:
        if dry or not plan.get("service") or not plan["ready"]:
            print(json.dumps({"dry_run": dry, **redacted(plan)}, indent=2))
    else:
        _print_plan(plan, dry_run=dry)
    if dry:
        return 0 if plan["ready"] else 1
    if not plan["ready"]:
        print("✗ not deployed: fix the lines under 'Not ready' (or see them with --dry-run)",
              file=sys.stderr)
        return 1
    if self_host and plan.get("service"):
        return _selfhost_deploy(plan, headers, as_json)
    req = plan["request"]
    try:
        r = httpx.request("POST", req["url"], json=req["body"], headers=headers, timeout=60.0)
    except Exception as exc:  # noqa: BLE001
        print(f"✗ control plane unreachable ({base}): {exc}", file=sys.stderr)
        return 1
    return _report(r, "POST", req["url"])


def _doors(base: str, headers: dict, as_json: bool) -> int:
    """Print the matrix the control plane serves. The kit keeps no copy of it, so what
    it prints is the table the gate itself reads."""
    import httpx

    url = f"{base}/v1/agent{DOORS_PATH}"
    try:
        r = httpx.request("GET", url, headers=headers, timeout=60.0)
    except Exception as exc:  # noqa: BLE001
        print(f"✗ control plane unreachable ({base}): {exc}", file=sys.stderr)
        return 1
    try:
        data: Any = r.json()
    except ValueError:
        data = {"raw": r.text[:400]}
    if r.status_code >= 400 or not isinstance(data, dict):
        print(f"✗ GET {url} -> {r.status_code}", file=sys.stderr)
        print(json.dumps(data, indent=2)[:1500], file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(data, indent=2))
        return 0
    if data.get("rule"):
        print(data["rule"])
    for i, door in enumerate(data.get("doors") or [], 1):
        print(f"\n{i}. {door.get('title')}  [{door.get('status')}]")
        print(f"   {door.get('what')}")
        print(f"   cost     {door.get('cost')}")
        print(f"   limit    {door.get('limit')}")
        print(f"   command  {door.get('command')}")
        if door.get("status_note"):
            print(f"   note     {door['status_note']}")
        for part in door.get("parts") or []:
            print(f"   - {part.get('part')}  [{part.get('status')}]: {part.get('command')}")
    return 0


def _selfhost_deploy(plan: dict[str, Any], headers: dict, as_json: bool) -> int:
    """Start the service (unless it already runs), then register it. A registration
    that fails stops what this call started, so a failed deploy leaves nothing behind."""
    import time

    import httpx

    from adk import twin_selfhost as sh

    t0 = time.time()
    name, svc, reach = plan["twin"]["name"], plan["service"], plan["reach"]
    req = plan["request"]
    token = ""
    if req:
        token, _where = sh.ensure_bearer(name)
    started_here = False
    if svc["running"]:
        state = sh.read_state(name) or {}
        if state.get("host") != svc["bind"]:
            print(f"✗ '{name}' already runs bound to {state.get('host')}, and this reach "
                  f"needs {svc['bind']}: `adk agent twin remove {name}`, then deploy again",
                  file=sys.stderr)
            return 1
    else:
        res = sh.start(name=name, project=svc["cwd"], port=svc["port"], host=svc["bind"],
                       model=svc.get("model") or "", bearer=token)
        if not res["ok"]:
            print(f"✗ not started: {res['error']}", file=sys.stderr)
            return 1
        started_here = True
        state = {"name": name, "pid": res["pid"], "port": svc["port"], "host": svc["bind"],
                 "project": svc["cwd"], "model": svc.get("model") or "", "log": res["log"],
                 "started_at": res.get("started_at"), "registered": False}
    state.update(reach=reach["mode"], invoke_url=reach["invoke_url"])
    sh.write_state(state)

    endpoint: Any = None
    if req:
        body = dict(req["body"])
        body["token"] = token
        error = ""
        try:
            r = httpx.request("POST", req["url"], json=body, headers=headers, timeout=60.0)
            try:
                data: Any = r.json()
            except ValueError:
                data = {"raw": r.text[:400]}
            if r.status_code >= 400 or (isinstance(data, dict) and data.get("ok") is False):
                error = f"POST {req['url']} -> {r.status_code}\n" + json.dumps(data, indent=2)[:1500]
            else:
                endpoint = data.get("endpoint") if isinstance(data, dict) else None
        except Exception as exc:  # noqa: BLE001 - the transport error IS the answer
            error = f"control plane unreachable ({req['url']}): {exc}"
        if error:
            print(f"✗ not registered: {error}", file=sys.stderr)
            if started_here:
                sh.stop(name)
                sh.clear_state(name)
                print("  the service this command started was stopped again. To run it "
                      "without registering: add --reach local", file=sys.stderr)
            return 1
        state["registered"] = True
        sh.write_state(state)

    seconds = round(time.time() - t0, 1)
    view = sh.local_view(state)
    if as_json:
        print(json.dumps({"ok": True, "twin": name, "service": view, "reach": reach,
                          "endpoint": endpoint, "seconds": seconds}, indent=2))
        return 0
    how = "kept running" if not started_here else "started"
    print(f"running    {name}: {how}, pid {state['pid']} on {state['host']}:{state['port']} "
          f"(log {state.get('log')})")
    if req:
        print(f"registered {reach['invoke_url']} reach={reach['mode']} "
              "(see it: adk agent twin list)")
    else:
        print("registered no: reach=local keeps it on this machine "
              "(listed by: adk agent twin list)")
    print(f"chat       POST {view['chat_url']}/chat  {{\"message\": \"...\"}}")
    print(f"stop       adk agent twin remove {name}")
    print(f"done in {seconds}s, $0")
    return 0


def _list(base: str, headers: dict) -> int:
    """Platform endpoints plus this machine's own twin records (``local``)."""
    import httpx

    from adk import twin_selfhost as sh

    local = [sh.local_view(st) for st in sh.all_states()]
    url = f"{base}/v1/agent{REGISTER_PATH}"
    if not headers and local:
        # Signed out, and there are twins running here: show them, say what is not shown.
        print(json.dumps({"endpoints": None,
                          "platform": "not signed in, so registered twins are not listed: "
                                      "run `adk login`",
                          "local": local}, indent=2))
        return 0
    try:
        r = httpx.request("GET", url, headers=headers, timeout=60.0)
    except Exception as exc:  # noqa: BLE001 - the transport error IS the answer
        print(f"✗ control plane unreachable ({base}): {exc}", file=sys.stderr)
        return 1
    if not local:
        return _report(r, "GET", url)
    try:
        data: Any = r.json()
    except ValueError:
        data = None
    if r.status_code >= 400 or not isinstance(data, dict):
        code = _report(r, "GET", url)
        print(json.dumps({"local": local}, indent=2))
        return code
    data["local"] = local
    print(json.dumps(data, indent=2))
    return 0


def _remove(args: Any, base: str, headers: dict) -> int:
    """Stop the twin's local service and remove its registration. Reports each half;
    exits 0 when something was removed and nothing failed."""
    import httpx

    from adk import twin_selfhost as sh

    name = (getattr(args, "name", "") or "").strip()
    if not _NAME_RE.match(name):
        print("✗ a twin name is letters, digits, _ or -", file=sys.stderr)
        return 1
    state = sh.read_state(name)
    out: dict[str, Any] = {"twin": name}
    out["service"] = sh.stop(name)
    failed = False
    if state is not None and not state.get("registered"):
        out["registration"] = "none (it was never registered)"
    else:
        url = f"{base}/v1/agent{REGISTER_PATH}/{name}"
        try:
            r = httpx.request("DELETE", url, headers=headers, timeout=60.0)
            if r.status_code == 404:
                out["registration"] = "none on the platform"
            elif r.status_code >= 400:
                failed = True
                out["registration"] = f"not removed: DELETE {url} -> {r.status_code}"
            else:
                out["registration"] = "removed"
        except Exception as exc:  # noqa: BLE001
            failed = True
            out["registration"] = f"not removed: control plane unreachable ({exc})"
    if not failed:
        sh.clear_state(name)
    found = out["service"]["known"] or out["registration"] == "removed"
    out["ok"] = found and not failed
    print(json.dumps(out, indent=2))
    if not found and not failed:
        print(f"✗ no twin named '{name}' on this machine or on the platform", file=sys.stderr)
    return 0 if out["ok"] else 1


def _dedicated(base: str, headers: dict, as_json: bool) -> int:
    """``--dedicated``: the plan and the quoted hourly rental. READ-ONLY, always.

    One GET to the quote route. This command never rents a GPU, with or without
    ``--dry-run``: renting is a separate, explicit step in your own provider account,
    and the steps to do it are printed.
    """
    import httpx

    url = f"{base}/v1/agent{QUOTE_PATH}"
    try:
        r = httpx.request("GET", url, headers=headers, timeout=60.0)
    except Exception as exc:  # noqa: BLE001
        print(f"✗ control plane unreachable ({base}): {exc}", file=sys.stderr)
        return 1
    try:
        data: Any = r.json()
    except ValueError:
        data = {"raw": r.text[:400]}
    if r.status_code >= 400 or not isinstance(data, dict):
        print(f"✗ GET {url} -> {r.status_code}", file=sys.stderr)
        print(json.dumps(data, indent=2)[:1500], file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps({"dry_run": True, **data}, indent=2))
        return 0
    quote = data.get("quote") or {}
    print(f"Dedicated compute for your twin ({quote.get('model')}, "
          f"{quote.get('min_vram_gb')} GB or more)")
    print(f"  cost    {quote.get('price')}")
    print(f"  billed  {quote.get('billing')}")
    if quote.get("no_live_quote_because"):
        print(f"  quote   no live quote: {quote['no_live_quote_because']}")
    for i, step in enumerate(data.get("steps") or [], 1):
        print(f"  {i}. {step}")
    if data.get("note"):
        print(f"  note    {data['note']}")
    print("nothing was rented: this command only quotes.")
    return 0


def _report(r: Any, method: str, url: str) -> int:
    try:
        data: Any = r.json()
    except ValueError:
        data = {"raw": r.text[:400]}
    if r.status_code >= 400:
        print(f"✗ {method} {url} -> {r.status_code}", file=sys.stderr)
        print(json.dumps(data, indent=2)[:1500], file=sys.stderr)
        return 1
    print(json.dumps(data, indent=2))
    return 1 if isinstance(data, dict) and data.get("ok") is False else 0


__all__ = ["TWIN_VERBS", "REGISTER_PATH", "HOSTED_PATH", "REBIND_PATH", "QUOTE_PATH",
           "DOORS_PATH", "build_anthropic_plan",
           "REQUIRED_FEATURE", "PENDING_TOKEN", "agent_base", "missing",
           "add_parser", "read_project", "build_plan", "build_selfhost_plan",
           "build_hosted_plan", "redacted", "run"]
