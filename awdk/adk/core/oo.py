"""Object-oriented agents: the class IS the agent.

Adapted from the NVIDIA NOOA design (labs-OO-Agents, Apache-2.0): an agent is
an ordinary Python class where

- the **class docstring** is the agent's instructions,
- **fields** are state,
- **plain methods** are deterministic capabilities (auto-exposed as tools),
- **async methods whose body is `...`** are *agentic*: calling one dispatches
  to an LLM loop, with the method docstring as the task prompt and the return
  annotation as a typed contract (validated, with retry).

This module is the thin keystone over primitives adk already ships: ellipsis
detection (:mod:`adk.ellipsis`), typed single-shot prediction
(:class:`adk.core.agent.PredictLoop`), iterative strategies
(:class:`adk.core.agent.ReActLoop`, :class:`adk.core.codeact.CodeActLoop`),
and visibility markers (:mod:`adk.agentdoc`). It deliberately does NOT port
NOOA's actor runtime, event sourcing, or snapshot planes.

Example::

    class SupportAgent(OOAgent, model=backend):
        \"\"\"You are a support agent for AcmeCo.\"\"\"

        refund_window_days: int = 30

        def is_refund_eligible(self, days_since_delivery: int) -> bool:
            return days_since_delivery <= self.refund_window_days

        async def triage(self, message: str) -> Ticket:
            \"\"\"Create a support ticket for the message.\"\"\"
            ...

    agent = SupportAgent()
    ticket = await agent.triage("my order never arrived")   # -> Ticket

Rules carried over from NOOA's docstring doctrine:

- Never template raw parameter values (``{message}``) into the docstring —
  arguments are rendered to the model automatically. Only ``{self.attr}``
  expressions are expanded, for instance state the signature cannot show.
- One method = one LLM task. Orchestrator methods have real bodies.
- ``_private`` methods and ``@hidden``-decorated methods are not exposed.
"""

from __future__ import annotations

import inspect
import re
from typing import Any, get_type_hints

from pydantic import BaseModel, RootModel

from adk.core.agent import Agent, AgentLoop, AgentResult, PredictLoop
from adk.core.capability import use_context
from adk.core.logging import get_logger
from adk.core.model import ModelBackend
from adk.core.tool import Tool
from adk.ellipsis import get_strategy_meta, has_ellipsis_body

try:
    from adk.agentdoc.visibility import is_hidden_method
except ImportError:  # agentdoc is part of the wheel, but stay import-safe
    def is_hidden_method(func: Any) -> bool:
        return False

try:
    from adk.agentdoc import truncating_pformat as _pformat_value
except ImportError:
    def _pformat_value(value: Any, **_kw: Any) -> str:
        return repr(value)

_log = get_logger("oo")

# Attribute markers set on wrapped agentic methods.
_AGENTIC_MARKER = "_oo_agentic"
_ORIGINAL_ATTR = "_oo_original"

# Framework names that must never become tools, even though they are public
# methods on the Agent base class.
_FRAMEWORK_NAMES = frozenset(
    {"run", "grant", "capabilities", "model", "tools", "memory", "loop", "tracer"}
)

_SELF_EXPR = re.compile(r"\{(self(?:\.[A-Za-z_]\w*)+)\}")

_ARG_PREVIEW_CHARS = 4000

_API_DOC_MAX_CHARS = 6000


class AgenticReturnError(RuntimeError):
    """The model's answer never validated against the method's return type."""


def _render_self_exprs(doc: str, agent: Any) -> str:
    """Expand ``{self.attr}`` expressions in a docstring; leave all else alone.

    Only dotted attribute chains rooted at ``self`` are evaluated — a
    ``{message}`` parameter template is deliberately NOT expanded (the
    framework already renders arguments; re-injecting raw values is the
    documented NOOA anti-pattern). Failures leave the expression literal.
    """

    def _sub(match: re.Match[str]) -> str:
        expr = match.group(1)
        obj: Any = agent
        try:
            for part in expr.split(".")[1:]:
                obj = getattr(obj, part)
            return str(obj)
        except AttributeError:
            return match.group(0)

    return _SELF_EXPR.sub(_sub, doc)


def _preview(value: Any) -> str:
    try:
        text = _pformat_value(value)
    except Exception:  # noqa: BLE001 — rendering must never break dispatch
        text = repr(value)
    if len(text) > _ARG_PREVIEW_CHARS:
        text = text[:_ARG_PREVIEW_CHARS] + f"... (truncated, len={len(text)})"
    return text


def _agent_api_doc(agent: Any) -> str:
    """agentdoc-rendered API contract of the agent, bounded; "" on any failure.

    Iterative loops (ReAct/CodeAct) get this appended to the task so the model
    sees typed signatures and current field values — not just tool names.
    ``@hidden`` / ``_private`` filtering is agentdoc's, so visibility decisions
    stay in one place.
    """
    try:
        from adk.agentdoc import doc

        text = doc(agent)
    except Exception:  # noqa: BLE001 — the contract is an aid, never a crash
        return ""
    if not isinstance(text, str):
        text = str(text)
    if len(text) > _API_DOC_MAX_CHARS:
        text = text[:_API_DOC_MAX_CHARS] + f"\n... (truncated, len={len(text)})"
    return text


def _resolve_return_type(func: Any) -> Any:
    """Best-effort resolved return annotation, or None when untyped/str."""
    try:
        hints = get_type_hints(func)
        rt = hints.get("return")
    except Exception as exc:  # noqa: BLE001 — unresolvable forward refs → untyped
        rt = func.__annotations__.get("return") if hasattr(func, "__annotations__") else None
        if isinstance(rt, str):
            # The contract silently degrading to str would be invisible — say so.
            _log.warning(
                "oo.return_type.unresolved",
                extra={"method": getattr(func, "__qualname__", "?"), "err": str(exc)},
            )
            rt = None
    if rt in (None, type(None), str, Any):
        return None
    return rt


def _make_agentic_wrapper(original: Any) -> Any:
    """Wrap an async ``...``-bodied method into an LLM-dispatched call."""
    sig = inspect.signature(original)
    doc = inspect.getdoc(original) or ""
    method_name = original.__name__

    async def wrapper(self: "OOAgent", *args: Any, **kwargs: Any) -> Any:
        bound = sig.bind(self, *args, **kwargs)
        bound.apply_defaults()
        arguments = {k: v for k, v in bound.arguments.items() if k != "self"}

        return_type = _resolve_return_type(original)
        output_model: type[BaseModel] | None = None
        unwrap_root = False
        if return_type is not None:
            if isinstance(return_type, type) and issubclass(return_type, BaseModel):
                output_model = return_type
            else:
                output_model = RootModel[return_type]  # type: ignore[valid-type]
                unwrap_root = True

        # Compose the task prompt: docstring (with {self.attr} expanded),
        # then rendered arguments, then the output contract.
        parts: list[str] = []
        task = _render_self_exprs(doc, self) if doc else f"Perform the task: {method_name}."
        parts.append(task)
        if arguments:
            arg_lines = "\n".join(f"- {k} = {_preview(v)}" for k, v in arguments.items())
            parts.append(f"Arguments:\n{arg_lines}")
        if output_model is not None:
            import json as _json

            schema = _json.dumps(output_model.model_json_schema())
            parts.append(
                "Respond ONLY with a JSON value conforming to this JSON schema "
                f"(no prose, no code fences):\n{schema}"
            )
        prompt = "\n\n".join(parts)

        # Per-method loop/model overrides from @strategy, else typed Predict,
        # else the agent's default loop.
        meta = get_strategy_meta(original)
        loop: AgentLoop | None = meta.get("loop")
        model_override: ModelBackend | None = meta.get("model")
        if loop is None:
            loop = PredictLoop(model=model_override, output_model=output_model)
            loop_is_typed_predict = True
        else:
            loop_is_typed_predict = False
            # Iterative loops act on the live object — show them its typed API.
            api_doc = _agent_api_doc(self)
            if api_doc:
                parts.append(f"Your API (live objects available to your code):\n{api_doc}")
                prompt = "\n\n".join(parts)

        _log.info(
            "oo.agentic.call",
            extra={"agent": self.name, "method": method_name, "typed": bool(output_model)},
        )
        with use_context(self.capabilities):
            result: AgentResult = await loop.run(self, prompt)

        if output_model is None:
            return result.output

        if loop_is_typed_predict and result.finish_reason == "validation_failed":
            raise AgenticReturnError(
                f"{type(self).__name__}.{method_name}: model output never validated "
                f"against {getattr(return_type, '__name__', return_type)!r}: "
                f"{result.output[:500]}"
            )
        try:
            validated = output_model.model_validate_json(result.output)
        except Exception as exc:
            raise AgenticReturnError(
                f"{type(self).__name__}.{method_name}: loop output does not match "
                f"declared return type {getattr(return_type, '__name__', return_type)!r}"
            ) from exc
        return validated.root if unwrap_root else validated

    wrapper.__name__ = method_name
    wrapper.__qualname__ = getattr(original, "__qualname__", method_name)
    wrapper.__doc__ = original.__doc__
    setattr(wrapper, _AGENTIC_MARKER, True)
    setattr(wrapper, _ORIGINAL_ATTR, original)
    return wrapper


_JSON_TYPES: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    tuple: "array",
    set: "array",
    dict: "object",
}


def _json_type(annotation: Any) -> str:
    """JSON-schema type name for a parameter annotation; "string" when unknown."""
    if annotation is None:
        return "string"
    import typing as _t

    origin = _t.get_origin(annotation)
    if origin is not None:
        return _JSON_TYPES.get(origin, "string")
    return _JSON_TYPES.get(annotation, "string")


class _BoundMethodTool(Tool):
    """A deterministic agent method exposed as a Tool for iterative loops."""

    def __init__(self, bound_method: Any) -> None:
        func = getattr(bound_method, "__func__", bound_method)
        doc = inspect.getdoc(func) or ""
        super().__init__(
            name=func.__name__,
            description=doc.splitlines()[0] if doc else func.__name__,
        )
        self._bound = bound_method
        self._func = func

    def call(self, **kwargs: Any) -> Any:
        return self._bound(**kwargs)

    def schema(self) -> dict[str, Any]:
        # Same signature-derived shape as @tool, minus the self parameter.
        # Parameter types come from the annotations, not a blanket "string".
        properties: dict[str, Any] = {}
        required: list[str] = []
        sig = inspect.signature(self._bound)
        try:
            hints = get_type_hints(self._func)
        except Exception:  # noqa: BLE001 — unresolvable hints → untyped params
            hints = {}
        for pname, param in sig.parameters.items():
            if pname in ("self", "cls"):
                continue
            properties[pname] = {"type": _json_type(hints.get(pname))}
            if param.default is inspect.Parameter.empty:
                required.append(pname)
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }


class OOAgent(Agent):
    """Subclass-first agent: docstring = instructions, methods = capabilities.

    Unlike :class:`Agent` ("compose; don't subclass"), this base class exists
    to be subclassed — that inversion is the point of the OO model. Class
    creation wraps every async ``...``-bodied method for LLM dispatch;
    instantiation turns remaining public methods into tools.

    Class kwargs::

        class MyAgent(OOAgent, model=backend, loop=ReActLoop()):
            ...

    ``model`` may instead be passed at instantiation. Everything else the
    plain :class:`Agent` constructor accepts is forwarded.
    """

    _oo_default_model: ModelBackend | None = None
    _oo_default_loop: AgentLoop | None = None

    def __init_subclass__(
        cls,
        *,
        model: ModelBackend | None = None,
        loop: AgentLoop | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init_subclass__(**kwargs)
        if model is not None:
            cls._oo_default_model = model
        if loop is not None:
            cls._oo_default_loop = loop
        for attr_name, attr_value in list(vars(cls).items()):
            if getattr(attr_value, _AGENTIC_MARKER, False):
                continue  # already wrapped (re-derived class)
            if inspect.iscoroutinefunction(attr_value) and has_ellipsis_body(attr_value):
                setattr(cls, attr_name, _make_agentic_wrapper(attr_value))

    def __init__(
        self,
        *,
        model: ModelBackend | None = None,
        name: str | None = None,
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> None:
        resolved_model = model or type(self)._oo_default_model
        if resolved_model is None:
            raise ValueError(
                f"{type(self).__name__}: no model. Pass model= at class definition "
                f"(class {type(self).__name__}(OOAgent, model=...)) or instantiation."
            )
        instructions = inspect.getdoc(type(self)) or ""
        if instructions == inspect.getdoc(OOAgent):
            instructions = ""
        loop = kwargs.pop("loop", None) or type(self)._oo_default_loop
        super().__init__(
            name=name or type(self).__name__,
            model=resolved_model,
            instructions=instructions,
            tools=list(tools or []),
            **({"loop": loop} if loop is not None else {}),
            **kwargs,
        )
        self.tools.extend(self._collect_method_tools())

    def _collect_method_tools(self) -> list[Tool]:
        """Public non-agentic methods declared below OOAgent become tools."""
        collected: list[Tool] = []
        seen: set[str] = set()
        for klass in type(self).__mro__:
            if klass in (OOAgent, Agent, object):
                break
            for attr_name, attr_value in vars(klass).items():
                if attr_name.startswith("_") or attr_name in seen:
                    continue
                seen.add(attr_name)
                if attr_name in _FRAMEWORK_NAMES:
                    continue
                if not (inspect.isfunction(attr_value) or inspect.iscoroutinefunction(attr_value)):
                    continue
                if getattr(attr_value, _AGENTIC_MARKER, False):
                    continue
                if is_hidden_method(attr_value):
                    continue
                collected.append(_BoundMethodTool(getattr(self, attr_name)))
        return collected


__all__ = [
    "AgenticReturnError",
    "OOAgent",
]
