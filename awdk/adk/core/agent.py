"""Agent + reasoning loop.

The :class:`Agent` is intentionally tiny. The reasoning loop is pluggable
via the :class:`AgentLoop` protocol; the default is :class:`ReActLoop` which
implements the standard ReAct pattern (Thought -> Action -> Observation).

Full tool-call orchestration with model-native function calling lands in
slice B once we have a real backend. The slice-A default loop is text-based
and deliberately simple, so any backend works.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from adk.core.capability import (
    Capability,
    CapabilityContext,
    use_context,
)
from adk.core.logging import get_logger
from adk.core.memory import InMemoryStore, Memory
from adk.core.model import Message, ModelBackend
from adk.core.tool import Tool, ToolResult
from adk.core.trace import Tracer, get_tracer

_log = get_logger("agent")

# Optional selfgraph integration (no-op if unavailable or disabled)
try:
    from adk.selfgraph import record_run, current_run, record_tool_call
    _SELFGRAPH_AVAILABLE = True
except ImportError:
    _SELFGRAPH_AVAILABLE = False


@dataclass(slots=True)
class AgentResult:
    """One agent run."""

    output: str
    messages: list[Message] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    finish_reason: str = "stop"

    def __post_init__(self) -> None:
        # Runtime-validate the typed contract at the model-facing loop's return
        # boundary (NOOA typed-I/O principle). Fail closed on type mismatch so a
        # loop that returns a malformed result is caught here, not downstream.
        if not isinstance(self.output, str):
            raise TypeError(
                f"AgentResult.output must be str, got {type(self.output).__name__}"
            )
        if not isinstance(self.messages, list):
            raise TypeError(
                f"AgentResult.messages must be list, got {type(self.messages).__name__}"
            )
        if not isinstance(self.tool_calls, list):
            raise TypeError(
                f"AgentResult.tool_calls must be list, got {type(self.tool_calls).__name__}"
            )
        if isinstance(self.steps, bool) or not isinstance(self.steps, int):
            raise TypeError(
                f"AgentResult.steps must be int, got {type(self.steps).__name__}"
            )
        if not isinstance(self.finish_reason, str):
            raise TypeError(
                f"AgentResult.finish_reason must be str, "
                f"got {type(self.finish_reason).__name__}"
            )


class AgentLoop(Protocol):
    """A pluggable reasoning loop."""

    async def run(self, agent: Agent, prompt: str) -> AgentResult: ...


# NOOA vocabulary: a "Strategy" is exactly an AgentLoop — the pluggable execution
# mode for an agentic method (Predict = single-shot typed call; ReAct = iterative
# CodeAct). Alias only; the contract is identical, so every loop is a strategy.
Strategy = AgentLoop


# ---------------------------------------------------------------------------
# Default ReAct loop
# ---------------------------------------------------------------------------

_REACT_SYS_TEMPLATE = """\
You are {name}.
{instructions}

You have access to these tools:
{tool_list}

Use the ReAct pattern. Each turn, emit EXACTLY ONE of:

  Thought: <your reasoning>
  Action: <tool_name>
  Action Input: <json arguments>

...then wait for the Observation. When you have the final answer, emit:

  Final Answer: <your answer>
"""

_ACTION_RE = re.compile(r"Action:\s*([A-Za-z0-9_\-]+)\s*\n\s*Action Input:\s*(.+)")
_FINAL_RE = re.compile(r"Final Answer:\s*(.*)", re.S)


class ReActLoop:
    """Textual ReAct loop. Works with any model backend.

    Args:
        max_steps: Maximum reasoning steps before terminating.
        model: Optional per-loop model override (None → use agent.model).
        max_preview_chars: If set, bound tool result observations to this many chars
            using pformat(). None (default) = unbounded str() behavior (backward compat).
    """

    def __init__(
        self,
        *,
        max_steps: int = 8,
        model: ModelBackend | None = None,
        max_preview_chars: int | None = None,
    ) -> None:
        self.max_steps = max_steps
        # Per-method model override (NOOA): None → use agent.model at run time.
        self.model = model
        self.max_preview_chars = max_preview_chars

    async def run(self, agent: Agent, prompt: str) -> AgentResult:
        tracer = agent.tracer
        model = self.model or agent.model
        tools_by_name = {t.name: t for t in agent.tools}
        tool_list = (
            "\n".join(f"- {t.name}: {t.description}" for t in agent.tools)
            or "  (no tools)"
        )
        system = _REACT_SYS_TEMPLATE.format(
            name=agent.name,
            instructions=agent.instructions or "",
            tool_list=tool_list,
        )
        # Inject authority-ranked memory + active decisions/corrections so the
        # model SEES authority. No-op for plain stores (which lack these hooks)
        # and never fatal — memory must not break the reasoning loop.
        if getattr(agent, "recall_memory", False):
            mem = agent.memory
            if hasattr(mem, "context_block") and hasattr(mem, "constraints_block"):
                try:
                    constr = await mem.constraints_block()
                    recalled = await mem.context_block(prompt)
                    extra = "\n\n".join(b for b in (constr, recalled) if b)
                    if extra:
                        system = f"{system}\n\n{extra}"
                except Exception as e:  # noqa: BLE001 — memory is best-effort
                    _log.warning("agent.memory.recall_failed", extra={"err": str(e)})
        messages: list[Message] = [
            Message(role="system", content=system),
            Message(role="user", content=prompt),
        ]
        tool_calls: list[dict[str, Any]] = []
        finish = "max_steps"
        output = ""

        # Open selfgraph recorder alongside tracer (no-op if unavailable or disabled)
        recorder = None
        if _SELFGRAPH_AVAILABLE:
            try:
                from adk.selfgraph import RunRecorder
                # Objective is the prompt; run will auto-generate a run_id
                recorder = RunRecorder(agent_id=agent.name, objective=prompt[:100])
                # Set as active for library code (best-effort)
                from adk.selfgraph import _current_run
                _current_run.set(recorder)
            except Exception:  # noqa: BLE001 — selfgraph failure is non-fatal
                recorder = None

        try:
            with tracer.span("agent.run", agent=agent.name, prompt_len=len(prompt)) as run_span:
                for step in range(1, self.max_steps + 1):
                    with tracer.span("agent.llm", step=step) as llm_span:
                        resp = await model.generate(messages)
                        llm_span.set_attr("model", resp.model)
                        llm_span.set_attr("finish_reason", resp.finish_reason or "")
                    messages.append(Message(role="assistant", content=resp.text))

                    final = _FINAL_RE.search(resp.text)
                    if final:
                        output = final.group(1).strip()
                        finish = "stop"
                        break

                    m = _ACTION_RE.search(resp.text)
                    if not m:
                        # Model didn't follow the protocol; treat the raw text as the answer.
                        output = resp.text.strip()
                        finish = "stop_no_protocol"
                        break

                    tool_name = m.group(1).strip()
                    raw_args = m.group(2).strip()
                    try:
                        args = json.loads(raw_args)
                        if not isinstance(args, dict):
                            raise ValueError("args must be a JSON object")
                    except (ValueError, json.JSONDecodeError) as e:
                        observation = f"ERROR: invalid Action Input JSON: {e}"
                        messages.append(Message(role="user", content=observation))
                        continue

                    tool = tools_by_name.get(tool_name)
                    if tool is None:
                        observation = f"ERROR: unknown tool {tool_name!r}"
                        messages.append(Message(role="user", content=observation))
                        continue

                    with tracer.span("agent.tool", tool=tool_name, step=step) as tool_span:
                        try:
                            result: ToolResult = await tool(**args)
                        except Exception as e:  # noqa: BLE001 — surface to model
                            result = ToolResult.failure(f"{type(e).__name__}: {e}")
                        tool_span.set_attr("ok", result.ok)

                    # Record tool call in selfgraph (best-effort, non-fatal)
                    if _SELFGRAPH_AVAILABLE:
                        try:
                            record_tool_call(tool_name, ok=result.ok)
                        except Exception:  # noqa: BLE001 — selfgraph is non-fatal
                            pass

                    tool_calls.append(
                        {"step": step, "name": tool_name, "args": args, "result": result}
                    )
                    # Render observation: use bounded pformat if max_preview_chars is set.
                    # truncating_pformat applies a hard cap via max_chars, producing
                    # compact representations (e.g., "list(len=100, ...)") that keep
                    # large results readable in the message history without token bloat.
                    if result.ok:
                        if self.max_preview_chars is not None:
                            from adk.agentdoc import truncating_pformat
                            observation = truncating_pformat(
                                result.value,
                                max_chars=self.max_preview_chars,
                            )
                        else:
                            observation = str(result.value)
                    else:
                        observation = f"ERROR: {result.error}"
                    messages.append(Message(role="user", content=observation))
                run_span.set_attr("steps", step)
                run_span.set_attr("finish_reason", finish)

            # Optionally persist the turn as a typed interaction memory so future
            # recalls can surface it. Off by default to avoid noise.
            if getattr(agent, "remember_interactions", False) and output:
                mem = agent.memory
                if hasattr(mem, "remember"):
                    try:
                        await mem.remember(
                            f"Q: {prompt}\nA: {output}", role="interaction",
                        )
                    except Exception as e:  # noqa: BLE001 — best-effort
                        _log.warning("agent.memory.remember_failed", extra={"err": str(e)})

            # Record run result as artifact and close recorder (best-effort)
            if recorder:
                try:
                    import hashlib
                    content_hash = hashlib.sha256(output.encode()).hexdigest()
                    recorder.artifact(
                        f"output-{finish}",
                        version=1,
                        content_hash=content_hash,
                        properties={"finish_reason": finish, "steps": step},
                    )
                    recorder.close(status="complete")
                except Exception:  # noqa: BLE001 — selfgraph is non-fatal
                    pass

            return AgentResult(
                output=output,
                messages=messages,
                tool_calls=tool_calls,
                steps=step,
                finish_reason=finish,
            )
        except Exception:
            # Close recorder on exception with status='failed' and re-raise
            if recorder:
                try:
                    recorder.close(status="failed")
                except Exception:  # noqa: BLE001 — selfgraph is non-fatal
                    pass
            raise


_PREDICT_SYS_TEMPLATE = """\
You are {name}.
{instructions}

Answer directly and concisely in a single response. Do not use tools."""


class PredictLoop:
    """Single-shot strategy (NOOA Predict): exactly one model call, no tool loop.

    Use for classification / extraction / transformation where iteration is
    unnecessary — cheaper and lower-latency than :class:`ReActLoop`. Honors the
    same capability context, tracing, and optional memory recall/remember hooks as
    ReActLoop, but never selects or executes tools. Pair with a ``model`` override
    to serve a fast model for such single-shot methods while the agent's default
    model serves open-ended work.

    Args:
        model: Optional per-loop model override (None → use agent.model).
        output_model: Optional pydantic BaseModel subclass for structured output
            validation. When set, the response is parsed as JSON and validated
            against this schema. Invalid responses are retried up to max_retries
            times. When None (default), behavior is unchanged (single call).
        max_retries: Maximum number of retries on validation failure (default 2).
            Total calls made = 1 + min(max_retries, actual failures).
    """

    def __init__(
        self,
        *,
        model: ModelBackend | None = None,
        output_model: type[BaseModel] | None = None,
        max_retries: int = 2,
    ) -> None:
        # Per-method model override (NOOA): None → use agent.model at run time.
        self.model = model
        self.output_model = output_model
        self.max_retries = max_retries

    async def run(self, agent: Agent, prompt: str) -> AgentResult:
        tracer = agent.tracer
        model = self.model or agent.model
        system = _PREDICT_SYS_TEMPLATE.format(
            name=agent.name,
            instructions=agent.instructions or "",
        )
        # Optional memory recall — mirror ReActLoop; best-effort, never fatal.
        if getattr(agent, "recall_memory", False):
            mem = agent.memory
            if hasattr(mem, "context_block") and hasattr(mem, "constraints_block"):
                try:
                    constr = await mem.constraints_block()
                    recalled = await mem.context_block(prompt)
                    extra = "\n\n".join(b for b in (constr, recalled) if b)
                    if extra:
                        system = f"{system}\n\n{extra}"
                except Exception as e:  # noqa: BLE001 — memory is best-effort
                    _log.warning("agent.memory.recall_failed", extra={"err": str(e)})

        messages: list[Message] = [
            Message(role="system", content=system),
            Message(role="user", content=prompt),
        ]

        # Open selfgraph recorder (no-op if unavailable or disabled)
        recorder = None
        if _SELFGRAPH_AVAILABLE:
            try:
                from adk.selfgraph import RunRecorder
                recorder = RunRecorder(agent_id=agent.name, objective=prompt[:100])
                from adk.selfgraph import _current_run
                _current_run.set(recorder)
            except Exception:  # noqa: BLE001 — selfgraph failure is non-fatal
                recorder = None

        try:
            # When no output_model is set, use the original single-call behavior.
            if self.output_model is None:
                with tracer.span(
                    "agent.run", agent=agent.name, prompt_len=len(prompt), strategy="predict"
                ) as run_span:
                    with tracer.span("agent.llm", step=1) as llm_span:
                        resp = await model.generate(messages)
                        llm_span.set_attr("model", resp.model)
                        llm_span.set_attr("finish_reason", resp.finish_reason or "")
                    output = resp.text.strip()
                    messages.append(Message(role="assistant", content=resp.text))
                    run_span.set_attr("steps", 1)
                    run_span.set_attr("finish_reason", resp.finish_reason or "stop")

                if getattr(agent, "remember_interactions", False) and output:
                    mem = agent.memory
                    if hasattr(mem, "remember"):
                        try:
                            await mem.remember(f"Q: {prompt}\nA: {output}", role="interaction")
                        except Exception as e:  # noqa: BLE001 — best-effort
                            _log.warning("agent.memory.remember_failed", extra={"err": str(e)})

                # Record run result as artifact (best-effort)
                if recorder:
                    try:
                        import hashlib
                        content_hash = hashlib.sha256(output.encode()).hexdigest()
                        recorder.artifact(
                            "output-predict",
                            version=1,
                            content_hash=content_hash,
                            properties={"strategy": "predict"},
                        )
                        recorder.close(status="complete")
                    except Exception:  # noqa: BLE001 — selfgraph is non-fatal
                        pass

                return AgentResult(
                    output=output,
                    messages=messages,
                    tool_calls=[],
                    steps=1,
                    finish_reason=resp.finish_reason or "stop",
                )

            # Structured output validation path: retry on validation failure.
            last_raw_text = ""
            finish_reason = "stop"
            output = ""

            with tracer.span(
                "agent.run",
                agent=agent.name,
                prompt_len=len(prompt),
                strategy="predict",
                output_model=self.output_model.__name__,
            ) as run_span:
                for attempt in range(1, self.max_retries + 2):  # +2: 1 initial + max_retries
                    with tracer.span("agent.llm", step=attempt) as llm_span:
                        resp = await model.generate(messages)
                        llm_span.set_attr("model", resp.model)
                        llm_span.set_attr("finish_reason", resp.finish_reason or "")
                        llm_span.set_attr("attempt", attempt)

                    last_raw_text = resp.text.strip()
                    messages.append(Message(role="assistant", content=resp.text))

                    # Try to parse and validate JSON
                    try:
                        parsed = json.loads(last_raw_text)
                        validated = self.output_model.model_validate(parsed)
                        output = validated.model_dump_json()
                        finish_reason = "stop"
                        run_span.set_attr("validation_success", True)
                        run_span.set_attr("attempts", attempt)
                        break
                    except (json.JSONDecodeError, ValidationError) as e:
                        error_msg = (
                            f"Validation error (attempt {attempt}/{self.max_retries + 1}): "
                            f"{type(e).__name__}: {e}"
                        )
                        # Only retry if we haven't exhausted retries
                        if attempt <= self.max_retries:
                            messages.append(Message(role="user", content=error_msg))
                            run_span.set_attr(f"validation_error_{attempt}", str(e))
                        else:
                            # All retries exhausted
                            output = last_raw_text
                            finish_reason = "validation_failed"
                            run_span.set_attr("validation_success", False)
                            run_span.set_attr("attempts", attempt)

                run_span.set_attr("steps", attempt)
                run_span.set_attr("finish_reason", finish_reason)

            if getattr(agent, "remember_interactions", False) and output:
                mem = agent.memory
                if hasattr(mem, "remember"):
                    try:
                        await mem.remember(f"Q: {prompt}\nA: {output}", role="interaction")
                    except Exception as e:  # noqa: BLE001 — best-effort
                        _log.warning("agent.memory.remember_failed", extra={"err": str(e)})

            # Record run result as artifact (best-effort)
            if recorder:
                try:
                    import hashlib
                    content_hash = hashlib.sha256(output.encode()).hexdigest()
                    recorder.artifact(
                        "output-predict",
                        version=1,
                        content_hash=content_hash,
                        properties={"strategy": "predict", "finish_reason": finish_reason},
                    )
                    recorder.close(status="complete")
                except Exception:  # noqa: BLE001 — selfgraph is non-fatal
                    pass

            return AgentResult(
                output=output,
                messages=messages,
                tool_calls=[],
                steps=attempt,
                finish_reason=finish_reason,
            )
        except Exception:
            # Close recorder on exception with status='failed' and re-raise
            if recorder:
                try:
                    recorder.close(status="failed")
                except Exception:  # noqa: BLE001 — selfgraph is non-fatal
                    pass
            raise


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class Agent:
    """A minimal agent. Compose; don't subclass.

    Example::

        agent = Agent(
            name="research",
            instructions="Find facts and cite sources.",
            model=auto_backend(),
            tools=[fetch, search],
            capabilities={Capability.NET_HTTP, Capability.LLM_INFERENCE},
        )
        result = await agent.run("What is the boiling point of mercury?")
    """

    def __init__(
        self,
        *,
        name: str,
        model: ModelBackend,
        instructions: str = "",
        tools: list[Tool] | None = None,
        memory: Memory | None = None,
        capabilities: set[Capability] | None = None,
        loop: AgentLoop | None = None,
        tracer: Tracer | None = None,
        recall_memory: bool = True,
        remember_interactions: bool = False,
    ) -> None:
        self.name = name
        self.model = model
        self.instructions = instructions
        self.tools = list(tools or [])
        self.memory = memory if memory is not None else InMemoryStore()
        # When True (and memory is a TypedMemory), each run injects
        # authority-ranked recall + active decisions/corrections into the
        # system prompt. No-op for plain stores.
        self.recall_memory = recall_memory
        # When True, each completed run is stored back as an interaction memory.
        self.remember_interactions = remember_interactions
        # Always grant LLM_INFERENCE by default; user can revoke if they want
        # to enforce dry-run mode.
        self._caps = CapabilityContext(capabilities or set()).grant(
            Capability.LLM_INFERENCE
        )
        self.loop = loop or ReActLoop()
        self.tracer = tracer or get_tracer()

    @property
    def capabilities(self) -> CapabilityContext:
        return self._caps

    def grant(self, *caps: Capability) -> Agent:
        self._caps.grant(*caps)
        return self

    async def run(self, prompt: str) -> AgentResult:
        # Runtime-validate input at the model-facing method boundary (NOOA typed-I/O).
        if not isinstance(prompt, str):
            raise TypeError(
                f"Agent.run(prompt) must be str, got {type(prompt).__name__}"
            )
        if not prompt.strip():
            raise ValueError("Agent.run(prompt) must be a non-empty string")
        _log.info(
            "agent.run.start",
            extra={"agent": self.name, "prompt_len": len(prompt)},
        )
        with use_context(self._caps):
            result = await self.loop.run(self, prompt)
        # Validate the pluggable loop honored its declared `-> AgentResult` contract.
        if not isinstance(result, AgentResult):
            raise TypeError(
                f"AgentLoop.run must return AgentResult, got {type(result).__name__}"
            )
        _log.info(
            "agent.run.end",
            extra={
                "agent": self.name,
                "steps": result.steps,
                "finish": result.finish_reason,
            },
        )
        return result
