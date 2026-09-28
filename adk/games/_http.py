"""Shared HTTP plumbing for game clients: one httpx.Client, injectable transport."""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from .base import GameError

DEFAULT_TIMEOUT = 30.0


def make_client(base_url: str, headers: Dict[str, str],
                transport: Optional[httpx.BaseTransport] = None,
                timeout: float = DEFAULT_TIMEOUT,
                verify: Any = True) -> httpx.Client:
    kwargs: Dict[str, Any] = {"base_url": base_url, "headers": headers,
                              "timeout": timeout}
    if transport is not None:
        kwargs["transport"] = transport
    else:
        kwargs["verify"] = verify
    return httpx.Client(**kwargs)


def request_json(client: httpx.Client, method: str, path: str,
                 *, what: str, **kwargs: Any) -> Any:
    """Send, and turn every failure into a GameError that names the step."""
    try:
        resp = client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise GameError(f"{what}: could not reach {client.base_url}{path} "
                        f"({type(exc).__name__}: {exc})") from exc
    if resp.status_code >= 400:
        detail: Any
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text[:300]
        err = GameError(f"{what}: {method} {path} -> {resp.status_code} {detail}")
        err.status_code = resp.status_code  # type: ignore[attr-defined]
        err.detail = detail  # type: ignore[attr-defined]
        raise err
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError as exc:
        raise GameError(f"{what}: {method} {path} answered non-JSON") from exc
