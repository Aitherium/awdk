"""Unified Gemini ``v1beta`` and OpenAI ``v1`` + Assistants runtime for declarative bots.

Supports:
- ``backend="mock"`` (``MockBotBackend``, deterministic offline responder with
  knowledge grounding and tool-call execution)
- Injected callable or ``LLMProvider`` / ``LLMRouter`` backends
- Real ``adk.llm`` backends (``gemini``, ``openai``, ``aither``, ``llamacpp``, ``vllm``, ``auto``)
  with explicit ``gemini``/``google`` key normalization and clean ``503`` when unreachable
- FastAPI ``APIRouter`` via ``mount_bot_routes`` and standalone ``create_bot_app``
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import math
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from adk.bots.actions import ActionExecutor, load_openapi_actions
from adk.bots.knowledge import KnowledgeHit, KnowledgeIndex
from adk.bots.manifest import BotManifest, load_bot_manifest
from adk.bots.onboard import resolve_provider_key
from adk.bots.ui import render_playground_html
from adk.bots.validator import _resolve_within
from adk.llm.base import LLMProvider, LLMResponse, Message, StreamChunk, ToolCall
from adk.tools import ToolRegistry

_TOOL_MARKER_RE = re.compile(
    r"\[(?:tool|call):([a-zA-Z0-9_.-]+)(?:\s+(\{.*?\}))?\]",
    re.DOTALL,
)
logger = logging.getLogger("adk.bots.runtime")


def _filter_supported_kwargs(
    fn: Callable[..., Any], extra: dict[str, Any]
) -> dict[str, Any]:
    """Return only non-None kwargs accepted by *fn* (or all if *fn* accepts ``**kwargs``)."""
    cleaned = {k: v for k, v in extra.items() if v is not None}
    if not cleaned:
        return {}
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return {}
    params = sig.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return cleaned
    return {k: v for k, v in cleaned.items() if k in params}


class BackendUnavailableError(RuntimeError):
    """Raised when a configured LLM backend is offline or unconfigured."""


@dataclass
class TurnResult:
    text: str
    model: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    executed_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    knowledge_hits: list[KnowledgeHit] = field(default_factory=list)
    finish_reason: str = "stop"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class MockBotBackend(LLMProvider):
    """Deterministic offline ``LLMProvider`` for testing and local preview."""

    def __init__(
        self,
        bot_name: str = "Aither Bot",
        default_model: str = "mock-bot-1",
        responder: Callable[[list[Message], dict[str, Any]], str | LLMResponse] | None = None,
    ) -> None:
        self.bot_name = bot_name
        self.default_model = default_model
        self.responder = responder

    async def list_models(self) -> list[str]:
        return [self.default_model]

    async def health_check(self) -> bool:
        return True

    async def chat(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        top_p: float | None = None,
        repetition_penalty: float | None = None,
        cache: bool = False,
        **kwargs: Any,
    ) -> LLMResponse:
        chosen_model = model or self.default_model
        if self.responder is not None:
            meta: dict[str, Any] = {
                "model": chosen_model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "tools": tools,
                "top_p": top_p,
            }
            for extra_k in ("response_format", "response_mime_type", "response_schema"):
                if extra_k in kwargs and kwargs[extra_k] is not None:
                    meta[extra_k] = kwargs[extra_k]
            raw = self.responder(messages, meta)
            if inspect.isawaitable(raw):
                raw = await raw
            if isinstance(raw, LLMResponse):
                if not raw.model:
                    raw.model = chosen_model
                return raw
            text = str(raw)
            pt = max(1, sum(len(str(m.content).split()) for m in messages))
            ct = max(1, len(text.split()))
            return LLMResponse(
                content=text,
                model=chosen_model,
                prompt_tokens=pt,
                completion_tokens=ct,
                tokens_used=pt + ct,
                finish_reason="stop",
            )

        last_user = ""
        last_tool_msg: Message | None = None
        knowledge_lines: list[str] = []
        for m in messages:
            content_str = m.content if isinstance(m.content, str) else json.dumps(m.content)
            if m.role == "system" and "[KNOWLEDGE CONTEXT]" in content_str:
                after = content_str.split("[KNOWLEDGE CONTEXT]", 1)[1].strip()
                knowledge_lines = [ln.strip() for ln in after.splitlines() if ln.strip()]
            elif m.role == "user":
                last_user = content_str
            elif m.role == "tool":
                last_tool_msg = m

        # If a tool result was just appended after the latest user turn, summarize it.
        if last_tool_msg is not None and messages and messages[-1].role == "tool":
            tool_results = [
                f"{m.name or 'tool'}={m.content}"
                for m in messages
                if m.role == "tool"
            ]
            reply = f"[{self.bot_name}] Tool result: {'; '.join(tool_results)}"
            pt = max(1, sum(len(str(m.content).split()) for m in messages))
            ct = max(1, len(reply.split()))
            return LLMResponse(
                content=reply,
                model=chosen_model,
                prompt_tokens=pt,
                completion_tokens=ct,
                tokens_used=pt + ct,
                finish_reason="stop",
            )

        # Trigger explicit tool calls via `[tool:<name> {"arg": "val"}]` when tools offered.
        offered = {
            t["function"]["name"]
            for t in (tools or [])
            if isinstance(t, dict) and isinstance(t.get("function"), dict) and t["function"].get("name")
        }
        matches = list(_TOOL_MARKER_RE.finditer(last_user))
        if matches and offered:
            calls: list[ToolCall] = []
            for idx, m in enumerate(matches):
                tname = m.group(1)
                if tname not in offered:
                    continue
                args_raw = m.group(2) or "{}"
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
                    args = {}
                calls.append(
                    ToolCall(
                        id=f"call_{uuid.uuid4().hex[:12]}_{idx}",
                        name=tname,
                        arguments=args if isinstance(args, dict) else {},
                    )
                )
            if calls:
                pt = max(1, sum(len(str(m.content).split()) for m in messages))
                return LLMResponse(
                    content="",
                    model=chosen_model,
                    prompt_tokens=pt,
                    completion_tokens=4,
                    tokens_used=pt + 4,
                    tool_calls=calls,
                    finish_reason="tool_calls",
                )

        parts = [f"[{self.bot_name}]"]
        if knowledge_lines:
            parts.append(f"Grounded answer ({knowledge_lines[0]})")
        if last_user.strip():
            parts.append(f"Response to: {last_user.strip()}")
        else:
            parts.append("Ready.")
        reply = " ".join(parts)
        pt = max(1, sum(len(str(m.content).split()) for m in messages))
        ct = max(1, len(reply.split()))
        return LLMResponse(
            content=reply,
            model=chosen_model,
            prompt_tokens=pt,
            completion_tokens=ct,
            tokens_used=pt + ct,
            finish_reason="stop",
        )

    async def chat_stream(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        top_p: float | None = None,
        repetition_penalty: float | None = None,
        cache: bool = False,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        resp = await self.chat(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
            cache=cache,
            **kwargs,
        )
        words = resp.content.split(" ") if resp.content else []
        for idx, w in enumerate(words):
            piece = w if idx == 0 else f" {w}"
            yield StreamChunk(content=piece, done=False, model=resp.model)
        yield StreamChunk(
            content="",
            done=True,
            model=resp.model,
            tool_calls=resp.tool_calls,
            finish_reason=resp.finish_reason,
        )


class _CallableBackendAdapter(LLMProvider):
    """Wraps a plain sync/async ``(messages, **kwargs) -> str | LLMResponse`` callable."""

    def __init__(self, fn: Callable[..., Any], default_model: str) -> None:
        self.fn = fn
        self.default_model = default_model

    async def list_models(self) -> list[str]:
        return [self.default_model]

    async def chat(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        top_p: float | None = None,
        repetition_penalty: float | None = None,
        cache: bool = False,
        **kwargs: Any,
    ) -> LLMResponse:
        chosen_model = model or self.default_model
        sig = inspect.signature(self.fn)
        call_kwargs: dict[str, Any] = {}
        candidate_kwargs: dict[str, Any] = {
            "model": chosen_model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "tools": tools,
            "top_p": top_p,
        }
        for extra_k in ("response_format", "response_mime_type", "response_schema"):
            if extra_k in kwargs and kwargs[extra_k] is not None:
                candidate_kwargs[extra_k] = kwargs[extra_k]
        for k, v in candidate_kwargs.items():
            if k in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                call_kwargs[k] = v

        res = self.fn(messages, **call_kwargs)
        if inspect.isawaitable(res):
            res = await res
        if isinstance(res, LLMResponse):
            if not res.model:
                res.model = chosen_model
            return res
        text = str(res)
        pt = max(1, sum(len(str(m.content).split()) for m in messages))
        ct = max(1, len(text.split()))
        return LLMResponse(
            content=text,
            model=chosen_model,
            prompt_tokens=pt,
            completion_tokens=ct,
            tokens_used=pt + ct,
            finish_reason="stop",
        )


def _load_tools_module(bot_dir: Path, mod_rel: str, registry: ToolRegistry) -> None:
    target, err = _resolve_within(bot_dir, mod_rel)
    if target is None or not target.is_file():
        raise ValueError(f"Invalid tools module {mod_rel!r}: {err or 'missing file'}")
    spec = importlib.util.spec_from_file_location(
        f"adk_bot_tools_{abs(hash(str(target)))}", target
    )
    if spec is None or spec.loader is None:
        raise ValueError(f"Unable to load tools module: {target}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    register_fn = getattr(module, "register", None)
    if callable(register_fn):
        register_fn(registry)


class BotRuntime:
    """Runtime engine tying together manifest, knowledge RAG, tools, actions, and LLM."""

    def __init__(
        self,
        bot_dir_or_manifest: str | Path | BotManifest,
        *,
        bot_dir: str | Path | None = None,
        backend: Any = None,
        llm: Any = None,
        allow_local_actions: bool = False,
        action_transport: httpx.AsyncBaseTransport | None = None,
        action_http_client: httpx.AsyncClient | None = None,
        keys_path: str | Path | None = None,
    ) -> None:
        if isinstance(bot_dir_or_manifest, BotManifest):
            self.manifest = bot_dir_or_manifest
            self.bot_dir: Path | None = Path(bot_dir) if bot_dir is not None else None
        else:
            p = Path(bot_dir_or_manifest)
            self.bot_dir = p.parent if p.is_file() else p
            self.manifest = load_bot_manifest(p)

        self.keys_path = keys_path
        self.allow_local_actions = allow_local_actions
        self.action_transport = action_transport
        self.action_http_client = action_http_client
        self._explicit_backend = llm if llm is not None else backend
        self.created_at = int(time.time())

        # Local knowledge base
        if self.bot_dir is not None and self.manifest.knowledge.paths:
            self.knowledge = KnowledgeIndex.from_manifest(
                self.bot_dir, self.manifest.knowledge
            )
        else:
            self.knowledge = KnowledgeIndex([])

        # ToolRegistry: custom modules + OpenAPI actions + local file_search
        self.registry = ToolRegistry()
        self.registry.agent_name = self.manifest.id

        if self.bot_dir is not None:
            for mod_rel in self.manifest.tools.modules:
                _load_tools_module(self.bot_dir, mod_rel, self.registry)

            all_actions = []
            for act_cfg in self.manifest.actions:
                all_actions.extend(load_openapi_actions(act_cfg, self.bot_dir))
            if all_actions:
                self.action_executor = ActionExecutor(
                    all_actions,
                    allow_local_urls=allow_local_actions,
                    transport=action_transport,
                    http_client=action_http_client,
                )
                self.action_executor.register_into(self.registry)
            else:
                self.action_executor = None
        else:
            self.action_executor = None

        if self.manifest.tools.packs:
            try:
                from adk.tool_pack_loader import get_tool_pack_loader

                env_pack_dirs = [
                    Path(d.strip())
                    for d in os.environ.get("AITHER_TOOLPACK_DIRS", "").split(os.pathsep)
                    if d.strip()
                ]
                loader = (
                    get_tool_pack_loader(extra_dirs=env_pack_dirs)
                    if env_pack_dirs
                    else get_tool_pack_loader()
                )
                pack_manifests = loader.load_packs(list(self.manifest.tools.packs))

                class _RegistryHost:
                    def __init__(self, reg: ToolRegistry) -> None:
                        self.tool_registry = reg
                        self.tools = reg

                reg_host = _RegistryHost(self.registry)
                for pm in pack_manifests:
                    loader.register_on_adk_agent(pm, reg_host)
            except Exception as exc:
                logger.debug("bot %s: tool pack load skipped: %s", self.manifest.id, exc)

        if (
            self.knowledge.chunks
            and (self.manifest.capabilities.file_search or self.manifest.grounding.local_knowledge)
            and self.registry.get("file_search") is None
        ):
            self.registry.register(
                self.knowledge.as_file_search_tool(
                    default_top_k=self.manifest.knowledge.top_k
                ),
                name="file_search",
                description="Search the bot's local knowledge base.",
                repeat_safe=True,
            )

        # In-memory Assistants API state seeded from bot.yaml
        self.assistants: dict[str, dict[str, Any]] = {}
        self.threads: dict[str, dict[str, Any]] = {}
        self.thread_messages: dict[str, list[dict[str, Any]]] = {}
        self.thread_runs: dict[str, list[dict[str, Any]]] = {}
        self._seed_default_assistant()

    def _seed_default_assistant(self) -> None:
        asst_id = f"asst_{self.manifest.id}"
        self.assistants[asst_id] = self._assistant_payload(
            assistant_id=asst_id,
            name=self.manifest.name,
            description=self.manifest.description,
            instructions=self.manifest.instructions,
            model=self.manifest.model,
        )

    def _assistant_payload(
        self,
        *,
        assistant_id: str,
        name: str,
        description: str,
        instructions: str,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        asst_tools = list(tools) if tools is not None else []
        if tools is None:
            if self.manifest.capabilities.file_search:
                asst_tools.append({"type": "file_search"})
            if self.manifest.capabilities.code_interpreter:
                asst_tools.append({"type": "code_interpreter"})
            for tool_def in self.registry.to_openai_format():
                asst_tools.append(tool_def)
        return {
            "id": assistant_id,
            "object": "assistant",
            "created_at": self.created_at,
            "name": name,
            "description": description,
            "model": model,
            "instructions": instructions,
            "tools": asst_tools,
            "metadata": metadata or {},
            "temperature": (
                self.manifest.generation_config.temperature
                if self.manifest.generation_config.temperature is not None
                else 0.7
            ),
            "top_p": (
                self.manifest.generation_config.top_p
                if self.manifest.generation_config.top_p is not None
                else 1.0
            ),
            "tool_resources": {},
            "response_format": "auto",
        }

    def resolve_backend(self) -> LLMProvider:
        """Resolve active ``LLMProvider`` without passing ``'mock'`` to ``LLMRouter``."""
        override = self._explicit_backend
        if override is not None:
            if isinstance(override, LLMProvider) or (
                hasattr(override, "chat") and callable(override.chat)
            ):
                return override
            if callable(override) and not isinstance(override, str):
                return _CallableBackendAdapter(override, self.manifest.model)
            backend_name = str(override).strip().lower()
        else:
            backend_name = self.manifest.backend.strip().lower()

        if backend_name == "mock":
            return MockBotBackend(
                bot_name=self.manifest.name,
                default_model=self.manifest.model,
            )

        if backend_name == "gemini":
            key = resolve_provider_key("gemini", keys_path=self.keys_path)
            if not key:
                raise BackendUnavailableError(
                    "Gemini backend selected but no GEMINI_API_KEY / GOOGLE_API_KEY "
                    "is configured in environment or provider_keys.json"
                )
            from adk.llm.gemini import GeminiProvider

            return GeminiProvider(api_key=key, default_model=self.manifest.model)

        if backend_name == "openai":
            key = resolve_provider_key("openai", keys_path=self.keys_path)
            if not key:
                raise BackendUnavailableError(
                    "OpenAI backend selected but no OPENAI_API_KEY is configured "
                    "in environment or provider_keys.json"
                )
            from adk.llm.openai_compat import OpenAIProvider

            oai_base = (os.getenv("OPENAI_BASE_URL") or "").strip()
            oai_kwargs: dict[str, Any] = {"api_key": key, "default_model": self.manifest.model}
            if oai_base:
                oai_kwargs["base_url"] = oai_base
            return OpenAIProvider(**oai_kwargs)

        if backend_name in ("awzeal", "zero-libc"):
            try:
                from adk.llm.awzeal import AwzealZeroLibcProvider
            except ImportError as exc:
                raise BackendUnavailableError(
                    f"{backend_name} backend selected but adk.llm.awzeal is unavailable: {exc}"
                ) from exc
            try:
                provider = AwzealZeroLibcProvider(
                    model=self.manifest.model,
                    model_sha256=self.manifest.model_sha256,
                    shared_ro_mmap=self.manifest.shared_ro_mmap,
                    target_surface=self.manifest.target_surface,
                    allow_model_path_coercion=False,
                )
            except TypeError:
                try:
                    provider = AwzealZeroLibcProvider(model=self.manifest.model)
                except ValueError as exc:
                    raise BackendUnavailableError(
                        f"{backend_name} backend rejected invalid configuration: {exc}"
                    ) from exc
            except ValueError as exc:
                raise BackendUnavailableError(
                    f"{backend_name} backend rejected invalid configuration: {exc}"
                ) from exc
            has_attr = any(
                hasattr(provider, attr)
                for attr in ("binary", "endpoint", "base_url", "url")
            )
            if has_attr and not any(
                getattr(provider, attr, None)
                for attr in ("binary", "endpoint", "base_url", "url")
            ):
                raise BackendUnavailableError(
                    f"{backend_name} backend selected but no AWZEAL_BINARY / ASM_LLM_BINARY "
                    "or AWZEAL_URL endpoint is configured"
                )
            return provider

        if backend_name in ("llamacpp", "vllm"):
            from adk.config import Config
            from adk.llm import LLMRouter

            cfg = Config.from_env()
            return LLMRouter(
                provider=backend_name,
                model=self.manifest.model,
                base_url=(cfg.llm_base_url or "").strip() or None,
                config=cfg,
            )

        # "auto" or "aither": check configured style BYOK key if no local spine, else LLMRouter
        from adk.config import Config
        from adk.llm import LLMRouter

        if backend_name == "auto":
            style_key = resolve_provider_key(self.manifest.style, keys_path=self.keys_path)
            if style_key and self.manifest.style == "gemini":
                from adk.llm.gemini import GeminiProvider

                return GeminiProvider(api_key=style_key, default_model=self.manifest.model)
            if style_key and self.manifest.style == "openai":
                from adk.llm.openai_compat import OpenAIProvider

                return OpenAIProvider(api_key=style_key, default_model=self.manifest.model)

        return LLMRouter(
            provider=None if backend_name in ("auto", "aither") else backend_name,
            model=self.manifest.model,
            config=Config.from_env(),
        )

    def _build_turn_messages(
        self,
        history: list[Message],
        *,
        extra_system: str = "",
    ) -> tuple[list[Message], list[KnowledgeHit]]:
        last_user_text = ""
        for m in reversed(history):
            if m.role == "user":
                last_user_text = m.content if isinstance(m.content, str) else json.dumps(m.content)
                break

        hits: list[KnowledgeHit] = []
        if (
            last_user_text.strip()
            and self.knowledge.chunks
            and (self.manifest.grounding.local_knowledge or self.manifest.capabilities.file_search)
        ):
            hits = self.knowledge.search(
                last_user_text, top_k=self.manifest.knowledge.top_k
            )

        sys_parts = [self.manifest.instructions]
        if extra_system.strip():
            sys_parts.append(extra_system.strip())
        if hits:
            sys_parts.append(self.knowledge.format_context(hits))

        merged_messages: list[Message] = [
            Message(role="system", content="\n\n".join(sys_parts))
        ]
        for m in history:
            if m.role == "system":
                sys_parts.append(str(m.content))
                merged_messages[0] = Message(role="system", content="\n\n".join(sys_parts))
            else:
                merged_messages.append(m)
        return merged_messages, hits

    async def generate(
        self,
        history: list[Message],
        *,
        model: str | None = None,
        extra_system: str = "",
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        extra_tools: list[dict[str, Any]] | None = None,
        max_tool_rounds: int = 6,
        response_format: dict[str, Any] | str | None = None,
        response_mime_type: str | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> TurnResult:
        """Execute a full conversation turn with RAG grounding and bounded ReAct tools."""
        provider = self.resolve_backend()
        messages, hits = self._build_turn_messages(history, extra_system=extra_system)

        offered_tools = self.registry.to_openai_format()
        existing_names = {
            t["function"]["name"]
            for t in offered_tools
            if isinstance(t.get("function"), dict)
        }
        if extra_tools:
            for t in extra_tools:
                fname = ((t.get("function") or {}) if isinstance(t, dict) else {}).get("name")
                if fname and fname not in existing_names:
                    offered_tools.append(t)
                    existing_names.add(fname)

        chosen_model = model or self.manifest.model
        eff_temp = (
            temperature
            if temperature is not None
            else (
                self.manifest.generation_config.temperature
                if self.manifest.generation_config.temperature is not None
                else 0.7
            )
        )
        eff_top_p = (
            top_p
            if top_p is not None
            else self.manifest.generation_config.top_p
        )
        eff_max = (
            max_tokens
            if max_tokens is not None
            else self.manifest.generation_config.effective_max_tokens(2048)
        )
        extra_chat_kwargs = _filter_supported_kwargs(
            provider.chat,
            {
                "response_format": (
                    response_format
                    if response_format is not None
                    else self.manifest.generation_config.response_format
                ),
                "response_mime_type": (
                    response_mime_type
                    if response_mime_type is not None
                    else self.manifest.generation_config.response_mime_type
                ),
                "response_schema": (
                    response_schema
                    if response_schema is not None
                    else self.manifest.generation_config.response_schema
                ),
            },
        )

        executed: list[dict[str, Any]] = []
        total_pt = 0
        total_ct = 0
        last_resp: LLMResponse | None = None

        for _ in range(max_tool_rounds):
            try:
                resp = await provider.chat(
                    messages,
                    model=chosen_model,
                    temperature=eff_temp,
                    max_tokens=eff_max,
                    tools=offered_tools or None,
                    top_p=eff_top_p,
                    **extra_chat_kwargs,
                )
            except BackendUnavailableError:
                raise
            except Exception as exc:
                raise BackendUnavailableError(
                    f"LLM backend {self.manifest.backend!r} unavailable: {exc}"
                ) from exc

            last_resp = resp
            total_pt += int(resp.prompt_tokens or 0)
            total_ct += int(resp.completion_tokens or 0)

            if not resp.tool_calls:
                return TurnResult(
                    text=resp.content or "",
                    model=resp.model or chosen_model,
                    tool_calls=[],
                    executed_tool_calls=executed,
                    knowledge_hits=hits,
                    finish_reason=resp.finish_reason or "stop",
                    prompt_tokens=total_pt,
                    completion_tokens=total_ct,
                    total_tokens=total_pt + total_ct,
                )

            # Separate local registered tools vs client-supplied tools
            local_calls = [tc for tc in resp.tool_calls if self.registry.get(tc.name) is not None]
            if not local_calls:
                return TurnResult(
                    text=resp.content or "",
                    model=resp.model or chosen_model,
                    tool_calls=list(resp.tool_calls),
                    executed_tool_calls=executed,
                    knowledge_hits=hits,
                    finish_reason="tool_calls",
                    prompt_tokens=total_pt,
                    completion_tokens=total_ct,
                    total_tokens=total_pt + total_ct,
                )

            messages.append(
                Message(
                    role="assistant",
                    content=resp.content or "",
                    tool_calls=[
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in local_calls
                    ],
                )
            )
            for tc in local_calls:
                out_str = await self.registry.execute(tc.name, tc.arguments)
                executed.append(
                    {
                        "id": tc.id,
                        "name": tc.name,
                        "arguments": tc.arguments,
                        "output": out_str,
                    }
                )
                messages.append(
                    Message(
                        role="tool",
                        name=tc.name,
                        tool_call_id=tc.id,
                        content=out_str,
                    )
                )

        final_text = (last_resp.content if last_resp else "") or ""
        return TurnResult(
            text=final_text,
            model=(last_resp.model if last_resp else chosen_model) or chosen_model,
            executed_tool_calls=executed,
            knowledge_hits=hits,
            finish_reason="stop",
            prompt_tokens=total_pt,
            completion_tokens=total_ct,
            total_tokens=total_pt + total_ct,
        )

    async def stream_text(
        self,
        history: list[Message],
        *,
        model: str | None = None,
        extra_system: str = "",
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        extra_tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | str | None = None,
        response_mime_type: str | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield normalized text/finish events for SSE endpoints."""
        # If local tools exist, run turn loop first so tool calls execute cleanly before streaming.
        if self.registry.list_tools():
            turn = await self.generate(
                history,
                model=model,
                extra_system=extra_system,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                extra_tools=extra_tools,
                response_format=response_format,
                response_mime_type=response_mime_type,
                response_schema=response_schema,
            )
            if turn.tool_calls:
                yield {
                    "type": "tool_calls",
                    "tool_calls": turn.tool_calls,
                    "model": turn.model,
                    "hits": turn.knowledge_hits,
                    "usage": (turn.prompt_tokens, turn.completion_tokens, turn.total_tokens),
                }
                return
            words = turn.text.split(" ") if turn.text else [""]
            for idx, w in enumerate(words):
                piece = w if idx == 0 else f" {w}"
                if piece:
                    yield {"type": "delta", "text": piece, "model": turn.model}
            yield {
                "type": "done",
                "model": turn.model,
                "finish_reason": turn.finish_reason,
                "hits": turn.knowledge_hits,
                "usage": (turn.prompt_tokens, turn.completion_tokens, turn.total_tokens),
            }
            return

        provider = self.resolve_backend()
        messages, hits = self._build_turn_messages(history, extra_system=extra_system)
        chosen_model = model or self.manifest.model
        eff_temp = (
            temperature
            if temperature is not None
            else (
                self.manifest.generation_config.temperature
                if self.manifest.generation_config.temperature is not None
                else 0.7
            )
        )
        eff_top_p = top_p if top_p is not None else self.manifest.generation_config.top_p
        eff_max = (
            max_tokens
            if max_tokens is not None
            else self.manifest.generation_config.effective_max_tokens(2048)
        )
        extra_stream_kwargs = _filter_supported_kwargs(
            provider.chat_stream,
            {
                "response_format": (
                    response_format
                    if response_format is not None
                    else self.manifest.generation_config.response_format
                ),
                "response_mime_type": (
                    response_mime_type
                    if response_mime_type is not None
                    else self.manifest.generation_config.response_mime_type
                ),
                "response_schema": (
                    response_schema
                    if response_schema is not None
                    else self.manifest.generation_config.response_schema
                ),
            },
        )

        acc: list[str] = []
        try:
            async for chunk in provider.chat_stream(
                messages,
                model=chosen_model,
                temperature=eff_temp,
                max_tokens=eff_max,
                tools=extra_tools or None,
                top_p=eff_top_p,
                **extra_stream_kwargs,
            ):
                if chunk.content:
                    acc.append(chunk.content)
                    yield {"type": "delta", "text": chunk.content, "model": chunk.model or chosen_model}
                if chunk.tool_calls:
                    yield {
                        "type": "tool_calls",
                        "tool_calls": chunk.tool_calls,
                        "model": chunk.model or chosen_model,
                        "hits": hits,
                        "usage": (1, 1, 2),
                    }
                    return
                if chunk.done:
                    pt = max(1, sum(len(str(m.content).split()) for m in messages))
                    ct = max(1, len("".join(acc).split()))
                    yield {
                        "type": "done",
                        "model": chunk.model or chosen_model,
                        "finish_reason": chunk.finish_reason or "stop",
                        "hits": hits,
                        "usage": (pt, ct, pt + ct),
                    }
                    return
        except BackendUnavailableError:
            raise
        except Exception as exc:
            raise BackendUnavailableError(
                f"LLM backend {self.manifest.backend!r} unavailable: {exc}"
            ) from exc

        pt = max(1, sum(len(str(m.content).split()) for m in messages))
        ct = max(1, len("".join(acc).split()))
        yield {
            "type": "done",
            "model": chosen_model,
            "finish_reason": "stop",
            "hits": hits,
            "usage": (pt, ct, pt + ct),
        }

    def get_public_info(self, base_url: str = "http://127.0.0.1:8095") -> dict[str, Any]:
        """Return public bot metadata and copyable SDK snippets (ZERO secrets)."""
        clean_base = (base_url or "http://127.0.0.1:8095").rstrip("/")
        info = self.manifest.to_public_dict()
        info["registered_tools"] = [t.name for t in self.registry.list_tools()]
        info["knowledge_chunks"] = len(self.knowledge.chunks)
        info["snippets"] = {
            "gemini": (
                "from google import genai\n\n"
                f'client = genai.Client(api_key="local", http_options={{"base_url": "{clean_base}"}})\n'
                "resp = client.models.generate_content(\n"
                f'    model="{self.manifest.model}",\n'
                '    contents="Hello!",\n'
                ")\n"
                "print(resp.text)"
            ),
            "openai": (
                "from openai import OpenAI\n\n"
                f'client = OpenAI(base_url="{clean_base}/v1", api_key="local")\n'
                "resp = client.chat.completions.create(\n"
                f'    model="{self.manifest.model}",\n'
                '    messages=[{"role": "user", "content": "Hello!"}],\n'
                ")\n"
                "print(resp.choices[0].message.content)"
            ),
        }
        return info


def _gemini_contents_to_messages(body: dict[str, Any]) -> tuple[list[Message], str, list[dict[str, Any]]]:
    extra_sys = ""
    sys_inst = body.get("systemInstruction") or body.get("system_instruction")
    if isinstance(sys_inst, dict):
        parts = sys_inst.get("parts") or []
        texts = [str(p.get("text") or "") for p in parts if isinstance(p, dict) and p.get("text")]
        extra_sys = "\n".join(texts)

    messages: list[Message] = []
    pending_call_ids: dict[str, list[str]] = {}
    for item in body.get("contents") or []:
        if not isinstance(item, dict):
            continue
        role_raw = str(item.get("role") or "user").lower()
        role = "assistant" if role_raw == "model" else "user"
        parts = item.get("parts") or []
        text_chunks: list[str] = []
        tool_calls_list: list[dict[str, Any]] = []
        for p in parts:
            if not isinstance(p, dict):
                continue
            if "text" in p and p["text"] is not None:
                text_chunks.append(str(p["text"]))
            elif "functionCall" in p and isinstance(p["functionCall"], dict):
                fc = p["functionCall"]
                fc_name = str(fc.get("name") or "").strip()
                fc_args = fc.get("args") if isinstance(fc.get("args"), dict) else {}
                fc_id = str(
                    fc.get("id")
                    or f"call_{len(messages)}_{len(tool_calls_list)}_{fc_name}"
                )
                tool_calls_list.append(
                    {
                        "id": fc_id,
                        "type": "function",
                        "function": {
                            "name": fc_name,
                            "arguments": json.dumps(fc_args),
                        },
                    }
                )
                if fc_name:
                    pending_call_ids.setdefault(fc_name, []).append(fc_id)
            elif "functionResponse" in p and isinstance(p["functionResponse"], dict):
                fr = p["functionResponse"]
                fr_name = str(fr.get("name") or "tool")
                fr_id = str(fr.get("id") or "")
                if not fr_id and pending_call_ids.get(fr_name):
                    fr_id = pending_call_ids[fr_name].pop(0)
                messages.append(
                    Message(
                        role="tool",
                        name=fr_name,
                        tool_call_id=fr_id or None,
                        content=json.dumps(fr.get("response") or {}),
                    )
                )
        if text_chunks or tool_calls_list:
            messages.append(
                Message(
                    role=role,
                    content="\n".join(text_chunks),
                    tool_calls=tool_calls_list or None,
                )
            )

    extra_tools: list[dict[str, Any]] = []
    for tgroup in body.get("tools") or []:
        if not isinstance(tgroup, dict):
            continue
        for decl in tgroup.get("functionDeclarations") or []:
            if not isinstance(decl, dict) or not decl.get("name"):
                continue
            extra_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": str(decl["name"]),
                        "description": str(decl.get("description") or ""),
                        "parameters": decl.get("parameters") or {"type": "object", "properties": {}},
                    },
                }
            )
    return messages, extra_sys, extra_tools


def _openai_messages_to_internal(raw_messages: list[dict[str, Any]]) -> list[Message]:
    out: list[Message] = []
    for item in raw_messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user")
        content = item.get("content")
        if content is None:
            content = ""
        elif isinstance(content, list):
            texts = [
                str(b.get("text") or "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            content = "\n".join(texts) if texts else json.dumps(content)
        out.append(
            Message(
                role=role,
                content=str(content),
                name=item.get("name"),
                tool_call_id=item.get("tool_call_id"),
                tool_calls=item.get("tool_calls"),
            )
        )
    return out


def _build_gemini_response(
    *,
    text: str,
    model: str,
    finish_reason: str = "STOP",
    tool_calls: list[ToolCall] | None = None,
    hits: list[KnowledgeHit] | None = None,
    safety_settings: list[Any] | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> dict[str, Any]:
    if tool_calls:
        parts: list[dict[str, Any]] = [
            {"functionCall": {"name": tc.name, "args": tc.arguments}}
            for tc in tool_calls
        ]
    else:
        parts = [{"text": text}]

    candidate: dict[str, Any] = {
        "content": {"role": "model", "parts": parts},
        "finishReason": finish_reason,
        "index": 0,
    }
    if hits:
        candidate["groundingMetadata"] = {
            "groundingChunks": [
                {
                    "retrievedContext": {
                        "uri": h.source,
                        "title": h.source,
                        "text": h.text,
                    }
                }
                for h in hits
            ]
        }
    if safety_settings:
        candidate["safetyRatings"] = [
            {
                "category": s.category if hasattr(s, "category") else s["category"],
                "probability": "NEGLIGIBLE",
                "blocked": False,
            }
            for s in safety_settings
        ]
    return {
        "candidates": [candidate],
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": completion_tokens,
            "totalTokenCount": prompt_tokens + completion_tokens,
        },
        "modelVersion": model,
    }


def _model_names_for_runtime(runtime: BotRuntime) -> list[str]:
    names = [runtime.manifest.model]
    if runtime.manifest.id not in names:
        names.append(runtime.manifest.id)
    return names


async def _read_json_dict(
    request: Request, *, allow_empty: bool = False
) -> dict[str, Any]:
    raw_body = await request.body()
    if not raw_body.strip():
        if allow_empty:
            return {}
        raise HTTPException(status_code=400, detail="Request body must be JSON object")
    try:
        parsed = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid JSON request body: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="Request body must be JSON object")
    return parsed


def _parse_opt_float(val: Any, field_name: str) -> float | None:
    if val is None:
        return None
    if isinstance(val, bool):
        raise HTTPException(
            status_code=400, detail=f"Invalid numeric value for '{field_name}'"
        )
    try:
        out = float(val)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid numeric value for '{field_name}'"
        ) from exc
    if not math.isfinite(out):
        raise HTTPException(
            status_code=400, detail=f"Invalid numeric value for '{field_name}'"
        )
    return out


def _parse_opt_int(val: Any, field_name: str) -> int | None:
    if val is None:
        return None
    if isinstance(val, bool):
        raise HTTPException(
            status_code=400, detail=f"Invalid integer value for '{field_name}'"
        )
    if isinstance(val, float):
        if not math.isfinite(val) or not val.is_integer():
            raise HTTPException(
                status_code=400, detail=f"Invalid integer value for '{field_name}'"
            )
        return int(val)
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        try:
            return int(val.strip())
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid integer value for '{field_name}'"
            ) from exc
    raise HTTPException(
        status_code=400, detail=f"Invalid integer value for '{field_name}'"
    )


def mount_bot_routes(
    app: FastAPI,
    runtime: BotRuntime,
    *,
    prefix: str = "",
    include_openai_chat: bool = True,
    include_root_ui: bool = True,
) -> None:
    """Mount Gemini ``v1beta``, OpenAI ``v1``/Assistants, and Playground routes onto *app*."""
    clean_prefix = prefix.rstrip("/")
    router = APIRouter(prefix=clean_prefix)

    @router.get("/bot/info")
    async def bot_info(request: Request) -> dict[str, Any]:
        base = str(request.base_url).rstrip("/")
        return runtime.get_public_info(base_url=base)

    @router.get("/bot/ui", response_class=HTMLResponse)
    async def bot_ui(request: Request) -> HTMLResponse:
        base = str(request.base_url).rstrip("/")
        return HTMLResponse(render_playground_html(runtime.get_public_info(base_url=base)))

    if include_root_ui:
        @router.get("/", response_class=HTMLResponse)
        async def bot_root_ui(request: Request) -> HTMLResponse:
            base = str(request.base_url).rstrip("/")
            return HTMLResponse(render_playground_html(runtime.get_public_info(base_url=base)))

    # ── Gemini v1beta endpoints ──────────────────────────────────────────────
    @router.get("/v1beta/models")
    async def gemini_list_models() -> dict[str, Any]:
        models = []
        for m in _model_names_for_runtime(runtime):
            models.append(
                {
                    "name": f"models/{m}",
                    "displayName": runtime.manifest.name,
                    "version": runtime.manifest.version,
                    "description": runtime.manifest.description,
                    "inputTokenLimit": 1048576,
                    "outputTokenLimit": runtime.manifest.generation_config.effective_max_tokens(8192),
                    "supportedGenerationMethods": [
                        "generateContent",
                        "streamGenerateContent",
                    ],
                }
            )
        return {"models": models, "nextPageToken": None}

    @router.get("/v1beta/models/{model_id:path}")
    async def gemini_get_model(model_id: str) -> dict[str, Any]:
        clean_id = model_id.removeprefix("models/")
        return {
            "name": f"models/{clean_id}",
            "displayName": runtime.manifest.name,
            "version": runtime.manifest.version,
            "description": runtime.manifest.description,
            "inputTokenLimit": 1048576,
            "outputTokenLimit": runtime.manifest.generation_config.effective_max_tokens(8192),
            "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
        }

    @router.post("/v1beta/models/{model_action:path}")
    async def gemini_model_action(model_action: str, request: Request):
        if model_action.endswith(":streamGenerateContent"):
            raw_model = model_action[: -len(":streamGenerateContent")].removeprefix("models/")
            is_stream = True
        elif model_action.endswith(":generateContent"):
            raw_model = model_action[: -len(":generateContent")].removeprefix("models/")
            is_stream = False
        else:
            raise HTTPException(status_code=404, detail=f"Unsupported Gemini action: {model_action}")

        body = await _read_json_dict(request, allow_empty=False)

        messages, extra_sys, extra_tools = _gemini_contents_to_messages(body)
        gen_cfg = body.get("generationConfig") or body.get("generation_config") or {}
        if not isinstance(gen_cfg, dict):
            gen_cfg = {}
        temp = _parse_opt_float(gen_cfg.get("temperature"), "temperature")
        raw_top_p = gen_cfg["topP"] if "topP" in gen_cfg else gen_cfg.get("top_p")
        top_p = _parse_opt_float(raw_top_p, "topP")
        raw_max_out = (
            gen_cfg["maxOutputTokens"]
            if "maxOutputTokens" in gen_cfg
            else gen_cfg.get("max_output_tokens")
        )
        max_out = _parse_opt_int(raw_max_out, "maxOutputTokens")
        resp_mime = gen_cfg.get("responseMimeType") or gen_cfg.get("response_mime_type")
        resp_schema = gen_cfg.get("responseSchema") or gen_cfg.get("response_schema")
        chosen_model = raw_model or runtime.manifest.model

        if not is_stream:
            try:
                turn = await runtime.generate(
                    messages,
                    model=chosen_model,
                    extra_system=extra_sys,
                    temperature=temp,
                    top_p=top_p,
                    max_tokens=max_out,
                    extra_tools=extra_tools or None,
                    response_mime_type=str(resp_mime) if resp_mime is not None else None,
                    response_schema=resp_schema if isinstance(resp_schema, dict) else None,
                )
            except BackendUnavailableError as exc:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "code": 503,
                            "message": str(exc),
                            "status": "UNAVAILABLE",
                        }
                    },
                )
            return _build_gemini_response(
                text=turn.text,
                model=turn.model,
                finish_reason="STOP",
                tool_calls=turn.tool_calls or None,
                hits=turn.knowledge_hits,
                safety_settings=runtime.manifest.safety_settings,
                prompt_tokens=turn.prompt_tokens,
                completion_tokens=turn.completion_tokens,
            )

        def _event_to_gemini_payload(ev: dict[str, Any]) -> dict[str, Any] | None:
            if ev["type"] == "delta":
                return _build_gemini_response(
                    text=ev["text"],
                    model=ev["model"],
                    finish_reason="FINISH_REASON_UNSPECIFIED",
                )
            if ev["type"] == "tool_calls":
                pt, ct, _ = ev["usage"]
                return _build_gemini_response(
                    text="",
                    model=ev["model"],
                    finish_reason="STOP",
                    tool_calls=ev["tool_calls"],
                    hits=ev.get("hits"),
                    safety_settings=runtime.manifest.safety_settings,
                    prompt_tokens=pt,
                    completion_tokens=ct,
                )
            if ev["type"] == "done":
                pt, ct, _ = ev["usage"]
                return _build_gemini_response(
                    text="",
                    model=ev["model"],
                    finish_reason="STOP",
                    hits=ev.get("hits"),
                    safety_settings=runtime.manifest.safety_settings,
                    prompt_tokens=pt,
                    completion_tokens=ct,
                )
            return None

        alt = (request.query_params.get("alt") or "").strip().lower()
        if alt != "sse":
            json_chunks: list[dict[str, Any]] = []
            try:
                async for ev in runtime.stream_text(
                    messages,
                    model=chosen_model,
                    extra_system=extra_sys,
                    temperature=temp,
                    top_p=top_p,
                    max_tokens=max_out,
                    extra_tools=extra_tools or None,
                    response_mime_type=str(resp_mime) if resp_mime is not None else None,
                    response_schema=resp_schema if isinstance(resp_schema, dict) else None,
                ):
                    payload = _event_to_gemini_payload(ev)
                    if payload is not None:
                        json_chunks.append(payload)
            except BackendUnavailableError as exc:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "code": 503,
                            "message": str(exc),
                            "status": "UNAVAILABLE",
                        }
                    },
                )
            return JSONResponse(content=json_chunks, media_type="application/json")

        async def _sse_gemini() -> AsyncIterator[str]:
            try:
                async for ev in runtime.stream_text(
                    messages,
                    model=chosen_model,
                    extra_system=extra_sys,
                    temperature=temp,
                    top_p=top_p,
                    max_tokens=max_out,
                    extra_tools=extra_tools or None,
                    response_mime_type=str(resp_mime) if resp_mime is not None else None,
                    response_schema=resp_schema if isinstance(resp_schema, dict) else None,
                ):
                    payload = _event_to_gemini_payload(ev)
                    if payload is not None:
                        yield f"data: {json.dumps(payload)}\n\n"
            except BackendUnavailableError as exc:
                err_payload = {
                    "error": {"code": 503, "message": str(exc), "status": "UNAVAILABLE"}
                }
                yield f"data: {json.dumps(err_payload)}\n\n"

        return StreamingResponse(
            _sse_gemini(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── OpenAI v1 Chat + Models ──────────────────────────────────────────────
    if include_openai_chat:
        @router.get("/v1/models")
        async def openai_list_models() -> dict[str, Any]:
            return {
                "object": "list",
                "data": [
                    {
                        "id": m,
                        "object": "model",
                        "created": runtime.created_at,
                        "owned_by": "aither-bot",
                    }
                    for m in _model_names_for_runtime(runtime)
                ],
            }

        @router.post("/v1/chat/completions")
        async def openai_chat_completions(request: Request):
            body = await _read_json_dict(request, allow_empty=False)

            raw_msgs = body.get("messages") or []
            if not isinstance(raw_msgs, list):
                raise HTTPException(status_code=400, detail="'messages' must be a list")

            messages = _openai_messages_to_internal(raw_msgs)
            req_model = str(body.get("model") or runtime.manifest.model)
            temp = _parse_opt_float(body.get("temperature"), "temperature")
            top_p = _parse_opt_float(body.get("top_p"), "top_p")
            raw_max_tok = (
                body["max_tokens"]
                if "max_tokens" in body
                else body.get("max_completion_tokens")
            )
            max_tok = _parse_opt_int(raw_max_tok, "max_tokens")
            extra_tools = body.get("tools") if isinstance(body.get("tools"), list) else None
            resp_format = body.get("response_format")
            stream = bool(body.get("stream", False))
            cmpl_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"
            created = int(time.time())

            if not stream:
                try:
                    turn = await runtime.generate(
                        messages,
                        model=req_model,
                        temperature=temp,
                        top_p=top_p,
                        max_tokens=max_tok,
                        extra_tools=extra_tools,
                        response_format=resp_format,
                    )
                except BackendUnavailableError as exc:
                    return JSONResponse(
                        status_code=503,
                        content={
                            "error": {
                                "message": str(exc),
                                "type": "backend_unavailable",
                                "code": 503,
                            }
                        },
                    )
                msg_payload: dict[str, Any] = {
                    "role": "assistant",
                    "content": None if turn.tool_calls else turn.text,
                }
                if turn.tool_calls:
                    msg_payload["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in turn.tool_calls
                    ]
                return {
                    "id": cmpl_id,
                    "object": "chat.completion",
                    "created": created,
                    "model": turn.model,
                    "choices": [
                        {
                            "index": 0,
                            "message": msg_payload,
                            "finish_reason": "tool_calls" if turn.tool_calls else "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": turn.prompt_tokens,
                        "completion_tokens": turn.completion_tokens,
                        "total_tokens": turn.total_tokens,
                    },
                }

            async def _sse_openai() -> AsyncIterator[str]:
                first = True
                try:
                    async for ev in runtime.stream_text(
                        messages,
                        model=req_model,
                        temperature=temp,
                        top_p=top_p,
                        max_tokens=max_tok,
                        extra_tools=extra_tools,
                        response_format=resp_format,
                    ):
                        if ev["type"] == "delta":
                            delta: dict[str, Any] = {"content": ev["text"]}
                            if first:
                                delta["role"] = "assistant"
                                first = False
                            chunk = {
                                "id": cmpl_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": ev["model"],
                                "choices": [
                                    {"index": 0, "delta": delta, "finish_reason": None}
                                ],
                            }
                            yield f"data: {json.dumps(chunk)}\n\n"
                        elif ev["type"] == "tool_calls":
                            tc_list = [
                                {
                                    "index": i,
                                    "id": tc.id,
                                    "type": "function",
                                    "function": {
                                        "name": tc.name,
                                        "arguments": json.dumps(tc.arguments),
                                    },
                                }
                                for i, tc in enumerate(ev["tool_calls"])
                            ]
                            chunk = {
                                "id": cmpl_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": ev["model"],
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"role": "assistant", "tool_calls": tc_list},
                                        "finish_reason": "tool_calls",
                                    }
                                ],
                            }
                            yield f"data: {json.dumps(chunk)}\n\n"
                        elif ev["type"] == "done":
                            chunk = {
                                "id": cmpl_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": ev["model"],
                                "choices": [
                                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                                ],
                            }
                            yield f"data: {json.dumps(chunk)}\n\n"
                except BackendUnavailableError as exc:
                    err_obj = {
                        "error": {
                            "message": str(exc),
                            "type": "backend_unavailable",
                            "code": 503,
                        }
                    }
                    yield f"data: {json.dumps(err_obj)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                _sse_openai(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

    # ── OpenAI Assistants API (/v1/assistants, /v1/threads, /v1/runs) ────────
    @router.post("/v1/assistants")
    async def create_assistant(request: Request) -> dict[str, Any]:
        body = await _read_json_dict(request, allow_empty=False)
        asst_id = f"asst_{uuid.uuid4().hex[:16]}"
        asst = runtime._assistant_payload(
            assistant_id=asst_id,
            name=str(body.get("name") or runtime.manifest.name),
            description=str(body.get("description") or runtime.manifest.description),
            instructions=str(body.get("instructions") or runtime.manifest.instructions),
            model=str(body.get("model") or runtime.manifest.model),
            tools=body.get("tools") if isinstance(body.get("tools"), list) else None,
            metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
        )
        runtime.assistants[asst_id] = asst
        return asst

    @router.get("/v1/assistants")
    async def list_assistants() -> dict[str, Any]:
        items = list(runtime.assistants.values())
        return {
            "object": "list",
            "data": items,
            "first_id": items[0]["id"] if items else None,
            "last_id": items[-1]["id"] if items else None,
            "has_more": False,
        }

    @router.get("/v1/assistants/{assistant_id}")
    async def get_assistant(assistant_id: str) -> dict[str, Any]:
        asst = runtime.assistants.get(assistant_id)
        if not asst:
            raise HTTPException(status_code=404, detail=f"Assistant {assistant_id} not found")
        return asst

    @router.post("/v1/threads")
    async def create_thread(request: Request) -> dict[str, Any]:
        body = await _read_json_dict(request, allow_empty=True)
        thread_id = f"thread_{uuid.uuid4().hex[:16]}"
        now = int(time.time())
        thread_obj = {
            "id": thread_id,
            "object": "thread",
            "created_at": now,
            "metadata": body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
            "tool_resources": body.get("tool_resources") or {},
        }
        runtime.threads[thread_id] = thread_obj
        runtime.thread_messages[thread_id] = []
        runtime.thread_runs[thread_id] = []

        for init_msg in body.get("messages") or []:
            if isinstance(init_msg, dict):
                _append_thread_message(
                    runtime,
                    thread_id=thread_id,
                    role=str(init_msg.get("role") or "user"),
                    content_text=str(init_msg.get("content") or ""),
                )
        return thread_obj

    @router.get("/v1/threads/{thread_id}")
    async def get_thread(thread_id: str) -> dict[str, Any]:
        thread = runtime.threads.get(thread_id)
        if not thread:
            raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
        return thread

    @router.post("/v1/threads/{thread_id}/messages")
    async def create_thread_message(thread_id: str, request: Request) -> dict[str, Any]:
        if thread_id not in runtime.threads:
            raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
        body = await _read_json_dict(request, allow_empty=False)
        raw_content = body.get("content") or ""
        if isinstance(raw_content, list):
            content_text = "\n".join(
                str(p.get("text") or "")
                for p in raw_content
                if isinstance(p, dict)
            )
        else:
            content_text = str(raw_content)
        return _append_thread_message(
            runtime,
            thread_id=thread_id,
            role=str(body.get("role") or "user"),
            content_text=content_text,
            metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
        )

    @router.get("/v1/threads/{thread_id}/messages")
    async def list_thread_messages(thread_id: str) -> dict[str, Any]:
        if thread_id not in runtime.threads:
            raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
        msgs = list(reversed(runtime.thread_messages.get(thread_id, [])))
        return {
            "object": "list",
            "data": msgs,
            "first_id": msgs[0]["id"] if msgs else None,
            "last_id": msgs[-1]["id"] if msgs else None,
            "has_more": False,
        }

    @router.post("/v1/threads/{thread_id}/runs")
    async def create_thread_run(thread_id: str, request: Request):
        if thread_id not in runtime.threads:
            raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
        body = await _read_json_dict(request, allow_empty=False)

        asst_id = str(body.get("assistant_id") or f"asst_{runtime.manifest.id}")
        asst = runtime.assistants.get(asst_id)
        if not asst:
            raise HTTPException(status_code=404, detail=f"Assistant {asst_id} not found")

        run_id = f"run_{uuid.uuid4().hex[:16]}"
        now = int(time.time())
        instructions = str(body.get("instructions") or asst.get("instructions") or runtime.manifest.instructions)
        model = str(body.get("model") or asst.get("model") or runtime.manifest.model)

        history = [
            Message(
                role="assistant" if m["role"] == "assistant" else "user",
                content=m["content"][0]["text"]["value"],
            )
            for m in runtime.thread_messages.get(thread_id, [])
        ]

        try:
            turn = await runtime.generate(
                history,
                model=model,
                extra_system=instructions if instructions != runtime.manifest.instructions else "",
            )
        except BackendUnavailableError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": str(exc),
                        "type": "backend_unavailable",
                        "code": 503,
                    }
                },
            )

        _append_thread_message(
            runtime,
            thread_id=thread_id,
            role="assistant",
            content_text=turn.text,
            assistant_id=asst_id,
            run_id=run_id,
        )

        run_obj = {
            "id": run_id,
            "object": "thread.run",
            "created_at": now,
            "thread_id": thread_id,
            "assistant_id": asst_id,
            "status": "completed",
            "model": turn.model,
            "instructions": instructions,
            "tools": asst.get("tools", []),
            "parallel_tool_calls": True,
            "started_at": now,
            "completed_at": int(time.time()),
            "usage": {
                "prompt_tokens": turn.prompt_tokens,
                "completion_tokens": turn.completion_tokens,
                "total_tokens": turn.total_tokens,
            },
            "metadata": body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
        }
        runtime.thread_runs[thread_id].append(run_obj)
        return run_obj

    @router.get("/v1/threads/{thread_id}/runs")
    async def list_thread_runs(thread_id: str) -> dict[str, Any]:
        if thread_id not in runtime.threads:
            raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
        runs = list(reversed(runtime.thread_runs.get(thread_id, [])))
        return {
            "object": "list",
            "data": runs,
            "first_id": runs[0]["id"] if runs else None,
            "last_id": runs[-1]["id"] if runs else None,
            "has_more": False,
        }

    @router.get("/v1/threads/{thread_id}/runs/{run_id}")
    async def get_thread_run(thread_id: str, run_id: str) -> dict[str, Any]:
        if thread_id not in runtime.threads:
            raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
        for r in runtime.thread_runs.get(thread_id, []):
            if r["id"] == run_id:
                return r
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    app.include_router(router)


def _append_thread_message(
    runtime: BotRuntime,
    *,
    thread_id: str,
    role: str,
    content_text: str,
    assistant_id: str | None = None,
    run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    msg = {
        "id": f"msg_{uuid.uuid4().hex[:16]}",
        "object": "thread.message",
        "created_at": int(time.time()),
        "thread_id": thread_id,
        "role": role,
        "status": "completed",
        "content": [{"type": "text", "text": {"value": content_text, "annotations": []}}],
        "assistant_id": assistant_id,
        "run_id": run_id,
        "attachments": [],
        "metadata": metadata or {},
    }
    runtime.thread_messages.setdefault(thread_id, []).append(msg)
    return msg


def create_bot_app(
    bot_dir_or_manifest: str | Path | BotManifest,
    *,
    bot_dir: str | Path | None = None,
    backend: Any = None,
    llm: Any = None,
    allow_local_actions: bool = False,
    action_transport: httpx.AsyncBaseTransport | None = None,
    action_http_client: httpx.AsyncClient | None = None,
    keys_path: str | Path | None = None,
) -> FastAPI:
    """Create a standalone FastAPI application serving a declarative bot."""
    runtime = BotRuntime(
        bot_dir_or_manifest,
        bot_dir=bot_dir,
        backend=backend,
        llm=llm,
        allow_local_actions=allow_local_actions,
        action_transport=action_transport,
        action_http_client=action_http_client,
        keys_path=keys_path,
    )
    app = FastAPI(
        title=runtime.manifest.name,
        version=runtime.manifest.version,
        description=runtime.manifest.description,
    )
    app.state.bot_runtime = runtime
    mount_bot_routes(app, runtime, prefix="", include_openai_chat=True, include_root_ui=True)
    return app
