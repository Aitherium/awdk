"""The memory cell: scoped agent memory (awm) as a cell.

The contract has no tenant, user or project parameter. WHOSE memory an op touches is
the caller's ``workspace`` (``tenant:user:project``), set by authentication, so a
caller can never read or write another workspace by naming it in a payload.

Requires the ``memory`` extra (``pip install awdk[memory]``) for ``AwmMemory``; the
contract itself imports nothing beyond the kernel.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .caller import current_caller
from .contract import Scope, contract, op


class MemoryItem(BaseModel):
    scope: str
    key: str
    value: str
    kind: str
    updated: float
    weight: float = 1.0


class MemoryVersion(BaseModel):
    value: str
    valid_from: float
    valid_to: float | None = None


class NoWorkspaceError(PermissionError):
    """The caller is authenticated but bound to no workspace."""


@contract("memory", version=1)
class MemoryCell:
    @op(scope=Scope.workspace, idempotent=True, cli="aw mem remember")
    async def remember(self, key: str, value: str, kind: str = "fact") -> MemoryItem:
        """Store a value under a key in your workspace. Overwrites the key."""

    @op(scope=Scope.workspace, cli="aw mem recall")
    async def recall(
        self, query: str | None = None, limit: int = 20, kind: str | None = None
    ) -> list[MemoryItem]:
        """Find memories in your workspace and the scopes above it, nearest first."""

    @op(scope=Scope.workspace)
    async def forget(self, key: str) -> bool:
        """Delete one key from your workspace. Never touches a parent scope."""

    @op(scope=Scope.workspace)
    async def history(self, key: str) -> list[MemoryVersion]:
        """Every value a key in your workspace has held, oldest first."""


def _caller_scope() -> Any:
    from awm import Scope as AwmScope

    caller = current_caller()
    if not caller.workspace:
        raise NoWorkspaceError(f"{caller.subject} is bound to no workspace")
    return AwmScope.parse(caller.workspace)


def _item(mem: Any) -> MemoryItem:
    return MemoryItem(
        scope=str(mem.scope), key=mem.key, value=mem.value, kind=mem.kind,
        updated=float(mem.updated), weight=float(getattr(mem, "weight", 1.0) or 1.0),
    )


class AwmMemory(MemoryCell):
    """The memory cell backed by an awm SQLite store. awm is synchronous, so each op
    runs in a worker thread and the event loop never blocks on disk."""

    def __init__(self, path: str | Path):
        from awm import MemoryStore

        self._store = MemoryStore(Path(path), check_same_thread=False)
        self._lock = asyncio.Lock()

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        async with self._lock:
            return await asyncio.to_thread(fn, *args, **kwargs)

    async def remember(self, key: str, value: str, kind: str = "fact") -> MemoryItem:
        mem = await self._call(self._store.remember, _caller_scope(), key, value, kind=kind)
        return _item(mem)

    async def recall(
        self, query: str | None = None, limit: int = 20, kind: str | None = None
    ) -> list[MemoryItem]:
        mems = await self._call(
            self._store.recall, _caller_scope(), query=query, limit=limit, kind=kind
        )
        return [_item(m) for m in mems]

    async def forget(self, key: str) -> bool:
        return bool(await self._call(self._store.forget, _caller_scope(), key))

    async def history(self, key: str) -> list[MemoryVersion]:
        entries = await self._call(self._store.history, _caller_scope(), key)
        return [
            MemoryVersion(
                value=e.value, valid_from=float(e.valid_from),
                valid_to=None if e.valid_to is None else float(e.valid_to),
            )
            for e in entries
        ]
