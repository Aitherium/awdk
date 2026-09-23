"""`adk agent bind|swap-brain|backend|managed ...` — thin client of Genesis /v1/agent.

Genesis ``routers/agent_binding.py`` (``APIRouter(prefix="/v1/agent")``) serves the
binding and managed-agent routes the portal uses; until this module the CLI had no
caller for any of them, so a CLI-only customer could not connect the Anthropic key
that ``managed/deploy`` requires.

Every verb prints what the gateway actually answered: a 4xx/5xx prints its body and
exits 1. The BYOK key is never taken as a positional argument (it would land in shell
history and process listings): it is read from ``--from-env VAR`` or a hidden prompt.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable, Optional

# verb -> (method, path under /v1/agent)
ROUTES: dict[str, tuple[str, str]] = {
    "bind": ("POST", "/binding/apply-pack"),
    "swap-brain": ("POST", "/binding/swap-brain"),
    "backend": ("POST", "/binding/backend"),
    "managed.status": ("GET", "/managed/status"),
    "managed.chat": ("POST", "/managed/chat"),
    "managed.run": ("POST", "/managed/run"),
    "managed.resync": ("POST", "/managed/resync"),
    "managed.byok": ("POST", "/managed/byok"),
}

BINDING_VERBS = ("bind", "swap-brain", "backend", "managed")
MANAGED_VERBS = ("status", "chat", "run", "resync", "byok")


def add_parsers(agent_sub: Any) -> None:
    """Register the binding/managed verbs under ``adk agent``."""
    bind = agent_sub.add_parser("bind", help="Apply a pack (listing or license) to your agent binding")
    bind.add_argument("listing_id", nargs="?", default="", help="Pack/listing id to apply")
    bind.add_argument("--license-key", dest="license_key", default="",
                      help="Apply by license instead of listing id")
    bind.add_argument("--agent-id", dest="agent_id", default="")
    bind.add_argument("--json", dest="json_output", action="store_true")

    swap = agent_sub.add_parser("swap-brain", help="Swap the agent's brain pack")
    swap.add_argument("brain_company", help="Brain company")
    swap.add_argument("brain_pack", help="Brain pack")
    swap.add_argument("--agent-id", dest="agent_id", default="")
    swap.add_argument("--json", dest="json_output", action="store_true")

    backend = agent_sub.add_parser("backend", help="Set the agent's backend (local | managed | ...)")
    backend.add_argument("backend", help="Backend name")
    backend.add_argument("--agent-id", dest="agent_id", default="")
    backend.add_argument("--json", dest="json_output", action="store_true")

    managed = agent_sub.add_parser("managed", help="Managed (hosted twin) agent: status|chat|run|resync|byok")
    msub = managed.add_subparsers(dest="managed_command")
    st = msub.add_parser("status", help="Deployment state of your managed agent")
    st.add_argument("--agent-id", dest="agent_id", default="")
    st.add_argument("--json", dest="json_output", action="store_true")
    ch = msub.add_parser("chat", help="Send one chat turn to your managed agent")
    ch.add_argument("message", help="Message text")
    ch.add_argument("--agent-id", dest="agent_id", default="")
    ch.add_argument("--conversation-key", dest="conversation_key", default="")
    ch.add_argument("--json", dest="json_output", action="store_true")
    rn = msub.add_parser("run", help="Run one job on a platform managed twin (internal-key gated)")
    rn.add_argument("agent", help="Platform agent name")
    rn.add_argument("task", help="Task text")
    rn.add_argument("--conversation-key", dest="conversation_key", default="")
    rn.add_argument("--json", dest="json_output", action="store_true")
    rs = msub.add_parser("resync", help="Recompile packs and update the hosted twin")
    rs.add_argument("--agent-id", dest="agent_id", default="")
    rs.add_argument("--model", default="")
    rs.add_argument("--system-prompt", dest="system_prompt", default="")
    rs.add_argument("--json", dest="json_output", action="store_true")
    by = msub.add_parser("byok", help="Store your Anthropic API key (read from --from-env or a hidden prompt)")
    by.add_argument("--from-env", dest="from_env", default="",
                    help="Name of an environment variable holding the key")
    by.add_argument("--environment-id", dest="environment_id", default="")
    by.add_argument("--json", dest="json_output", action="store_true")


def _drop_empty(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if v not in ("", None)}


def _read_byok_key(args: Any, prompt: Callable[[str], str]) -> str:
    env_name = (getattr(args, "from_env", "") or "").strip()
    if env_name:
        return (os.environ.get(env_name) or "").strip()
    if not sys.stdin.isatty():
        return (sys.stdin.readline() or "").strip()
    return (prompt("Anthropic API key: ") or "").strip()


def build_request(args: Any, prompt: Optional[Callable[[str], str]] = None
                  ) -> tuple[str, str, Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Map parsed args to ``(method, path, json_body, query_params)``. Raises ValueError
    on a usage error (the message is printed by :func:`run`)."""
    verb = getattr(args, "agent_command", "")
    if verb == "managed":
        sub = getattr(args, "managed_command", None)
        if sub not in MANAGED_VERBS:
            raise ValueError("usage: adk agent managed {status|chat|run|resync|byok}")
        key = f"managed.{sub}"
    else:
        key = verb
    if key not in ROUTES:
        raise ValueError(f"unknown agent binding verb: {verb}")
    method, path = ROUTES[key]
    agent_id = getattr(args, "agent_id", "") or None

    if key == "bind":
        if not (args.listing_id or args.license_key):
            raise ValueError("usage: adk agent bind <listing_id> | --license-key KEY")
        return method, path, _drop_empty({"listing_id": args.listing_id,
                                          "license_key": args.license_key,
                                          "agent_id": agent_id}), None
    if key == "swap-brain":
        return method, path, _drop_empty({"brain_company": args.brain_company,
                                          "brain_pack": args.brain_pack,
                                          "agent_id": agent_id}), None
    if key == "backend":
        return method, path, _drop_empty({"backend": args.backend, "agent_id": agent_id}), None
    if key == "managed.status":
        return method, path, None, _drop_empty({"agent_id": agent_id}) or None
    if key == "managed.chat":
        return method, path, _drop_empty({"message": args.message, "agent_id": agent_id,
                                          "conversation_key": args.conversation_key}), None
    if key == "managed.run":
        return method, path, _drop_empty({"agent": args.agent, "task": args.task,
                                          "conversation_key": args.conversation_key}), None
    if key == "managed.resync":
        return method, path, _drop_empty({"agent_id": agent_id, "model": args.model,
                                          "system_prompt": args.system_prompt}), None
    # managed.byok
    if prompt is None:
        import getpass
        prompt = getpass.getpass
    api_key = _read_byok_key(args, prompt)
    if not api_key:
        raise ValueError("no Anthropic key supplied (use --from-env VAR or the prompt)")
    return method, path, _drop_empty({"anthropic_api_key": api_key,
                                      "environment_id": args.environment_id}), None


def run(args: Any, prompt: Optional[Callable[[str], str]] = None) -> int:
    """Execute one binding/managed verb against the gateway. Returns an exit code."""
    import httpx

    from adk.fleet_manager import _api_key, _gateway_base

    try:
        method, path, body, params = build_request(args, prompt)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    url = f"{_gateway_base()}/v1/agent{path}"
    headers = {"Authorization": f"Bearer {_api_key()}"} if _api_key() else {}
    try:
        r = httpx.request(method, url, json=body, params=params, headers=headers,
                          timeout=180.0 if "chat" in path or "run" in path else 60.0)
    except Exception as exc:  # noqa: BLE001 - the transport error IS the answer
        print(f"✗ gateway unreachable ({_gateway_base()}): {exc}", file=sys.stderr)
        return 1
    try:
        data: Any = r.json()
    except ValueError:
        data = {"raw": r.text[:400]}
    if r.status_code >= 400:
        print(f"✗ {method} {url} -> {r.status_code}", file=sys.stderr)
        print(json.dumps(data, indent=2)[:1500], file=sys.stderr)
        return 1
    if getattr(args, "json_output", False) or not isinstance(data, dict):
        print(json.dumps(data, indent=2))
    elif path == "/managed/chat" and "content" in data:
        print(data.get("content") or "")
        if data.get("error"):
            print(f"error: {data['error']}", file=sys.stderr)
    else:
        print(json.dumps(data, indent=2))
    if isinstance(data, dict) and data.get("ok") is False:
        return 1
    return 0


__all__ = ["ROUTES", "BINDING_VERBS", "MANAGED_VERBS", "add_parsers", "build_request", "run"]
