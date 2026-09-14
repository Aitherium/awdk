"""Tool primitives.

Two ways to make a tool:

1. ``@tool`` decorator on any function (sync or async). Schema is derived
   from the signature + type hints + docstring.
2. Subclass :class:`Tool` for stateful tools.

Every tool can declare ``requires`` (a list of :class:`Capability`). The
runtime checks them before invocation. Default-deny.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Callable, get_type_hints

from adk.core.capability import Capability, current_context


class ToolError(RuntimeError):
    """Raised by a tool when it fails in a way the agent should see."""


@dataclass(slots=True)
class ToolResult:
    """Structured tool result. ``ok=False`` signals a recoverable error."""

    ok: bool
    value: Any = None
    error: str | None = None

    @classmethod
    def success(cls, value: Any) -> "ToolResult":
        return cls(ok=True, value=value)

    @classmethod
    def failure(cls, error: str) -> "ToolResult":
        return cls(ok=False, error=error)


class Tool:
    """Base class for stateful or class-based tools.

    Subclasses implement :meth:`call` (sync or async) and may declare
    ``requires`` as a class-level tuple of :class:`Capability`.
    """

    name: str = ""
    description: str = ""
    requires: tuple[Capability, ...] = ()

    def __init__(self, *, name: str | None = None, description: str | None = None) -> None:
        if name:
            self.name = name
        if description:
            self.description = description
        if not self.name:
            self.name = self.__class__.__name__
        if not self.description:
            self.description = (self.__class__.__doc__ or "").strip().splitlines()[0:1]
            self.description = self.description[0] if self.description else ""

    async def __call__(self, **kwargs: Any) -> ToolResult:
        ctx = current_context()
        for cap in self.requires:
            ctx.check(cap)
        result = self.call(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, ToolResult):
            return result
        return ToolResult.success(result)

    def call(self, **kwargs: Any) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def schema(self) -> dict[str, Any]:
        sig = inspect.signature(self.call)
        return _schema_from_signature(self.name, self.description, sig, self.call)


def tool(
    _fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    requires: tuple[Capability, ...] = (),
) -> Any:
    """Turn a function into a :class:`Tool`.

    Usage::

        @tool(requires=(Capability.NET_HTTP,))
        async def fetch(url: str) -> str:
            ...
    """

    def wrap(fn: Callable[..., Any]) -> Tool:
        tool_name = name or fn.__name__
        tool_desc = description or (fn.__doc__ or "").strip().splitlines()[0:1]
        tool_desc = tool_desc[0] if tool_desc else ""

        class _FnTool(Tool):
            pass

        _FnTool.name = tool_name
        _FnTool.description = tool_desc
        _FnTool.requires = tuple(requires)

        if asyncio.iscoroutinefunction(fn):

            async def _call(self: Tool, **kwargs: Any) -> Any:
                return await fn(**kwargs)

        else:

            def _call(self: Tool, **kwargs: Any) -> Any:
                return fn(**kwargs)

        _FnTool.call = _call  # type: ignore[method-assign]

        instance = _FnTool()

        # The schema for a function-tool comes from the *function* signature,
        # not the wrapper, so override.
        def _schema(self: Tool, _fn=fn) -> dict[str, Any]:
            return _schema_from_signature(
                self.name, self.description, inspect.signature(_fn), _fn
            )

        _FnTool.schema = _schema  # type: ignore[method-assign]
        instance.__wrapped__ = fn  # type: ignore[attr-defined]
        return instance

    if _fn is not None:
        return wrap(_fn)
    return wrap


_PY_TO_JSON = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _schema_from_signature(
    name: str,
    description: str,
    sig: inspect.Signature,
    fn: Callable[..., Any],
) -> dict[str, Any]:
    try:
        hints = get_type_hints(fn)
    except Exception:
        hints = {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        py_type = hints.get(param_name, str)
        json_type = _PY_TO_JSON.get(py_type, "string")
        prop: dict[str, Any] = {"type": json_type}
        if param.default is inspect.Parameter.empty:
            required.append(param_name)
        else:
            prop["default"] = param.default
        properties[param_name] = prop
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }
