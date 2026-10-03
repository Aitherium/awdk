"""Surfaces generated from contracts: MCP tools, CLI verbs, schedules, the index.

Nothing here is hand-written per op. Adding an op to a contract adds it everywhere.
"""

from __future__ import annotations

from typing import Any, Iterable

from .caller import Caller
from .contract import ContractSpec, OpSpec


def _ops(specs: Iterable[ContractSpec]) -> list[OpSpec]:
    return [op for spec in specs for op in spec.ops.values()]


def mcp_tools(specs: Iterable[ContractSpec], caller: Caller) -> list[dict[str, Any]]:
    """The MCP tool list this caller may see: contract ops marked ``mcp`` that its
    scopes allow. An agent is never shown a tool it could not call."""
    tools = []
    for op in _ops(specs):
        if not op.mcp or not caller.allows(op):
            continue
        tools.append({
            "name": f"{op.cell}_{op.name}",
            "description": op.doc or f"{op.qualname} (v{op.version})",
            "inputSchema": op.params.model_json_schema(),
            "annotations": {"idempotentHint": op.idempotent},
        })
    return tools


def cli_verbs(specs: Iterable[ContractSpec]) -> dict[str, str]:
    """``{"aw mem recall": "memory.recall"}`` for every op that names a CLI verb."""
    return {op.cli: op.qualname for op in _ops(specs) if op.cli}


def schedules(specs: Iterable[ContractSpec]) -> list[dict[str, str]]:
    """Every op with a cron schedule. A schedule lives on the op it calls."""
    return [{"op": op.qualname, "cron": op.schedule} for op in _ops(specs) if op.schedule]


def contract_index(specs: Iterable[ContractSpec]) -> dict[str, Any]:
    return {
        spec.name: {
            "version": spec.version,
            "ops": {
                op.name: {
                    "route": op.route,
                    "scope": op.scope.value,
                    "params": op.params.model_json_schema(),
                    "idempotent": op.idempotent,
                    "schedule": op.schedule,
                }
                for op in spec.ops.values()
            },
        }
        for spec in specs
    }
