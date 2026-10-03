"""Serve in-process cells over HTTPS. Every route is generated from a contract."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from .caller import Caller, ScopeDeniedError
from .contract import ContractSpec, OpSpec
from .registry import Cells
from .surfaces import contract_index

Authenticate = Callable[[str | None], Caller]


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    return header[7:] if header.lower().startswith("bearer ") else None


def build_app(cells: Cells, authenticate: Authenticate) -> FastAPI:
    """One FastAPI app exposing every cell this process hosts.

    ``authenticate`` turns the bearer token into a Caller; scope comes only from it.
    """
    app = FastAPI(title="aither cells")
    impls = cells.local_impls()
    hosted: list[ContractSpec] = [cells.specs[name] for name in impls]

    @app.get("/cells/_contracts")
    async def contracts() -> dict[str, Any]:
        return contract_index(hosted)

    @app.get("/cells/_health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "cells": sorted(impls)}

    def bind(op: OpSpec, impl: Any) -> None:
        async def handler(request: Request) -> dict[str, Any]:
            caller = authenticate(_bearer(request))
            try:
                caller.require(op)
            except ScopeDeniedError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from None
            try:
                params = op.params.model_validate(await request.json())
            except ValidationError as exc:
                raise HTTPException(status_code=422, detail=exc.errors()) from None
            except ValueError:
                raise HTTPException(status_code=422, detail="body is not JSON") from None
            result = await getattr(impl, op.name)(**dict(params))
            return {"result": op.result_adapter().dump_python(result, mode="json")}

        app.add_api_route(op.route, handler, methods=["POST"], name=op.qualname)

    for spec in hosted:
        for op in spec.ops.values():
            bind(op, impls[spec.name])
    return app
