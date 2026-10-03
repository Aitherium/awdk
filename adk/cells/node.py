"""A node serving its cells: HTTPS only, callers from a token file that holds hashes.

``python -m adk.cells serve --cell memory=/data/memory.db --tokens tokens.yaml
--cert node.pem --key node.key`` hosts the named cells on this machine. The tokens file
maps the SHA-256 of each bearer token to the caller it authenticates, so the file
never holds a usable credential::

    tokens:
      "<sha256 hex of the token>":
        subject: agent:builder-1
        scopes: [workspace]
        workspace: acme:dana:proj
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from typing import Any, Callable

import yaml
from fastapi import FastAPI, HTTPException, Request

from .caller import ANONYMOUS, Caller
from .contract import Scope
from .inventory import local_node, node_doc
from .registry import Cells
from .server import build_app


class TokenFileError(ValueError):
    pass


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_callers(path: str | Path) -> Callable[[str | None], Caller]:
    """An ``authenticate`` function for ``build_app`` from a hashed token file.
    Unknown or missing tokens are ANONYMOUS, which can reach only public ops."""
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    table: dict[str, Caller] = {}
    for digest, raw in (doc.get("tokens") or {}).items():
        digest = str(digest).lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise TokenFileError(f"token keys must be sha256 hex digests, got {digest[:12]}...")
        raw = raw or {}
        try:
            scopes = frozenset(Scope(s) for s in raw.get("scopes") or [])
        except ValueError as exc:
            raise TokenFileError(f"bad scope for {raw.get('subject')!r}: {exc}") from None
        table[digest] = Caller(
            subject=str(raw.get("subject") or "unnamed"), scopes=scopes,
            workspace=raw.get("workspace"),
        )

    def authenticate(token: str | None) -> Caller:
        if not token:
            return ANONYMOUS
        candidate = token_hash(token)
        for digest, caller in table.items():
            if hmac.compare_digest(digest, candidate):
                return caller
        return ANONYMOUS

    return authenticate


def build_node_app(
    cells: Cells,
    authenticate: Callable[[str | None], Caller],
    node_name: str | None = None,
    labels: dict[str, Any] | None = None,
) -> FastAPI:
    """The cells app plus ``GET /cells/_node``: this machine's measured inventory,
    for operators only (it is what the control plane plans on)."""
    app = build_app(cells, authenticate)

    @app.get("/cells/_node")
    async def node(request: Request) -> dict[str, Any]:
        header = request.headers.get("authorization", "")
        caller = authenticate(header[7:] if header.lower().startswith("bearer ") else None)
        if Scope.operator not in caller.scopes:
            raise HTTPException(status_code=403, detail="inventory is operator-only")
        return node_doc(local_node(node_name, labels))

    return app


def host_cells(specs: list[str]) -> Cells:
    """``["memory=/data/memory.db"]`` -> Cells hosting those implementations."""
    cells = Cells()
    for spec in specs:
        name, sep, target = spec.partition("=")
        if name == "memory" and sep and target:
            from .memory import AwmMemory

            cells.host(AwmMemory(target))
        else:
            raise ValueError(f"unknown cell spec {spec!r} (known: memory=<db path>)")
    return cells


def serve(
    cells: Cells,
    authenticate: Callable[[str | None], Caller],
    *,
    cert: str,
    key: str,
    host: str = "127.0.0.1",
    port: int = 8443,
    node_name: str | None = None,
) -> None:
    """Run the node over TLS. There is no plain-HTTP mode."""
    import uvicorn

    for label, path in (("cert", cert), ("key", key)):
        if not Path(path).is_file():
            raise FileNotFoundError(f"--{label} {path} does not exist")
    app = build_node_app(cells, authenticate, node_name)
    uvicorn.run(app, host=host, port=port, ssl_certfile=cert, ssl_keyfile=key,
                log_level="warning")
