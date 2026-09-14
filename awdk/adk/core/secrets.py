"""Secret store — the security-posture #2 commitment.

Tools should never receive raw secret values. They receive a
:class:`SecretHandle`; the runtime resolves the handle through whatever
store is currently active. Default-deny: resolving a handle requires
:attr:`adk.core.capability.Capability.SECRET_READ` in the active
capability context.

Three concrete stores ship out of the box:

* :class:`EnvStore` — reads from process environment variables.
* :class:`FileStore` — reads from a JSON file on disk.
* :class:`KeyringStore` — reads from the OS keychain via the optional
  ``keyring`` package. Lazy-imported; raises if missing only when used.

The active store is a :data:`ContextVar` (process-wide default,
swappable per scope), so tests can plug in a stub without monkey-patching.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, Protocol

from adk.core.capability import Capability, current_context


class SecretNotFound(KeyError):
    """Raised when a handle cannot be resolved."""


@dataclass(frozen=True, slots=True)
class SecretHandle:
    """Opaque pointer to a secret. Holds NO value — value lives in the store."""

    key: str

    def __repr__(self) -> str:  # never leak the value
        return f"SecretHandle({self.key!r})"


class SecretStore(Protocol):
    """Minimal contract for a secret backend."""

    name: str

    def get(self, key: str) -> str: ...

    def has(self, key: str) -> bool: ...


# ---------------------------------------------------------------------------
# EnvStore
# ---------------------------------------------------------------------------


class EnvStore:
    """Read secrets from environment variables.

    Optional ``prefix`` lets you scope: ``EnvStore(prefix="AITHER_SECRET_")``
    means handle ``"openai"`` resolves to ``AITHER_SECRET_OPENAI``.
    """

    name = "env"

    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix

    def _key(self, key: str) -> str:
        return f"{self.prefix}{key}".upper() if self.prefix else key

    def get(self, key: str) -> str:
        env_key = self._key(key)
        try:
            return os.environ[env_key]
        except KeyError as e:
            raise SecretNotFound(env_key) from e

    def has(self, key: str) -> bool:
        return self._key(key) in os.environ


# ---------------------------------------------------------------------------
# FileStore (JSON on disk)
# ---------------------------------------------------------------------------


class FileStore:
    """Read secrets from a JSON file (``{"key": "value", ...}``).

    Useful for local development. NOT recommended for production —
    use :class:`KeyringStore` or AitherSecrets in production.
    """

    name = "file"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._cache: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        if self._cache is None:
            if not self.path.exists():
                raise SecretNotFound(str(self.path))
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise SecretNotFound(f"{self.path}: invalid JSON: {e}") from e
            if not isinstance(data, Mapping):
                raise SecretNotFound(f"{self.path}: not a JSON object")
            self._cache = {str(k): str(v) for k, v in data.items()}
        return self._cache

    def reload(self) -> None:
        self._cache = None

    def get(self, key: str) -> str:
        data = self._load()
        if key not in data:
            raise SecretNotFound(key)
        return data[key]

    def has(self, key: str) -> bool:
        try:
            return key in self._load()
        except SecretNotFound:
            return False


# ---------------------------------------------------------------------------
# KeyringStore (optional)
# ---------------------------------------------------------------------------


class KeyringStore:
    """Read secrets from the OS keychain via the ``keyring`` package.

    The ``keyring`` package is an *optional* dependency. We import it
    lazily so the rest of ADK works without it.
    """

    name = "keyring"

    def __init__(self, service: str = "aither_adk") -> None:
        self.service = service

    def _keyring(self):  # pragma: no cover - exercised only with extra installed
        try:
            import keyring  # type: ignore[import-not-found]
        except ImportError as e:
            raise SecretNotFound(
                "the optional 'keyring' package is not installed"
            ) from e
        return keyring

    def get(self, key: str) -> str:
        value = self._keyring().get_password(self.service, key)
        if value is None:
            raise SecretNotFound(f"{self.service}:{key}")
        return value

    def has(self, key: str) -> bool:
        try:
            return self._keyring().get_password(self.service, key) is not None
        except SecretNotFound:
            return False


# ---------------------------------------------------------------------------
# ChainStore — try multiple stores in order
# ---------------------------------------------------------------------------


class ChainStore:
    """Try a list of stores in order; return the first hit."""

    name = "chain"

    def __init__(self, stores: list[SecretStore]) -> None:
        if not stores:
            raise ValueError("ChainStore requires at least one backing store")
        self.stores = list(stores)

    def get(self, key: str) -> str:
        for store in self.stores:
            try:
                return store.get(key)
            except SecretNotFound:
                continue
        raise SecretNotFound(key)

    def has(self, key: str) -> bool:
        return any(store.has(key) for store in self.stores)


# ---------------------------------------------------------------------------
# Active store + handle resolution
# ---------------------------------------------------------------------------


_DEFAULT_STORE: SecretStore = EnvStore()
_active: ContextVar[SecretStore | None] = ContextVar(
    "aither_adk_secret_store", default=None
)


def get_store() -> SecretStore:
    """Return the active secret store (scope-local override, else default)."""
    store = _active.get()
    return store if store is not None else _DEFAULT_STORE


def set_default_store(store: SecretStore) -> None:
    """Replace the process-wide default secret store."""
    global _DEFAULT_STORE
    _DEFAULT_STORE = store


@contextmanager
def use_store(store: SecretStore) -> Iterator[SecretStore]:
    """Activate ``store`` for the current scope."""
    token = _active.set(store)
    try:
        yield store
    finally:
        _active.reset(token)


def resolve(handle: SecretHandle | str) -> str:
    """Resolve a handle to its raw value, checking SECRET_READ first.

    Pass a :class:`SecretHandle` or a bare string key. Returns the secret
    value — caller is responsible for never logging it.
    """
    key = handle.key if isinstance(handle, SecretHandle) else str(handle)
    current_context().check(Capability.SECRET_READ)
    return get_store().get(key)


def handle(key: str) -> SecretHandle:
    """Convenience factory for a :class:`SecretHandle`."""
    return SecretHandle(key=key)


__all__ = [
    "ChainStore",
    "EnvStore",
    "FileStore",
    "KeyringStore",
    "SecretHandle",
    "SecretNotFound",
    "SecretStore",
    "get_store",
    "handle",
    "resolve",
    "set_default_store",
    "use_store",
]
