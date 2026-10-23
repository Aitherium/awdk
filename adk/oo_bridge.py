"""Bridge the OO agent model onto the production adk plane.

:class:`adk.core.oo.OOAgent` runs on the *core* primitives (``ModelBackend``,
``adk.core.tool.Tool``); production agents run on the deployed plane
(``adk.llm.LLMRouter`` for models, ``adk.tools.ToolRegistry`` for tools).
This module is the seam that lets an OO agent be written once and served
with full production capability:

    from adk import OOAgent
    from adk.oo_bridge import RouterBackend, tools_from_registry

    class Atlas(OOAgent):
        \"\"\"You are Atlas, the service-discovery agent.\"\"\"

        async def survey(self, service: str) -> Report:
            \"\"\"Survey the service and report its state.\"\"\"
            ...

    agent = Atlas(
        model=RouterBackend(router),
        tools=tools_from_registry(registry),
    )

Clearance is honored fail-closed: a registry tool that declares
``required_clearance > 0`` is NOT bridged unless the caller explicitly
grants a clearance level that covers it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from adk.core.logging import get_logger
from adk.core.model import Message as CoreMessage
from adk.core.model import ModelResponse
from adk.core.oo import OOAgent
from adk.core.tool import Tool

_log = get_logger("oo_bridge")


class RouterBackend:
    """Core :class:`ModelBackend` over the production :class:`LLMRouter`.

    Lets an OOAgent run on the same routed model plane as every deployed
    agent (the router fronts the platform scheduler / configured provider).

    Args:
        router: An ``adk.llm.LLMRouter`` (or any provider with an async
            ``chat(messages, ...)`` returning an ``LLMResponse``).
        model: Optional model name forwarded to the router per call.
    """

    name = "router"

    def __init__(self, router: Any, *, model: str | None = None) -> None:
        self._router = router
        self.model = model or getattr(router, "default_model", "") or "router"

    async def generate(
        self,
        messages: list[CoreMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **opts: Any,
    ) -> ModelResponse:
        from adk.llm.base import Message as LLMMessage

        conv = [LLMMessage(role=m.role, content=m.content) for m in messages]
        resp = await self._router.chat(
            conv,
            model=self.model if self.model != "router" else None,
            temperature=temperature,
            max_tokens=max_tokens or 4096,
        )
        return ModelResponse(
            text=resp.content,
            model=resp.model or self.model,
            finish_reason=resp.finish_reason or "stop",
        )

    async def stream(self, messages: list[CoreMessage], **opts: Any):
        # Real incremental delivery via the router's chat_stream (StreamChunk
        # objects); a router without one falls back to a single-chunk stream.
        chat_stream = getattr(self._router, "chat_stream", None)
        if chat_stream is None:
            resp = await self.generate(messages, **opts)
            yield resp.text
            return
        from adk.llm.base import Message as LLMMessage

        conv = [LLMMessage(role=m.role, content=m.content) for m in messages]
        async for chunk in chat_stream(
            conv, model=self.model if self.model != "router" else None
        ):
            text = getattr(chunk, "content", None)
            if text:
                yield text


class RegistryTool(Tool):
    """A production :class:`ToolDef` exposed as a core :class:`Tool`."""

    def __init__(self, tool_def: Any) -> None:
        super().__init__(name=tool_def.name, description=tool_def.description)
        self._def = tool_def

    def call(self, **kwargs: Any) -> Any:
        # Tool.__call__ awaits awaitables, so async ToolDef fns work unchanged.
        return self._def.fn(**kwargs)

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self._def.parameters or {"type": "object", "properties": {}},
        }


def tools_from_registry(
    registry: Any,
    *,
    clearance: int = 0,
    allow: Iterable[str] | None = None,
) -> list[Tool]:
    """Bridge a :class:`ToolRegistry` into core Tools an OOAgent can use.

    Fail-closed on clearance: a tool whose ``required_clearance`` exceeds
    the granted ``clearance`` is skipped (and logged), never silently
    exposed. The default grants level 0 — public tools only.

    Args:
        registry: An ``adk.tools.ToolRegistry`` (anything with ``list_tools()``).
        clearance: Granted clearance level (default 0 = public tools only).
        allow: Optional name whitelist. ``None`` bridges every tool the
            clearance permits; an iterable (even an empty one) bridges ONLY
            the named tools — the default-deny contract of a brain pack's
            ``tools:`` list.

    Returns:
        The bridged tools, in registry order.
    """
    allowed = None if allow is None else frozenset(allow)
    bridged: list[Tool] = []
    skipped: list[str] = []
    for tool_def in registry.list_tools():
        if allowed is not None and tool_def.name not in allowed:
            continue
        if getattr(tool_def, "required_clearance", 0) > clearance:
            skipped.append(tool_def.name)
            continue
        bridged.append(RegistryTool(tool_def))
    if skipped:
        _log.info(
            "oo_bridge.tools.clearance_skipped",
            extra={"count": len(skipped), "names": skipped[:20]},
        )
    return bridged


# ── brain packs → OOAgent shells ──────────────────────────────────────────────
# A brain pack (``brain_pack.yaml``: ``identity`` + ``system_prompt`` + a
# ``tools:`` whitelist) is how most production identities are declared today.
# These functions give every such pack a generated OOAgent class, so a pack
# agent runs on the OO model with no hand rewrite; a hand-written subclass of
# the generated class can then add typed ``...`` methods one at a time.

BRAIN_PACK_FILENAME = "brain_pack.yaml"


def load_brain_pack(pack: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    """Return a brain pack as a dict.

    Args:
        pack: A mapping (returned as a copy), a ``brain_pack.yaml`` path, or a
            directory that contains one.

    Returns:
        The parsed pack.

    Raises:
        FileNotFoundError: The path (or ``<dir>/brain_pack.yaml``) is absent.
        ValueError: The file is not a YAML mapping.
    """
    if isinstance(pack, Mapping):
        return dict(pack)
    path = Path(pack)
    if path.is_dir():
        path = path / BRAIN_PACK_FILENAME
    if not path.is_file():
        raise FileNotFoundError(f"no brain pack at {path}")
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(
            f"{path}: a brain pack must be a YAML mapping, got {type(data).__name__}"
        )
    return data


def _pack_identity(spec: Mapping[str, Any]) -> str:
    for key in ("identity", "id", "name", "app_name"):
        value = spec.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "brain-pack"


def _class_name(identity: str) -> str:
    parts = "".join(c if c.isalnum() else " " for c in identity).split()
    name = "".join(p[:1].upper() + p[1:] for p in parts) or "BrainPack"
    if name[0].isdigit():
        name = "Pack" + name
    return name + "Agent"


def brain_pack_tool_whitelist(spec: Mapping[str, Any]) -> tuple[str, ...]:
    """The pack's ``tools:`` list as names — DEFAULT-DENY.

    A list (even ``[]``) is the whitelist. A pack with no ``tools:`` key gets
    the empty whitelist too: a pack that names no tools is granted none, the
    contract the managed-agent compiler already enforces. Entries may be
    plain names or ``{name: ...}`` mappings.

    Raises:
        ValueError: ``tools`` is present but not a list.
    """
    raw = spec.get("tools")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"brain pack 'tools' must be a list, got {type(raw).__name__}")
    names: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, Mapping):
            name = str(entry.get("name") or entry.get("id") or "")
        else:
            name = ""
        name = name.strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def oo_class_from_brain_pack(
    pack: str | Path | Mapping[str, Any],
    *,
    base: type[OOAgent] = OOAgent,
) -> type[OOAgent]:
    """Generate an :class:`OOAgent` subclass from a brain pack.

    The pack's ``system_prompt`` becomes the class docstring (= the agent's
    instructions), its identity becomes the class name, and the class carries
    ``brain_pack`` (the parsed spec), ``brain_pack_identity`` and
    ``tool_whitelist``. Subclass the result to add typed agentic methods::

        Hera = oo_class_from_brain_pack("my-pack/")

        class Growth(Hera):
            async def plan_campaign(self, goal: str) -> Plan:
                ...

    Args:
        pack: Mapping, ``brain_pack.yaml`` path, or a directory holding one.
        base: The OOAgent subclass to derive from.

    Returns:
        The generated class.

    Raises:
        ValueError: ``system_prompt`` is not a string or ``tools`` not a list.
    """
    spec = load_brain_pack(pack)
    identity = _pack_identity(spec)
    prompt = spec.get("system_prompt") or ""
    if not isinstance(prompt, str):
        raise ValueError("brain pack 'system_prompt' must be a string")
    attrs: dict[str, Any] = {
        "__doc__": prompt.strip() or f"You are {identity}.",
        "__module__": __name__,
        "brain_pack": spec,
        "brain_pack_identity": identity,
        "tool_whitelist": brain_pack_tool_whitelist(spec),
    }
    return type(_class_name(identity), (base,), attrs)


def oo_agent_from_brain_pack(
    pack: str | Path | Mapping[str, Any],
    *,
    model: Any,
    registry: Any = None,
    clearance: int = 0,
    name: str | None = None,
    base: type[OOAgent] = OOAgent,
    **kwargs: Any,
) -> OOAgent:
    """Instantiate a brain pack as a running :class:`OOAgent`.

    Registry tools are bridged through :func:`tools_from_registry` with the
    pack's ``tools:`` list as the whitelist and ``clearance`` fail-closed.
    Whitelisted names the registry does not have are logged and recorded on
    the instance as ``missing_tools`` — a pack advertising a tool nobody
    registered is reported, never silently shrunk.

    Args:
        pack: Mapping, ``brain_pack.yaml`` path, or a directory holding one.
        model: A core ``ModelBackend`` (wrap a production router with
            :class:`RouterBackend`).
        registry: Optional ``ToolRegistry`` to bridge tools from.
        clearance: Granted clearance level for registry tools.
        name: Agent name (defaults to the pack identity).
        base: The OOAgent subclass to derive from.
        **kwargs: Forwarded to the agent constructor.

    Returns:
        The constructed agent.
    """
    cls: Any = oo_class_from_brain_pack(pack, base=base)
    whitelist: tuple[str, ...] = cls.tool_whitelist
    tools: list[Tool] = []
    if registry is not None:
        tools = tools_from_registry(registry, clearance=clearance, allow=whitelist)
        known = {t.name for t in registry.list_tools()}
        missing = [n for n in whitelist if n not in known]
    else:
        missing = list(whitelist)
    if missing:
        _log.warning(
            "oo_bridge.brain_pack.tools_missing",
            extra={"identity": cls.brain_pack_identity, "names": missing[:20]},
        )
    agent = cls(model=model, name=name or cls.brain_pack_identity, tools=tools, **kwargs)
    agent.missing_tools = missing
    return agent


__all__ = [
    "BRAIN_PACK_FILENAME",
    "RegistryTool",
    "RouterBackend",
    "brain_pack_tool_whitelist",
    "load_brain_pack",
    "oo_agent_from_brain_pack",
    "oo_class_from_brain_pack",
    "tools_from_registry",
]
