"""Cells: one codebase, deployed as one process or as a swarm of microservices.

A cell is a microservice in deployment and a module in code. Its contract generates
its HTTPS routes, client calls, MCP tools, CLI verbs and schedules. Where it runs is a
placement decision (``estate.plan``), never a code change::

    from adk.cells import Cells, contract, op, Scope

    @contract("memory", version=1)
    class Memory:
        @op(scope=Scope.workspace)
        async def recall(self, query: str, k: int = 8) -> list[str]: ...

    cells = Cells().host(MyMemory())                 # laptop: in-process
    cells = Cells().remote(Memory, "https://n2:8443") # swarm: another node
    await cells.memory.recall(query="why", k=3)      # same call either way
"""

from .caller import ANONYMOUS, Caller, ScopeDeniedError, operator
from .contract import ContractError, ContractSpec, OpSpec, Scope, contract, op, spec_of
from .estate import Estate, EstateError, Node, PlanError, diff, load, load_files, plan
from .registry import Cells, UnknownCellError
from .surfaces import cli_verbs, contract_index, mcp_tools, schedules
from .transport import CellCallError, HttpsTransport, LocalTransport

__all__ = [
    "ANONYMOUS", "Caller", "CellCallError", "Cells", "ContractError", "ContractSpec",
    "Estate", "EstateError", "HttpsTransport", "LocalTransport", "Node", "OpSpec",
    "PlanError", "Scope", "ScopeDeniedError", "UnknownCellError", "cli_verbs", "contract",
    "contract_index", "diff", "load", "load_files", "mcp_tools", "op", "operator",
    "plan", "schedules", "spec_of",
]
