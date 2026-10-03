"""The authenticated caller. Scope is decided here, never from a payload field."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

from .contract import OpSpec, Scope


class ScopeDeniedError(PermissionError):
    def __init__(self, caller: "Caller", op: OpSpec):
        super().__init__(f"{caller.subject} lacks scope {op.scope.value!r} for {op.qualname}")
        self.op = op


@dataclass(frozen=True)
class Caller:
    subject: str
    scopes: frozenset[Scope] = field(default_factory=frozenset)
    workspace: str | None = None

    def allows(self, op: OpSpec) -> bool:
        return op.scope is Scope.public or Scope.operator in self.scopes or op.scope in self.scopes

    def require(self, op: OpSpec) -> None:
        if not self.allows(op):
            raise ScopeDeniedError(self, op)


ANONYMOUS = Caller(subject="anonymous")


def operator(subject: str = "operator") -> Caller:
    return Caller(subject=subject, scopes=frozenset({Scope.operator}))


_CURRENT: ContextVar[Caller] = ContextVar("aither_cell_caller", default=ANONYMOUS)


def current_caller() -> Caller:
    """The authenticated caller of the op now running. Transports set it; an op reads
    it to decide WHOSE data it touches, so tenancy never comes from a payload field."""
    return _CURRENT.get()


@contextmanager
def acting_as(caller: Caller) -> Iterator[Caller]:
    token = _CURRENT.set(caller)
    try:
        yield caller
    finally:
        _CURRENT.reset(token)
