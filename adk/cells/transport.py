"""Transports: how a call reaches a cell. Caller code never knows which one it used.

``LocalTransport`` calls an implementation in this process. ``HttpsTransport`` calls
the same op on another node. Both validate the arguments against the contract and
enforce the caller's scope before anything runs.
"""

from __future__ import annotations

import ssl
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from .caller import Caller
from .contract import OpSpec, spec_of


class CellCallError(RuntimeError):
    def __init__(self, op: OpSpec, status: int, detail: str):
        super().__init__(f"{op.qualname} failed ({status}): {detail}")
        self.status = status
        self.detail = detail


class Transport(Protocol):
    async def call(self, op: OpSpec, caller: Caller, args: dict[str, Any]) -> Any: ...


class LocalTransport:
    """Run the op on an implementation object living in this process."""

    def __init__(self, impl: Any):
        self.spec = spec_of(impl)
        self.impl = impl

    async def call(self, op: OpSpec, caller: Caller, args: dict[str, Any]) -> Any:
        caller.require(op)
        params = op.params.model_validate(args)
        result = await getattr(self.impl, op.name)(**dict(params))
        return op.result_adapter().validate_python(result)


class HttpsTransport:
    """Call a cell on another node. Plain ``http://`` is refused, so no call can hang
    on a TLS port or travel unencrypted between nodes. Certificates are always checked:
    pass the swarm CA as ``ca`` (a bundle path or an ``ssl.SSLContext``); there is no
    switch that turns verification off."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        ca: str | ssl.SSLContext | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        if urlsplit(base_url).scheme != "https":
            raise ValueError(f"cells speak https only, got {base_url!r}")
        if isinstance(ca, str):
            ca = ssl.create_default_context(cafile=ca)
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = client or httpx.AsyncClient(verify=ca or True, timeout=30.0)

    async def call(self, op: OpSpec, caller: Caller, args: dict[str, Any]) -> Any:
        caller.require(op)
        params = op.params.model_validate(args)
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        resp = await self._client.post(
            self.base_url + op.route, json=params.model_dump(mode="json"), headers=headers
        )
        if resp.status_code != 200:
            raise CellCallError(op, resp.status_code, resp.text[:300])
        return op.result_adapter().validate_python(resp.json()["result"])

    async def aclose(self) -> None:
        await self._client.aclose()
