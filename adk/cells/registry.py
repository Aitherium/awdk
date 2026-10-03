"""The cell registry: ``cells.memory.recall(...)`` resolves to whichever transport
placement chose, so the same caller code runs on a laptop or across a swarm."""

from __future__ import annotations

from typing import Any

from .caller import ANONYMOUS, Caller
from .contract import ContractSpec, spec_of
from .transport import HttpsTransport, LocalTransport, Transport


class UnknownCellError(LookupError):
    pass


class _Bound:
    def __init__(self, spec: ContractSpec, transport: Transport, caller: Caller):
        self._spec, self._transport, self._caller = spec, transport, caller

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        op = self._spec.ops.get(name)
        if op is None:
            raise AttributeError(f"cell {self._spec.name} has no op {name!r}")

        async def invoke(**kwargs: Any) -> Any:
            return await self._transport.call(op, self._caller, kwargs)

        invoke.__name__ = op.name
        invoke.__doc__ = op.doc
        return invoke


class Cells:
    """Contracts plus where each one runs."""

    def __init__(self) -> None:
        self._specs: dict[str, ContractSpec] = {}
        self._routes: dict[str, Transport] = {}

    def host(self, impl: Any) -> "Cells":
        """Run this implementation in-process."""
        spec = spec_of(impl)
        self._specs[spec.name] = spec
        self._routes[spec.name] = LocalTransport(impl)
        return self

    def remote(self, contract_cls: type, transport: Transport | str) -> "Cells":
        """Reach this contract on another node."""
        spec = spec_of(contract_cls)
        self._specs[spec.name] = spec
        self._routes[spec.name] = (
            HttpsTransport(transport) if isinstance(transport, str) else transport
        )
        return self

    @property
    def specs(self) -> dict[str, ContractSpec]:
        return dict(self._specs)

    def local_impls(self) -> dict[str, Any]:
        return {n: t.impl for n, t in self._routes.items() if isinstance(t, LocalTransport)}

    def as_caller(self, caller: Caller) -> "_CellsView":
        return _CellsView(self, caller)

    def __getattr__(self, name: str) -> _Bound:
        if name.startswith("_"):
            raise AttributeError(name)
        return _CellsView(self, ANONYMOUS).__getattr__(name)


class _CellsView:
    def __init__(self, cells: Cells, caller: Caller):
        self._cells, self._caller = cells, caller

    def __getattr__(self, name: str) -> _Bound:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            spec = self._cells._specs[name]
        except KeyError:
            raise UnknownCellError(f"no cell named {name!r} is placed") from None
        return _Bound(spec, self._cells._routes[name], self._caller)
