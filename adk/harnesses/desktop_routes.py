"""``/desk/tools`` on the harness daemon: the Windows desktop tools, for the fleet.

The MCP gateway is a Linux container; it cannot list a window, read a service or ask
File Locksmith anything. This daemon runs on the Windows host, so the gateway's
``mcp_desktop`` proxy calls these routes (``host.docker.internal:8362``) with the same
``/desk``-scoped bearer it already holds for the ``/desk/*`` awdesk proxy.

* ``GET  /desk/tools``         -- every tool: name, action class, description, schema.
* ``POST /desk/tools/{name}``  -- body ``{"args": {...}, "confirm": false}``.

The allowlist IS :data:`adk.desktop.tools.DESKTOP_TOOLS`: a name outside it is a 404,
and a tool added there needs no change here. A tool whose action class is ``write``
runs live only when the effective ``dry_run`` is False AND ``confirm`` is exactly
``true``; a dry run of a write tool needs no confirm. Arguments are bound against the
tool's signature and type-checked against its schema before anything runs. Auth is
the daemon's own ``auth`` dependency (bearer + path scope), nothing new.
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("adk.harnesses.desktop_routes")

__all__ = ["mount", "invoke", "list_tools", "WRITE_CLASS"]

WRITE_CLASS = "write"
ROUTE_PREFIX = "/desk/tools"

Table = Dict[str, Tuple[Callable[..., str], str]]

_JSON_TYPES: Dict[str, Tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,),
    "array": (list,), "object": (dict,),
}


def _table() -> Table:
    from adk.desktop.tools import desktop_tool_table

    return desktop_tool_table()


def _schema(fn: Callable[..., Any]) -> Dict[str, Any]:
    from adk.tools import _extract_parameters

    return _extract_parameters(fn)


def list_tools(table: Optional[Table] = None) -> Dict[str, Any]:
    """Every desktop tool the route will run, with its class and parameter schema."""
    rows = []
    for name, (fn, cls) in sorted((table or _table()).items()):
        doc = (fn.__doc__ or "").strip()
        rows.append({"name": name, "action_class": cls or "read",
                     "description": doc.split("\n\n", 1)[0].replace("\n", " "),
                     "parameters": _schema(fn)})
    return {"ok": True, "tools": rows}


def _type_error(name: str, value: Any, spec: Dict[str, Any]) -> str:
    want = spec.get("type")
    kinds = _JSON_TYPES.get(str(want))
    if not kinds:
        return ""
    if want in ("integer", "number") and isinstance(value, bool):
        return f"{name} must be {want}, got boolean"
    if not isinstance(value, kinds):
        return f"{name} must be {want}, got {type(value).__name__}"
    if want == "array" and spec.get("items", {}).get("type") == "string":
        if not all(isinstance(v, str) for v in value):
            return f"{name} must be an array of strings"
    return ""


def invoke(name: str, body: Any, table: Optional[Table] = None,
           principal_id: str = "") -> Tuple[int, Dict[str, Any]]:
    """Validate and run one desktop tool. Returns ``(http_status, payload)``.

    Never raises for a caller mistake: 404 unknown tool, 400 bad arguments, 403 a
    write that would act without ``confirm: true``, 500 a tool that crashed.
    """
    tools = table if table is not None else _table()
    hit = tools.get(name)
    if hit is None:
        return 404, {"ok": False, "reason": "unknown_tool",
                     "error": f"no desktop tool {name!r}; known: {sorted(tools)}"}
    fn, cls = hit
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return 400, {"ok": False, "reason": "bad_args", "error": "body must be an object"}
    args = body.get("args", {})
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return 400, {"ok": False, "reason": "bad_args", "error": "args must be an object"}
    confirm = body.get("confirm") is True
    sig = inspect.signature(fn)
    try:
        bound = sig.bind(**args)
    except TypeError as exc:
        return 400, {"ok": False, "reason": "bad_args", "error": f"{name}: {exc}"}
    props = _schema(fn).get("properties", {})
    for key, value in args.items():
        err = _type_error(key, value, props.get(key, {}))
        if err:
            return 400, {"ok": False, "reason": "bad_args", "error": f"{name}: {err}"}
    live = True
    if cls == WRITE_CLASS:
        if "dry_run" in sig.parameters:
            default = sig.parameters["dry_run"].default
            live = bound.arguments.get("dry_run", default) is False
        if live and not confirm:
            return 403, {"ok": False, "reason": "confirm_required",
                         "error": f"{name} is a write tool: with dry_run=false it changes "
                                  "this machine, so the call must also send "
                                  "confirm=true (or run it with dry_run=true first)"}
    logger.info("desktop tool %s by %s (class=%s live=%s confirm=%s)", name,
                principal_id or "?", cls or "read", live, confirm)
    try:
        raw = fn(**bound.arguments)
    except Exception as exc:  # noqa: BLE001 -- a crashed tool is a 500 answer, not a dead route
        logger.exception("desktop tool %s crashed", name)
        return 500, {"ok": False, "reason": "tool_error", "error": f"{type(exc).__name__}: "
                                                                  f"{exc}"}
    try:
        result = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        result = {"ok": False, "error": "tool returned non-JSON", "raw": str(raw)[:2000]}
    ok = bool(result.get("ok")) if isinstance(result, dict) else True
    return 200, {"ok": ok, "tool": name, "action_class": cls or "read",
                 "confirmed": confirm, "result": result}


def mount(app: Any, auth: Any) -> None:
    """Add the routes to the daemon's FastAPI ``app`` (``auth`` = its bearer dependency)."""
    from fastapi import Body, Depends
    from fastapi.responses import JSONResponse

    @app.get(ROUTE_PREFIX)
    def desktop_tools_list(principal=Depends(auth)) -> Dict[str, Any]:
        return list_tools()

    @app.post(ROUTE_PREFIX + "/{name}")
    def desktop_tools_invoke(name: str, body: Any = Body(default=None),
                             principal=Depends(auth)) -> Any:
        status, payload = invoke(name, body, principal_id=getattr(principal, "id", ""))
        return JSONResponse(status_code=status, content=payload)
