"""The control plane's view of one node: its inventory and its runtime, over TLS.

``RemoteNode`` implements the reconcile ``Runtime`` protocol against a node's
operator-only ``/cells/_runtime`` surface, so ``reconcile.apply`` drives a machine on
the other side of the swarm exactly as it drives a local podman.
"""

from __future__ import annotations

import ssl
from typing import Any
from urllib.parse import urlsplit

import httpx


class NodeError(RuntimeError):
    def __init__(self, node: str, what: str, status: int | None, detail: str):
        where = f"{status}" if status is not None else "unreachable"
        super().__init__(f"node {node}: {what} failed ({where}): {detail}")
        self.node = node
        self.status = status


class RemoteNode:
    """A node reached at ``https://...`` with an operator token and the swarm CA."""

    def __init__(
        self,
        name: str,
        url: str,
        token: str,
        *,
        ca: str | ssl.SSLContext | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        action_timeout: float = 200.0,
    ):
        if urlsplit(url).scheme != "https":
            raise ValueError(f"nodes speak https only, got {url!r}")
        if isinstance(ca, str):
            ca = ssl.create_default_context(cafile=ca)
        self.name = name
        self.url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client = client or httpx.Client(verify=ca or True, timeout=timeout)
        # Starting or stopping a container waits on the runtime: an image pull, or a
        # stop that sits out its grace period (podman rm --time 30) plus the hop into
        # the node's runtime. Reads stay on the short timeout; actions get this one, so
        # a slow-but-successful stop is not reported as a failure.
        self._action_timeout = action_timeout

    def _call(self, method: str, path: str, what: str, body: Any = None,
              timeout: float | None = None) -> Any:
        extra = {} if timeout is None else {"timeout": timeout}
        try:
            resp = self._client.request(method, self.url + path, json=body,
                                        headers=self._headers, **extra)
        except httpx.HTTPError as exc:
            raise NodeError(self.name, what, None, str(exc)[:300]) from None
        if resp.status_code != 200:
            raise NodeError(self.name, what, resp.status_code, resp.text[:300])
        return resp.json()

    def inventory(self) -> dict[str, Any]:
        """The node's ``nodes:`` entry, renamed to the name the controller knows it by."""
        doc = self._call("GET", "/cells/_node", "inventory")
        entries = list((doc.get("nodes") or {}).values())
        if len(entries) != 1:
            raise NodeError(self.name, "inventory", 200, f"expected 1 node, got {len(entries)}")
        return entries[0]

    def running(self) -> list[str]:
        return list(self._call("GET", "/cells/_runtime", "running")["running"])

    def start(self, cell: str) -> None:
        self._call("POST", "/cells/_runtime/start", f"start {cell}", {"cell": cell},
                   timeout=self._action_timeout)

    def stop(self, cell: str) -> None:
        self._call("POST", "/cells/_runtime/stop", f"stop {cell}", {"cell": cell},
                   timeout=self._action_timeout)

    def close(self) -> None:
        self._client.close()
