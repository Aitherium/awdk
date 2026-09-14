"""Object registry for pass-by-reference rendering in agent observations.

This module provides a singleton-capable registry that maps stable handles to live
Python objects. This prevents unbounded serialized text from reaching models while
allowing them to request more details about large or complex objects via handle.

Example::

    from adk.object_registry import ObjectRegistry, render_observation

    registry = ObjectRegistry()
    large_list = list(range(10000))

    # render_observation returns a bounded preview + handle for large objects
    preview = render_observation(large_list, registry=registry, max_chars=200)
    # Output: list(len=10000, [:5]=[0, 1, 2, 3, 4], [-5:]=[9995, 9996, 9997, 9998, 9999], id=obj:list:a1b2c3)

    # Model can ask for details via the handle
    obj = registry.get("obj:list:a1b2c3")
    assert obj is large_list
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Optional
from threading import Lock

from adk.agentdoc import pformat, truncating_pformat


class ObjectRegistry:
    """A thread-safe registry mapping stable handles to live Python objects.

    Handles are human/model-friendly strings like 'obj:list:a1b2c3', not raw UUIDs.
    Objects are stored with optional TTL and size-cap eviction policies.

    Attributes:
        max_objects: Maximum number of objects to retain (LRU eviction when exceeded).
        default_ttl_seconds: Default TTL for registered objects (None = no expiry).
    """

    def __init__(
        self,
        *,
        max_objects: int = 1000,
        default_ttl_seconds: Optional[float] = None,
    ) -> None:
        """Initialize the registry.

        Args:
            max_objects: Maximum objects before LRU eviction (default 1000).
            default_ttl_seconds: Default TTL for objects (None = no expiry).
        """
        self.max_objects = max_objects
        self.default_ttl_seconds = default_ttl_seconds

        # Store: handle -> (obj, timestamp, ttl)
        self._store: dict[str, tuple[Any, float, Optional[float]]] = {}
        self._lock = Lock()
        # Access order for LRU eviction (weakref to detect garbage collection)
        self._access_order: list[str] = []

    def _generate_handle(self, obj: Any) -> str:
        """Generate a human-friendly handle for an object.

        Format: obj:<type>:<hash_prefix>
        Example: obj:list:a1b2c3
        """
        type_name = type(obj).__name__
        # Use object id + type name to create a stable hash (stable within session)
        obj_id = id(obj)
        hash_input = f"{obj_id}:{type_name}".encode()
        hash_hex = hashlib.sha256(hash_input).hexdigest()
        hash_prefix = hash_hex[:6]
        return f"obj:{type_name}:{hash_prefix}"

    def _is_expired(self, timestamp: float, ttl: Optional[float]) -> bool:
        """Check if an object has expired."""
        if ttl is None:
            return False
        return time.time() - timestamp > ttl

    def _evict_lru(self) -> None:
        """Remove the least recently used item if max_objects is exceeded."""
        if len(self._store) > self.max_objects and self._access_order:
            oldest = self._access_order.pop(0)
            self._store.pop(oldest, None)

    def register(self, obj: Any, ttl_seconds: Optional[float] = None) -> str:
        """Register an object and return its handle.

        If the object is already registered, returns its existing handle.

        Args:
            obj: The object to register.
            ttl_seconds: Optional TTL override (None uses default).

        Returns:
            A stable handle string like 'obj:list:a1b2c3'.
        """
        handle = self._generate_handle(obj)

        with self._lock:
            # Check if already registered (by identity)
            for stored_handle, (stored_obj, _, _) in list(self._store.items()):
                if stored_obj is obj:
                    # Move to end (most recently used)
                    if stored_handle in self._access_order:
                        self._access_order.remove(stored_handle)
                    self._access_order.append(stored_handle)
                    return stored_handle

            # New registration
            ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
            self._store[handle] = (obj, time.time(), ttl)
            self._access_order.append(handle)
            self._evict_lru()

        return handle

    def get(self, handle: str) -> Any:
        """Retrieve an object by handle.

        Args:
            handle: The object handle (e.g., 'obj:list:a1b2c3').

        Returns:
            The registered object.

        Raises:
            KeyError: If handle not found or has expired.
        """
        with self._lock:
            if handle not in self._store:
                raise KeyError(f"Object handle not found: {handle}")

            obj, timestamp, ttl = self._store[handle]

            if self._is_expired(timestamp, ttl):
                del self._store[handle]
                self._access_order.remove(handle)
                raise KeyError(f"Object handle expired: {handle}")

            # Move to end (most recently used)
            if handle in self._access_order:
                self._access_order.remove(handle)
            self._access_order.append(handle)

            return obj

    def deref(self, text_or_handle: str) -> Optional[Any]:
        """Attempt to resolve a handle string to its object.

        If the text is a valid handle, returns the object. Otherwise returns None.

        Args:
            text_or_handle: Possibly a handle string.

        Returns:
            The object if the handle is valid, None otherwise.
        """
        try:
            return self.get(text_or_handle)
        except KeyError:
            return None

    def preview(self, handle: str, max_chars: Optional[int] = None) -> str:
        """Get a bounded preview of a registered object.

        Args:
            handle: The object handle.
            max_chars: Optional char limit for the preview.

        Returns:
            A bounded string preview.

        Raises:
            KeyError: If handle not found or expired.
        """
        obj = self.get(handle)

        # For strings, apply the char limit directly since truncating_pformat
        # passes strings through verbatim
        if isinstance(obj, str):
            if max_chars is not None and len(obj) > max_chars:
                return f"str(len={len(obj):,}, repr={repr(obj[:max_chars])}...)"
            return repr(obj) if max_chars is not None else obj

        if max_chars is not None:
            return truncating_pformat(obj, max_chars=max_chars)
        return pformat(obj)

    def describe(self, handle: str, max_preview_chars: Optional[int] = 100) -> str:
        """Get a description of a registered object.

        Format: <type>: <len>=<count> | <preview>

        Args:
            handle: The object handle.
            max_preview_chars: Max chars in the preview (default 100).

        Returns:
            A description string.

        Raises:
            KeyError: If handle not found or expired.
        """
        obj = self.get(handle)
        type_name = type(obj).__name__

        # Try to get length if available
        try:
            obj_len = len(obj)  # type: ignore
            len_str = f"len={obj_len}"
        except TypeError:
            len_str = ""

        # Generate preview
        preview = truncating_pformat(obj, max_chars=max_preview_chars or 100)

        parts = [type_name, len_str] if len_str else [type_name]
        parts.append(f"id={handle}")
        parts.append(f"preview={preview}")

        return " | ".join(parts)

    def clear(self) -> None:
        """Clear all registered objects."""
        with self._lock:
            self._store.clear()
            self._access_order.clear()

    def stats(self) -> dict[str, Any]:
        """Get registry statistics.

        Returns:
            A dict with 'count' and 'max_objects'.
        """
        with self._lock:
            return {"count": len(self._store), "max_objects": self.max_objects}


# Global singleton instance
_singleton: Optional[ObjectRegistry] = None
_singleton_lock = Lock()


def get_registry() -> ObjectRegistry:
    """Get or create the global ObjectRegistry singleton.

    Returns:
        The singleton ObjectRegistry instance.
    """
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = ObjectRegistry()
    return _singleton


def render_observation(
    value: Any,
    *,
    registry: Optional[ObjectRegistry] = None,
    max_chars: int = 500,
) -> str:
    """Render an observation value with bounded preview and optional handle.

    For small/simple values, returns the plaintext representation.
    For large/complex objects, returns a bounded preview + handle so the model
    can ask for more details.

    Args:
        value: The value to render.
        registry: Optional registry (uses singleton if None).
        max_chars: Char limit for the bounded preview (default 500).

    Returns:
        A bounded preview string, possibly with an object handle.

    Example:
        >>> large_list = list(range(10000))
        >>> preview = render_observation(large_list, max_chars=200)
        >>> # Output includes handle like: "list(len=10000, ..., id=obj:list:a1b2c3)"
    """
    if registry is None:
        registry = get_registry()

    # For strings, check if they're too large
    if isinstance(value, str):
        if len(value) <= max_chars:
            return value
        # Large string: register and return bounded preview
        handle = registry.register(value)
        # Show a compact repr of the string (first N chars + handle)
        preview = f"str(len={len(value):,}, repr={repr(value[:max_chars])[:200]}...)"
        return f"{preview} (id={handle})"

    # For other types, use truncating_pformat to get a bounded representation
    preview = truncating_pformat(value, max_chars=max_chars)

    # Check if the preview was truncated by looking for the truncation marker
    # The TruncatingStringIO adds "<truncated-output>" if it had to truncate
    if "<truncated-output>" in preview or len(preview) > max_chars * 2:
        # Object is large enough to register and provide a handle
        handle = registry.register(value)
        return f"{preview}\n(available via handle: {handle})"

    # Small enough to render plainly
    return preview
