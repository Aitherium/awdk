"""Declarative agent loading from YAML / JSON / dict.

Lets users define agents as data files instead of writing Python::

    # research.yaml
    name: research
    instructions: Find facts and cite sources.
    model: openai:gpt-4o-mini
    capabilities: [NET_HTTP, LLM_INFERENCE]
    memory:
      kind: file
      path: ./.aither/research.jsonl
    tools:
      - aither_adk.contrib.tools.web:search
      - mypkg.tools:summarize

    # python
    agent = Agent.from_file("research.yaml")
    result = await agent.run("What is the boiling point of mercury?")

Model strings use the ``"<backend>:<model>"`` form, e.g.
``"openai:gpt-4o-mini"``, ``"ollama:llama3.1:8b"``, ``"anthropic:claude-3-5-sonnet"``,
``"deepseek:deepseek-chat"``, ``"vllm:auto"``. ``"auto"`` runs :func:`auto_backend`.

Tools are loaded by dotted path. The target must be either a :class:`Tool`
instance or a function decorated with :func:`tool`.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import Any

from adk.core.agent import Agent
from adk.core.capability import Capability
from adk.core.memory import InMemoryStore, Memory
from adk.core.model import ModelBackend, auto_backend
from adk.core.persistence import FileStore
from adk.core.tool import Tool


class AgentSpecError(ValueError):
    """Raised when an agent spec is malformed."""


def load_agent(spec: dict[str, Any] | str | Path) -> Agent:
    """Build an :class:`Agent` from a dict or a YAML/JSON file path."""
    if isinstance(spec, (str, Path)):
        return _load_file(Path(spec))
    return _build(spec)


def _load_file(path: Path) -> Agent:
    if not path.exists():
        raise AgentSpecError(f"agent spec file not found: {path}")
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as e:
            raise AgentSpecError("PyYAML required for YAML specs") from e
        data = yaml.safe_load(raw)
    elif path.suffix.lower() == ".json":
        data = json.loads(raw)
    else:
        raise AgentSpecError(f"unsupported spec extension: {path.suffix}")
    if not isinstance(data, dict):
        raise AgentSpecError("agent spec must be a mapping at the top level")
    return _build(data)


def _build(spec: dict[str, Any]) -> Agent:
    name = spec.get("name")
    if not name or not isinstance(name, str):
        raise AgentSpecError("agent spec requires a string 'name'")
    instructions = spec.get("instructions", "") or ""
    if not isinstance(instructions, str):
        raise AgentSpecError("'instructions' must be a string")

    model = _resolve_model(spec.get("model"))
    tools = _resolve_tools(spec.get("tools") or [])
    memory = _resolve_memory(spec.get("memory"))
    capabilities = _resolve_capabilities(spec.get("capabilities") or [])

    return Agent(
        name=name,
        model=model,
        instructions=instructions,
        tools=tools,
        memory=memory,
        capabilities=capabilities,
    )


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------

_BACKEND_CLASSES: dict[str, str] = {
    "openai": "adk.core.backends.openai_compat:OpenAIBackend",
    "vllm": "adk.core.backends.openai_compat:VLLMBackend",
    "deepseek": "adk.core.backends.openai_compat:DeepSeekBackend",
    "anthropic": "adk.core.backends.anthropic:AnthropicBackend",
    "ollama": "adk.core.backends.ollama:OllamaBackend",
}

# Env-var fallbacks for backends that require credentials. Loader fills these
# in automatically so a spec like ``model: openai:gpt-4o-mini`` Just Works
# when ``OPENAI_API_KEY`` is set — matching the Claude Code / OpenAI SDK UX.
_BACKEND_ENV_KWARGS: dict[str, dict[str, str]] = {
    "openai": {"api_key": "OPENAI_API_KEY"},
    "deepseek": {"api_key": "DEEPSEEK_API_KEY"},
    "anthropic": {"api_key": "ANTHROPIC_API_KEY"},
    "vllm": {"base_url": "VLLM_BASE_URL", "api_key": "VLLM_API_KEY"},
    "ollama": {"base_url": "OLLAMA_HOST"},
}


def _resolve_model(value: Any) -> ModelBackend:
    if value is None or value == "auto":
        return auto_backend()
    # Duck-type instead of isinstance — ModelBackend is a Protocol without
    # @runtime_checkable, and we want to accept any backend-like object.
    if hasattr(value, "generate") and not isinstance(value, str):
        return value
    if not isinstance(value, str):
        raise AgentSpecError(f"'model' must be a string, got {type(value).__name__}")
    if ":" not in value:
        raise AgentSpecError(
            f"model spec must be '<backend>:<model>' or 'auto', got {value!r}"
        )
    backend, _, model_name = value.partition(":")
    backend = backend.lower()
    path = _BACKEND_CLASSES.get(backend)
    if not path:
        raise AgentSpecError(
            f"unknown backend {backend!r}. "
            f"Choices: {', '.join(sorted(_BACKEND_CLASSES))}"
        )
    cls = _import_dotted(path)
    kwargs: dict[str, Any] = {}
    for kw, env in _BACKEND_ENV_KWARGS.get(backend, {}).items():
        env_val = os.environ.get(env)
        if env_val:
            kwargs[kw] = env_val
    if model_name:
        kwargs["model"] = model_name
    try:
        return cls(**kwargs)
    except TypeError as e:
        raise AgentSpecError(
            f"cannot construct {backend} backend: {e}. "
            f"Set required env vars: {list(_BACKEND_ENV_KWARGS.get(backend, {}).values())}"
        ) from e


def _resolve_tools(items: list[Any]) -> list[Tool]:
    tools: list[Tool] = []
    for entry in items:
        if isinstance(entry, Tool):
            tools.append(entry)
            continue
        if not isinstance(entry, str):
            raise AgentSpecError(
                f"tool entries must be dotted paths or Tool instances, got {entry!r}"
            )
        obj = _import_dotted(entry)
        if not isinstance(obj, Tool):
            raise AgentSpecError(
                f"tool path {entry!r} did not resolve to a Tool (got {type(obj).__name__})"
            )
        tools.append(obj)
    return tools


def _resolve_memory(spec: Any) -> Memory:
    if spec is None:
        return InMemoryStore()
    if isinstance(spec, Memory):  # pragma: no cover - protocol check
        return spec
    if not isinstance(spec, dict):
        raise AgentSpecError("'memory' must be a mapping with a 'kind' key")
    kind = (spec.get("kind") or "memory").lower()
    if kind in ("memory", "inmemory", "in_memory"):
        return InMemoryStore()
    if kind == "file":
        path = spec.get("path")
        if not path:
            raise AgentSpecError("file memory requires a 'path'")
        return FileStore(path)
    if kind == "typed":
        # Authority/activation layer over an inner store. `backing` is another
        # memory spec (defaults to in-memory); recall tuning is read by the
        # agent loop, not the store, so it's accepted but not required here.
        from adk.core.typed_memory import TypedMemory
        backing_spec = spec.get("backing")
        backing = _resolve_memory(backing_spec) if backing_spec is not None else InMemoryStore()
        return TypedMemory(backing)
    raise AgentSpecError(f"unknown memory kind: {kind!r}")


def _resolve_capabilities(items: list[Any]) -> set[Capability]:
    out: set[Capability] = set()
    for item in items:
        if isinstance(item, Capability):
            out.add(item)
            continue
        if not isinstance(item, str):
            raise AgentSpecError(f"capability entries must be names or enums, got {item!r}")
        try:
            out.add(Capability[item.upper()])
        except KeyError as e:
            valid = ", ".join(c.name for c in Capability)
            raise AgentSpecError(
                f"unknown capability {item!r}. Valid: {valid}"
            ) from e
    return out


def _import_dotted(path: str) -> Any:
    if ":" in path:
        module_path, _, attr = path.partition(":")
    else:
        module_path, _, attr = path.rpartition(".")
        if not module_path:
            raise AgentSpecError(f"dotted path missing module: {path!r}")
    try:
        mod = importlib.import_module(module_path)
    except ImportError as e:
        raise AgentSpecError(f"cannot import {module_path!r}: {e}") from e
    try:
        return getattr(mod, attr)
    except AttributeError as e:
        raise AgentSpecError(
            f"{module_path!r} has no attribute {attr!r}"
        ) from e
