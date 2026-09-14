"""Admin-console bridge to the AitherOS ISO Factory.

The ISO Factory lives in the platform repository, OUTSIDE the adk
wheel — so ``admin_api`` cannot import it. This module exposes bearer-gated
``/admin/factory/*`` routes that shell out to the factory CLI (``python -m
factory.cli … --json``) and relay the JSON, crossing the packaging boundary
cleanly. It is only useful where the factory is present (the monorepo / a
sovereign appliance build host); elsewhere the routes report it unavailable.

Safety posture: every input that reaches a subprocess argument is validated
(product/scope/tier/target against strict patterns), and the console only ever
requests DRY-RUN plans — a real (`--no-dry-run`) build is refused unless the host
explicitly opts in with ``AITHER_FACTORY_ALLOW_BUILD=1`` (real builds belong in
CI on a bootc runner, not behind a web tunnel).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,64}$")
_TARGETS = {"iso", "vhdx", "qcow2", "node"}
_TIERS = {"", "lite", "entry", "pro", "pro-reasoning", "full"}


def _find_factory_dir() -> Optional[Path]:
    """Locate the ``.DEPLOYMENT`` dir that contains the ``factory`` package."""
    override = os.getenv("AITHER_FACTORY_DIR", "").strip()
    if override:
        p = Path(override)
        return p if (p / "factory" / "cli.py").exists() else None
    # Walk up from this file looking for the factory directory (platform layout).
    for base in Path(__file__).resolve().parents:
        cand = base / ".DEPLOYMENT"
        if (cand / "factory" / "cli.py").exists():
            return cand
    return None


async def _run_factory(deployment_dir: Path, args: list[str]) -> tuple[int, dict]:
    """Run ``python -m factory.cli <args> --json`` in the factory dir."""
    cmd = ["python", "-m", "factory.cli", *args, "--json"]
    try:
        proc = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True,
            timeout=120, cwd=str(deployment_dir),
        )
    except subprocess.TimeoutExpired:
        return 504, {"error": "factory command timed out"}
    except FileNotFoundError:
        return 500, {"error": "python not found"}
    out = (proc.stdout or "").strip()
    try:
        payload = json.loads(out) if out else {}
    except json.JSONDecodeError:
        payload = {"raw": out[:4000], "stderr": (proc.stderr or "")[:2000]}
    return (200 if proc.returncode == 0 else 400), payload


def register_admin_factory_routes(app: FastAPI, *, state: dict[str, Any]) -> None:
    """Register ``/admin/factory/*`` on *app* (bearer-gated by the existing middleware)."""

    def _selector(body: dict) -> tuple[Optional[list[str]], Optional[str]]:
        """Build the --product/--scope selector args from a request body."""
        product = (body.get("product") or "").strip()
        scope = (body.get("scope") or "").strip()
        if product:
            if not _NAME_RE.match(product):
                return None, "invalid product name"
            return ["--product", product], None
        if scope:
            # A scope path must stay within the deployment tree (no traversal).
            if ".." in scope or scope.startswith(("/", "\\")):
                return None, "scope must be a relative path inside the factory dir"
            return ["--scope", scope], None
        return None, "product or scope required"

    @app.get("/admin/factory/status")
    async def factory_status():
        d = _find_factory_dir()
        return {
            "available": d is not None,
            "factory_dir": str(d) if d else None,
            "real_build_enabled": os.getenv("AITHER_FACTORY_ALLOW_BUILD", "") == "1",
        }

    @app.get("/admin/factory/products")
    async def factory_products():
        d = _find_factory_dir()
        if d is None:
            return JSONResponse(status_code=503, content={"error": "factory_unavailable"})
        code, payload = await _run_factory(d, ["list-products"])
        return JSONResponse(status_code=code, content=payload)

    @app.post("/admin/factory/resolve")
    async def factory_resolve(request: Request):
        d = _find_factory_dir()
        if d is None:
            return JSONResponse(status_code=503, content={"error": "factory_unavailable"})
        body = await request.json()
        sel, err = _selector(body)
        if err:
            return JSONResponse(status_code=400, content={"error": err})
        code, payload = await _run_factory(d, ["resolve", *sel])
        return JSONResponse(status_code=code, content=payload)

    @app.post("/admin/factory/build")
    async def factory_build(request: Request):
        d = _find_factory_dir()
        if d is None:
            return JSONResponse(status_code=503, content={"error": "factory_unavailable"})
        body = await request.json()
        sel, err = _selector(body)
        if err:
            return JSONResponse(status_code=400, content={"error": err})
        target = (body.get("target") or "iso").strip()
        if target not in _TARGETS:
            return JSONResponse(status_code=400, content={"error": f"invalid target {target!r}"})
        tier = (body.get("tier") or "").strip()
        if tier not in _TIERS:
            return JSONResponse(status_code=400, content={"error": f"invalid tier {tier!r}"})

        args = ["build", *sel, "--target", target]
        if tier:
            args += ["--tier", tier]

        # Real builds are refused from the console unless the host opts in — they
        # belong on a CI bootc runner, not behind a public tunnel.
        want_real = bool(body.get("no_dry_run"))
        if want_real and os.getenv("AITHER_FACTORY_ALLOW_BUILD", "") != "1":
            return JSONResponse(
                status_code=403,
                content={"error": "real_build_disabled",
                         "detail": "set AITHER_FACTORY_ALLOW_BUILD=1 to allow --no-dry-run "
                                   "from the console; otherwise build via CI."},
            )
        if want_real:
            args.append("--no-dry-run")
        code, payload = await _run_factory(d, args)
        return JSONResponse(status_code=code, content=payload)
