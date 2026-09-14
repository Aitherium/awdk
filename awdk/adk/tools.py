"""Tool system — @tool decorator and ToolRegistry for agent function calling."""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from adk.auth import AuthContext

logger = logging.getLogger("adk.tools")


@dataclass
class ToolDef:
    """A registered tool definition."""
    name: str
    description: str
    parameters: dict  # JSON Schema
    fn: Callable
    is_async: bool = False
    required_clearance: int = 0  # Minimum clearance level required to execute
    action_class: str = ""  # Action class (e.g., "write", "delete", "admin")
    intent_categories: list[str] = field(default_factory=list)  # Intent types this tool is available for (empty=all)
    expose_to_a2a: bool = False  # Opt-in: remotely invokable over A2A skills/invoke (default: local-only)


class ToolRegistry:
    """Registry of tool functions that agents can call."""

    def __init__(self):
        self._tools: dict[str, ToolDef] = {}

    def register(
        self,
        fn: Callable,
        name: str | None = None,
        description: str | None = None,
        required_clearance: int = 0,
        action_class: str = "",
        intent_categories: list[str] | None = None,
        expose_to_a2a: bool = False,
    ) -> ToolDef:
        """Register a function as a tool.

        Args:
            fn: The function to register.
            name: Tool name (defaults to function name).
            description: Tool description (defaults to docstring first line).
            required_clearance: Minimum clearance level required (default 0, no restriction).
            action_class: Action class category (e.g., "write", "delete"). Empty = no restriction.
            intent_categories: Intent types this tool is available for (default None=all intents).
            expose_to_a2a: Allow remote invocation via A2A skills/invoke (default False =
                local-only). A remote peer can only reach tools explicitly opted in here.
        """
        tool_name = name or fn.__name__
        tool_desc = description or fn.__doc__ or f"Tool: {tool_name}"
        tool_desc = tool_desc.strip().split("\n")[0]  # First line only

        params = _extract_parameters(fn)
        is_async = inspect.iscoroutinefunction(fn)

        td = ToolDef(
            name=tool_name,
            description=tool_desc,
            parameters=params,
            fn=fn,
            is_async=is_async,
            required_clearance=required_clearance,
            action_class=action_class,
            intent_categories=intent_categories or [],
            expose_to_a2a=expose_to_a2a,
        )
        self._tools[tool_name] = td
        return td

    def unregister(self, name: str) -> bool:
        """Remove a tool. Returns whether it was there.

        Registration used to be one-way, which is fine for a capability that is
        either present or absent for a whole process. It is NOT fine for a tool
        whose legality depends on the CURRENT model — the external-thinking
        scratchpad only works while the provider's native reasoning channel can
        be switched off, and swapping models mid-session changes that answer.
        Without this, such a tool stays armed after a swap and the model emits
        on two reasoning channels at once, or the request is rejected outright.

        Deliberately narrow: it removes the tool from the registry so it stops
        being exported to the model, and does nothing else. A caller re-adds it
        with :meth:`register`.
        """
        return self._tools.pop(name, None) is not None

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def list_tools(self) -> list[ToolDef]:
        return list(self._tools.values())

    def to_openai_format(self, tools: list["ToolDef"] | None = None) -> list[dict]:
        """Export tools in OpenAI function-calling format.

        tools: optional subset to export (e.g. an intent-filtered list). When
        None, exports the full registry (backward-compatible).
        """
        result = []
        for td in (tools if tools is not None else self._tools.values()):
            result.append({
                "type": "function",
                "function": {
                    "name": td.name,
                    "description": td.description,
                    "parameters": td.parameters,
                },
            })
        return result

    async def execute(self, name: str, arguments: dict, auth: AuthContext | None = None) -> str:
        """Execute a tool by name with given arguments. Returns result as string.

        Args:
            name: Tool name.
            arguments: Arguments dict for the tool.
            auth: Optional AuthContext for authorization. If provided and the principal
                 is not authorized, returns a forbidden error without executing the tool.
                 If None (default), all tools execute normally (backward compatible).

        Returns:
            JSON string with tool result or error dict.
        """
        td = self._tools.get(name)
        if not td:
            return json.dumps({"error": f"Unknown tool: {name}"})

        # Authorization check: if auth is provided, enforce it
        if auth is not None:
            if not auth.can(
                name,
                required_clearance=td.required_clearance,
                action_class=td.action_class,
            ):
                logger.warning(
                    f"Forbidden tool call: {name} by {auth.principal.subject_id} "
                    f"(clearance={auth.principal.clearance}, "
                    f"required={td.required_clearance}, "
                    f"verified={auth.principal.verified})"
                )
                return json.dumps({
                    "error": "forbidden",
                    "tool": name,
                    "reason": "principal not authorized",
                })

        try:
            # Coerce LLM-supplied args toward each parameter's annotated type. Models
            # routinely pass a scalar wrapped in a list/str (e.g. limit=["5"]) which
            # would otherwise crash the tool (int() of a list). Conservative: only
            # touches int/float/bool/str params.
            if isinstance(arguments, dict):
                arguments = _coerce_arguments(td.fn, arguments)
            if td.is_async:
                result = await td.fn(**arguments)
            else:
                result = td.fn(**arguments)

            if isinstance(result, str):
                return result
            return json.dumps(result, default=str)
        except Exception as e:
            logger.error(f"Tool {name} failed: {e}")
            return json.dumps({"error": str(e)})


# Module-level registry for the @tool decorator
_global_registry = ToolRegistry()


def tool(
    fn: Callable | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    intent_categories: list[str] | None = None,
):
    """Decorator to register a function as an agent tool.

    Usage:
        @tool
        def search_web(query: str) -> str:
            '''Search the web for information.'''
            ...

        @tool(name="calculator", description="Evaluate math expressions")
        def calc(expression: str) -> str:
            ...

        @tool(intent_categories=["code", "analysis"])
        def file_read(path: str) -> str:
            '''Read a file.'''
            ...
    """
    def decorator(f: Callable) -> Callable:
        td = _global_registry.register(
            f,
            name=name,
            description=description,
            intent_categories=intent_categories,
        )
        f._tool_def = td
        f.name = td.name
        f.description = td.description
        return f

    if fn is not None:
        return decorator(fn)
    return decorator


def get_global_registry() -> ToolRegistry:
    """Get the global tool registry (populated by @tool decorator)."""
    return _global_registry


# ─────────────────────────────────────────────────────────────────────────────
# Native FunctionTool / ToolContext
# Standalone replacements for google.adk.tools.{FunctionTool, ToolContext} so the
# kit never *requires* google-adk. Platform modules import these via a guarded
# fallback (google's classes when google-adk is installed, these otherwise).
# ─────────────────────────────────────────────────────────────────────────────


class FunctionTool:
    """Wrap a plain function as a tool object (mirrors the minimal surface of
    ``google.adk.tools.FunctionTool``: ``.func``, ``.name``, callable)."""

    def __init__(self, func: Callable):
        self.func = func
        self.name = getattr(func, "name", None) or getattr(func, "__name__", "tool")
        self.__name__ = self.name
        doc = getattr(func, "__doc__", "") or ""
        self.description = doc.strip().split("\n")[0] if doc else f"Tool: {self.name}"
        self.is_long_running = False

    def __call__(self, *args, **kwargs):
        return self.func(*args, **kwargs)

    def __repr__(self) -> str:  # pragma: no cover
        return f"FunctionTool({self.name})"


class ToolContext:
    """Minimal stand-in for ``google.adk.tools.ToolContext``.

    google-adk passes a rich per-call context (state + artifact service) to
    tools. Standalone, tools that accept ``tool_context`` still import and run;
    artifact persistence is a no-op unless a real context is supplied.
    """

    def __init__(self, state: dict | None = None, **_kw):
        self.state: dict = state if state is not None else {}

    async def save_artifact(self, *_args, **_kwargs):  # pragma: no cover
        return None

    async def load_artifact(self, *_args, **_kwargs):  # pragma: no cover
        return None


def _coerce_arguments(fn: Callable, arguments: dict) -> dict:
    """Coerce LLM-supplied args toward each parameter's annotated type.

    LLMs frequently pass an int as a list or string (``limit=["5"]``,
    ``angles="3 angles"``); calling the tool then raises ``int() ... not 'list'``
    mid-loop. This conservatively coerces int/float/bool/str params and leaves
    everything else untouched. Never raises — returns the args as-is on any problem.
    """
    try:
        # Same postponed-annotations issue as _extract_parameters: modules with
        # `from __future__ import annotations` store hints as strings, so a raw
        # `__annotations__` lookup never matches _coerce_value's real type-object
        # checks and this coercion silently no-ops for every builtin tool.
        import typing
        try:
            hints = typing.get_type_hints(fn) or {}
        except Exception:
            hints = getattr(fn, "__annotations__", {}) or {}
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return arguments
    out = dict(arguments)
    for pname, value in list(out.items()):
        if pname in params:
            out[pname] = _coerce_value(value, hints.get(pname))
    return out


def _coerce_value(value, hint):
    """Best-effort coerce a single value toward an int/float/bool/str hint."""
    if hint not in (int, float, bool, str) or value is None:
        return value
    if isinstance(value, hint) and not (hint is int and isinstance(value, bool)):
        return value
    # Models often wrap a scalar in a list/tuple, e.g. limit=["5"] or angles=["a","b"].
    if isinstance(value, (list, tuple, set)):
        seq = list(value)
        if hint in (int, float):
            value = seq[0] if len(seq) == 1 else len(seq)  # one→that number; many→count
        else:
            value = seq[0] if len(seq) == 1 else " ".join(map(str, seq))
    try:
        if hint is str:
            return str(value)
        if hint is bool:
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "y", "on")
            return bool(value)
        if hint is int:
            if isinstance(value, str):
                import re as _re
                m = _re.search(r"-?\d+", value)
                return int(m.group()) if m else value
            return int(value)
        if hint is float:
            return float(value)
    except (TypeError, ValueError):
        return value
    return value


def _extract_parameters(fn: Callable) -> dict:
    """Extract JSON Schema parameters from function signature and type hints."""
    sig = inspect.signature(fn)
    # `get_type_hints` (not raw `__annotations__`) is required: modules using
    # `from __future__ import annotations` (e.g. builtin_tools.py) store hints
    # as unevaluated strings ("int", not the int type), so a raw dict lookup
    # against _type_to_schema's type-object keys always misses and silently
    # degrades every non-str param to {"type": "string"} — verified live: this
    # broke file_read's start_line/end_line (declared `int`), causing the model
    # to pass "0"/"1" strings straight into `start_line - 1` and crash with
    # "unsupported operand type(s) for -: 'str' and 'int'" on every call that
    # used them.
    import typing
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        hints = getattr(fn, "__annotations__", {})

    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue

        hint = hints.get(param_name, str)
        prop = _type_to_schema(hint)
        prop_desc = ""

        # Try to extract from docstring
        doc = fn.__doc__ or ""
        for line in doc.split("\n"):
            stripped = line.strip()
            if stripped.startswith(f"{param_name}:") or stripped.startswith(f"{param_name} "):
                prop_desc = stripped.split(":", 1)[-1].strip() if ":" in stripped else ""
                break

        if prop_desc:
            prop["description"] = prop_desc

        properties[param_name] = prop

        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    schema: dict = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


def _type_to_schema(hint) -> dict:
    """Convert a Python type hint to a JSON Schema type."""
    type_map = {
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
    }
    if hint in type_map:
        return dict(type_map[hint])

    origin = getattr(hint, "__origin__", None)
    if origin is list:
        args = getattr(hint, "__args__", (str,))
        return {"type": "array", "items": _type_to_schema(args[0] if args else str)}
    if origin is dict:
        return {"type": "object"}

    return {"type": "string"}
