"""Agent Notebook tools — plan, run, and inspect ``.anb`` notebooks from an ADK agent.

An **Agent Notebook** is AitherOS's executable, reviewable unit of agent work: an
ordered list of typed cells (``context`` / ``plan`` / ``prompt`` / ``tool_call`` /
``agent_delegate`` / ``checkpoint`` / ``result`` …) that runs on Genesis, records a
cost-tracked run per execution, and can be replayed, diffed, reviewed, and exported to
a Jupyter ``.ipynb``. It is the durable counterpart to a one-shot ``adk forge`` dispatch.

These tools are thin, fail-soft proxies onto the Genesis ``/notebooks/*`` router (the
same surface the portal notebook UI drives). Every call returns a JSON string so the
agent's ReAct loop can read the result directly; transport/HTTP errors are surfaced as
``{"error": ...}`` rather than raised.

Registered as the ``notebooks`` tool category (see ``builtin_tools.py``).
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

# Kept in sync with builtin_tools._STRUCTURED_ML_MAX_RESP_CHARS — notebook run traces
# and definitions can be large, so we summarise rather than flood the model's context.
_MAX_RESP_CHARS = 8000


def _genesis_url() -> str:
    """Resolve the Genesis base URL.

    An explicit DIRECT base (``AITHER_GENESIS_URL`` / ``AITHER_API_URL``, e.g.
    ``https://aitheros-genesis:8001`` in-network) wins. Otherwise the control
    plane's Genesis bridge. The old default, ``http://localhost:8001``, refused on
    every host -- Genesis publishes no host port and speaks TLS -- so every
    notebook call from a desktop read as "Genesis unreachable" (measured
    2026-10-28: ``<control plane>/api/bridge/genesis/notebooks/<id>`` -> 200).
    """
    from adk.control_plane import genesis_data_base, genesis_override

    url = (genesis_override() or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return genesis_data_base().rstrip("/")
    return url.rstrip("/")


def _via_bridge() -> bool:
    """True when calls go through the control plane's bridge, not a direct base."""
    from adk.control_plane import genesis_override

    return not (genesis_override() or "").lower().startswith(("http://", "https://"))


def _tls_verify() -> bool:
    """Trust the internal CA; only disable when explicitly asked (never verify=False by default)."""
    return os.getenv("AITHER_TLS_VERIFY", "true").lower() != "false"


def _auth_headers() -> dict:
    """Carry the logged-in ADK session so write ops run AS the authenticated user.

    Notebook create/plan/execute are fail-closed RBAC-gated (require ``can_execute``);
    without the caller's bearer they 403. The token is the ``adk login`` session
    backfilled from ``~/.aither/auth.json`` (env ``AITHER_API_KEY`` wins if set).
    Fail-soft: no token → no header (reads still work).
    """
    token = os.getenv("AITHER_API_KEY", "").strip()
    if not token:
        try:
            from adk.config import load_saved_config
            cfg = load_saved_config()
            token = (cfg.get("access_token") or cfg.get("api_key") or "").strip()
        except Exception:  # noqa: BLE001 — auth is best-effort; reads work unauthenticated
            token = ""
    return {"Authorization": f"Bearer {token}"} if token else {}


def _cap(data: Any) -> str:
    """Serialise a response, summarising oversized notebook/run payloads."""
    full = json.dumps(data, default=str)
    if len(full) <= _MAX_RESP_CHARS:
        return full
    summary: dict = {
        "truncated": True,
        "note": (
            "Notebook payload too large to return in full. Use notebook_get / "
            "notebook_run_status with a specific id, or notebook_export to save it."
        ),
    }
    if isinstance(data, dict):
        summary["keys"] = list(data.keys())
        for k in ("notebook_id", "run_id", "status", "total", "state"):
            if k in data:
                summary[k] = data[k]
        nbs = data.get("notebooks")
        if isinstance(nbs, list):
            summary["notebook_count"] = len(nbs)
            summary["notebooks_sample"] = [
                {"id": n.get("id"), "name": n.get("name"), "status": n.get("status")}
                for n in nbs[:25]
                if isinstance(n, dict)
            ]
    return json.dumps(summary, default=str)


# Upstream states that mean "genesis wasn't ready" (mid-restart / cold worker /
# standby not serving) rather than "your request was bad" — nginx returns these
# from proxy_connect / upstream-unavailable, NOT from a slow-but-alive request
# (the genesis-lb read timeout is 3600s). They clear once genesis warms, so we
# retry instead of surfacing a spurious failure.
_TRANSIENT_STATUS = {502, 503, 504}
_MAX_ATTEMPTS = 4
_BACKOFF_S = (1.5, 4.0, 8.0)  # between attempts 1→2, 2→3, 3→4


def _request(method: str, path: str, *, json_body: Optional[dict] = None,
             params: Optional[dict] = None, timeout: float = 120.0,
             base: Optional[str] = None) -> Any:
    """Make a Genesis request, retrying transient upstream errors.

    Returns parsed JSON, or an ``{"error": ...}`` dict once retries are exhausted.
    Retries on connect/transport errors and 502/503/504 (genesis mid-restart or a
    cold worker) with bounded backoff; a 4xx (auth/validation/not-found) is returned
    immediately — retrying it would never help. ``base`` replaces the Genesis base
    for the one route that must go through the portal (see ``notebook_list``).
    """
    import time

    import httpx

    url = f"{base or _genesis_url()}{path}"
    last_err = "unknown error"
    for attempt in range(_MAX_ATTEMPTS):
        try:
            with httpx.Client(timeout=timeout, verify=_tls_verify()) as c:
                resp = c.request(method, url, json=json_body, params=params,
                                 headers=_auth_headers())
                if resp.status_code in _TRANSIENT_STATUS:
                    last_err = f"HTTP {resp.status_code}: genesis not ready (transient)"
                    # fall through to backoff+retry
                elif resp.status_code >= 400:
                    try:
                        detail = resp.json().get("detail", resp.text)
                    except Exception:  # noqa: BLE001
                        detail = resp.text
                    return {"error": f"HTTP {resp.status_code}: {detail}"}
                else:
                    if not resp.content:
                        return {"success": True}
                    try:
                        return resp.json()
                    except Exception:  # noqa: BLE001 — non-JSON (e.g. .ipynb export)
                        return {"raw": resp.text}
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                httpx.ReadError, httpx.RemoteProtocolError, httpx.PoolTimeout) as e:
            last_err = f"{type(e).__name__}: {e}"
        except Exception as e:  # noqa: BLE001 — non-retryable transport/other error
            return {"error": f"Genesis unreachable at {base or _genesis_url()}: {e}"}

        if attempt < _MAX_ATTEMPTS - 1:
            time.sleep(_BACKOFF_S[attempt])

    return {"error": f"Genesis unavailable after {_MAX_ATTEMPTS} attempts "
                     f"(genesis restarting/saturated): {last_err}"}


# ─────────────────────────────────────────────────────────────────────────────
# Agent-callable tools
# ─────────────────────────────────────────────────────────────────────────────


def notebook_plan(prompt: str, agent: str = "atlas", effort: int = 5,
                  context: str = "") -> str:
    """Create an Agent Notebook from a natural-language task description.

    An LLM decomposes the prompt into structured cells (context, plan, tool_call,
    prompt, checkpoint, result) and persists a runnable ``.anb`` notebook. This is
    how you turn "build X" into a reviewable, re-runnable plan instead of a one-shot
    dispatch. The returned ``notebook`` has an ``id`` you pass to the other tools.

    Args:
        prompt: The task to plan a notebook for (e.g. "audit our auth flow for fail-open gates").
        agent: Planning agent persona (default "atlas"; e.g. demiurge, hydra, athena).
        effort: Planner effort 1-10 (default 5). Higher = more reasoning on decomposition.
        context: Optional extra context to ground the plan.
    """
    body: dict = {"prompt": prompt, "agent": agent, "effort": int(effort)}
    if context:
        body["context"] = context
    return _cap(_request("POST", "/notebooks/plan", json_body=body))


def notebook_list(workspace: str = "", status: str = "", limit: int = 50) -> str:
    """List Agent Notebooks, optionally filtered by workspace or status.

    Args:
        workspace: Filter to a workspace (optional).
        status: Filter by status, e.g. draft / ready / running / completed (optional).
        limit: Max notebooks to return (default 50).
    """
    params: dict = {"limit": int(limit)}
    if workspace:
        params["workspace"] = workspace
    if status:
        params["status"] = status
    if not _via_bridge():
        return _cap(_request("GET", "/notebooks/", params=params))
    # Through the control plane the list cannot use the bridge: it rebuilds the path
    # WITHOUT its trailing slash, Genesis answers `/notebooks` with a 307 to
    # `/notebooks/`, and the bridge passes the 307 on with no Location (measured
    # 2026-10-28: 307 `{"raw": ""}`). The portal's own list route -- the one
    # AitherOne renders -- answers instead. It forwards status/tag only, so the
    # limit is applied here, and it reports a Genesis failure behind it as an EMPTY
    # list: callers that must tell "none" from "down" verify with notebook_get.
    from adk.control_plane import control_plane_url

    data = _request("GET", "/api/notebooks", params=params, base=control_plane_url())
    if isinstance(data, dict) and not data.get("error") and isinstance(
            data.get("notebooks"), list):
        rows = data["notebooks"]
        data = {"notebooks": rows[:int(limit)], "total": len(rows), "via": "portal"}
    return _cap(data)


def notebook_outline(notebook_id: str) -> str:
    """A notebook's cells as a compact outline (id, type, name, tool, first words).

    ``notebook_get`` returns the whole definition, which for a recorded session is
    tens of KB and so arrives summarised without its cells. This is the readable
    view: what the notebook is and what a replay would run.

    Args:
        notebook_id: The notebook id.
    """
    if not notebook_id:
        return json.dumps({"error": "notebook_id is required"})
    data = _request("GET", f"/notebooks/{notebook_id}")
    if not isinstance(data, dict) or data.get("error"):
        return json.dumps(data if isinstance(data, dict) else {"error": "no notebook"})
    cells = data.get("cells") or (data.get("spec") or {}).get("cells") or []
    outline = []
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        cfg = cell.get("config") or {}
        text = cfg.get("text") or cfg.get("prompt") or cfg.get("task") or ""
        if not text and (cfg.get("input") or cfg.get("arguments")):
            text = json.dumps(cfg.get("input") or cfg.get("arguments"), default=str)
        outline.append({"id": cell.get("id"), "type": cell.get("type"),
                        "name": cell.get("name"),
                        "tool": cfg.get("tool") or cfg.get("tool_name") or "",
                        "text": str(text)[:100]})
    keep = ("id", "name", "description", "status", "created_at", "created_by",
            "workspace", "baseline_run_id")
    out = {k: data.get(k) for k in keep if k in data}
    # Bounded by construction (80 cells x ~200 chars), so it is never _cap'd away.
    out.update(cell_count=len(outline), cells=outline[:80],
               cells_not_shown=max(0, len(outline) - 80))
    return json.dumps(out, default=str)


def notebook_get(notebook_id: str) -> str:
    """Get an Agent Notebook definition (its cells, spec, variables, and status).

    Args:
        notebook_id: The notebook id (from notebook_plan / notebook_list).
    """
    if not notebook_id:
        return json.dumps({"error": "notebook_id is required"})
    return _cap(_request("GET", f"/notebooks/{notebook_id}"))


def notebook_execute(notebook_id: str, variables: Optional[dict] = None,
                     mode: str = "sequential") -> str:
    """Execute an Agent Notebook and return its run handle (run_id + status).

    Cells run on Genesis with cost tracking. Execution may pause at a ``checkpoint``
    cell awaiting approval — poll notebook_run_status to see progress and resolve
    gates from the portal. Pass variables to fill the notebook's templated inputs.

    Args:
        notebook_id: The notebook to run.
        variables: Optional variable overrides for this run (dict).
        mode: "sequential" (default) or "parallel" where the graph allows it.
    """
    if not notebook_id:
        return json.dumps({"error": "notebook_id is required"})
    body = {"variables": variables or {}, "mode": mode}
    return _cap(_request("POST", f"/notebooks/{notebook_id}/execute", json_body=body,
                         timeout=300.0))


def notebook_run_status(run_id: str) -> str:
    """Get a notebook run's status, per-cell traces, and cost summary.

    Args:
        run_id: The run id returned by notebook_execute.
    """
    if not run_id:
        return json.dumps({"error": "run_id is required"})
    return _cap(_request("GET", f"/notebooks/runs/{run_id}"))


def notebook_export(notebook_id: str, path: str = "") -> str:
    """Export an Agent Notebook to a Jupyter ``.ipynb`` file on disk.

    Open the result in VS Code, JupyterLab, or any Jupyter viewer.

    Args:
        notebook_id: The notebook to export.
        path: Where to write the .ipynb (default "./<notebook_id>.ipynb").
    """
    if not notebook_id:
        return json.dumps({"error": "notebook_id is required"})

    # Reuse the resilient JSON path — the export route returns .ipynb JSON, which
    # _request surfaces as parsed JSON (or {"raw": ...} on the off chance it isn't).
    result = _request("GET", f"/notebooks/{notebook_id}/export")
    if isinstance(result, dict) and result.get("error"):
        return json.dumps(result)

    text = result if isinstance(result, str) else json.dumps(result)
    out_path = path or f"./{notebook_id}.ipynb"
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"Export write failed: {e}"})
    return json.dumps({"success": True, "path": os.path.abspath(out_path),
                       "bytes": len(text)})


# Exported for builtin_tools category registration.
NOTEBOOK_TOOLS = [
    notebook_plan,
    notebook_list,
    notebook_get,
    notebook_execute,
    notebook_run_status,
    notebook_export,
]
