"""Cell contracts: the one declaration every surface is generated from.

A contract is a class whose methods are marked with ``@op``. Each op becomes an
HTTP route, a generated client call, an MCP tool, an optional CLI verb and an
optional schedule - from the same signature, so they cannot drift apart.
"""

from __future__ import annotations

import inspect
import typing
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, TypeAdapter, create_model


class Scope(str, Enum):
    """Who may call an op. A caller holds a set of these; ``operator`` implies all."""

    public = "public"
    user = "user"
    workspace = "workspace"
    operator = "operator"


class ContractError(Exception):
    """A contract declaration is malformed."""


@dataclass(frozen=True)
class OpSpec:
    cell: str
    version: int
    name: str
    scope: Scope
    doc: str
    params: type[BaseModel]
    returns: Any
    mcp: bool = True
    cli: str | None = None
    idempotent: bool = False
    schedule: str | None = None

    @property
    def qualname(self) -> str:
        return f"{self.cell}.{self.name}"

    @property
    def route(self) -> str:
        return f"/cells/{self.cell}/v{self.version}/{self.name}"

    def result_adapter(self) -> TypeAdapter:
        return TypeAdapter(self.returns)


@dataclass(frozen=True)
class ContractSpec:
    name: str
    version: int
    cls: type
    ops: dict[str, OpSpec] = field(default_factory=dict)


_OP_MARK = "__aither_op__"
_CONTRACT_MARK = "__aither_contract__"


def op(
    *,
    scope: Scope = Scope.workspace,
    mcp: bool = True,
    cli: str | None = None,
    idempotent: bool = False,
    schedule: str | None = None,
) -> Callable[[Callable], Callable]:
    """Mark an async contract method as an op."""

    def mark(fn: Callable) -> Callable:
        if not inspect.iscoroutinefunction(fn):
            raise ContractError(f"op {fn.__qualname__} must be async")
        setattr(fn, _OP_MARK, {
            "scope": Scope(scope), "mcp": mcp, "cli": cli,
            "idempotent": idempotent, "schedule": schedule,
        })
        return fn

    return mark


def _params_model(cell: str, fn: Callable, hints: dict[str, Any]) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for pname, param in list(inspect.signature(fn).parameters.items())[1:]:
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            raise ContractError(f"op {cell}.{fn.__name__}: *args/**kwargs are not allowed")
        if pname not in hints:
            raise ContractError(f"op {cell}.{fn.__name__}: parameter {pname!r} needs a type")
        default = ... if param.default is param.empty else param.default
        fields[pname] = (hints[pname], default)
    model_name = f"{cell.title().replace('-', '')}{fn.__name__.title().replace('_', '')}Params"
    return create_model(model_name, __config__=ConfigDict(extra="forbid"), **fields)


def contract(name: str, *, version: int = 1) -> Callable[[type], type]:
    """Declare a class as the contract of cell ``name``."""

    def build(cls: type) -> type:
        ops: dict[str, OpSpec] = {}
        for attr, fn in vars(cls).items():
            meta = getattr(fn, _OP_MARK, None)
            if meta is None:
                continue
            hints = typing.get_type_hints(fn, include_extras=True)
            if "return" not in hints:
                raise ContractError(f"op {name}.{attr} needs a return type")
            ops[attr] = OpSpec(
                cell=name, version=version, name=attr,
                doc=inspect.cleandoc(fn.__doc__ or ""),
                params=_params_model(name, fn, hints), returns=hints["return"], **meta,
            )
        if not ops:
            raise ContractError(f"contract {name} declares no ops")
        setattr(cls, _CONTRACT_MARK, ContractSpec(name=name, version=version, cls=cls, ops=ops))
        return cls

    return build


def spec_of(obj: Any) -> ContractSpec:
    """The ContractSpec of a contract class, or of an implementation subclassing one."""
    cls = obj if isinstance(obj, type) else type(obj)
    found = getattr(cls, _CONTRACT_MARK, None)
    if found is None:
        raise ContractError(f"{cls.__qualname__} is not a cell contract")
    return found
