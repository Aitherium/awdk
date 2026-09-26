"""A synchronous ModelBackend over a fleet MicroScheduler (OpenAI-shaped ``/v1``).

Every LLM call in a fleet goes through its MicroScheduler, which queues and routes
to the served models. This backend is the reasoning loop's and the ARC eval's way
in:

* **synchronous** ``chat(messages, max_tokens=..., temperature=...)`` -- the call
  shape a blocking reasoning loop uses (it returns :class:`ChatReply`); the async
  ``generate`` / ``stream`` of :class:`adk.core.model.ModelBackend` wrap it;
* **https with the internal CA**: ``verify`` comes from :func:`adk._tls.tls_verify`
  (the installed AitherNet CA bundle, else the system store). Verification is
  never switched off here;
* **counts tokens**: ``calls``, ``prompt_tokens``, ``completion_tokens`` and
  ``llm_s`` accumulate across calls (from the response's ``usage``; a reply
  without usage is estimated at 4 characters per token and flagged);
* **fails loudly**: an unreachable scheduler, a 5xx or a reply with no choices
  raises :class:`SchedulerUnavailableError` (``backend_dead = True``), which the
  ARC suite turns into exit 2 rather than a scored zero.

Where it points: ``base_url=`` > ``$AITHER_MICROSCHEDULER_URL`` > the in-fleet
name when running inside a container > ``https://127.0.0.1:8150`` on a host.

Which model: ``model=`` > ``$ADK_SOLVE_MODEL`` > ``"auto"``. ``"auto"`` asks the
scheduler for its model list and takes the first of :data:`PREFERRED_MODELS` it
serves (the strongest local model first; paid cloud routes are never auto-picked).
The choice is recorded in ``self.model`` and ``self.model_source``.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence, Union

from adk.core.model import Message, ModelResponse

__all__ = [
    "ChatReply",
    "MicroSchedulerBackend",
    "PREFERRED_MODELS",
    "CrossModelRouteError",
    "SchedulerUnavailableError",
    "default_scheduler_url",
]

HOST_URL = "https://127.0.0.1:8150"
FLEET_URL = "https://aitheros-microscheduler:8150"
URL_VAR = "AITHER_MICROSCHEDULER_URL"
MODEL_VAR = "ADK_SOLVE_MODEL"

#: ``"auto"`` takes the first of these the scheduler lists. Strongest first; all
#: are locally served (no per-token spend). Measured list on the reference fleet:
#: the 284B DeepSeek-V4 flash pool, then Bonsai-2 27B, then Gemma-4 12B.
PREFERRED_MODELS: tuple = (
    "deepseek-v4-flash-pool",
    "pool-284b",
    "v4-flash-pool",
    "bonsai2-27b",
    "gemma4-reasoning",
    "gemma4-12b",
)


class SchedulerUnavailableError(RuntimeError):
    """The scheduler cannot answer (down, 5xx, or an empty reply). Never a scored zero."""

    backend_dead = True

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class CrossModelRouteError(SchedulerUnavailableError):
    """The scheduler answered with a DIFFERENT model than the one requested.

    MicroScheduler falls a busy or down lane over to a cloud peer
    (``aither_route.cross_model``); an eval row scored on that answer measures the
    wrong model, so it is refused -- a dead backend, never a scored row. Set
    ``allow_cross_model=True`` on the backend to accept it (it is still counted).
    """

    def __init__(self, requested: str, served_by: str) -> None:
        super().__init__(
            "MicroScheduler answered '%s' with '%s' (cross-model route); refusing it"
            % (requested, served_by)
        )
        self.requested = requested
        self.served_by = served_by


@dataclass
class ChatReply:
    content: str
    usage: Dict[str, int] = field(default_factory=dict)
    latency_s: float = 0.0
    model: str = ""
    finish_reason: Optional[str] = None


def _in_container() -> bool:
    return Path("/run/.containerenv").exists() or Path("/.dockerenv").exists()


def default_scheduler_url() -> str:
    explicit = os.environ.get(URL_VAR, "").strip()
    if explicit:
        return explicit.rstrip("/")
    return FLEET_URL if _in_container() else HOST_URL


def _as_dicts(messages: Sequence[Union[Message, Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for m in messages:
        out.append(m.as_dict() if isinstance(m, Message) else dict(m))
    return out


class MicroSchedulerBackend:
    """``ModelBackend`` + a blocking ``chat()`` over a MicroScheduler."""

    name = "microscheduler"

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 300.0,
        token: Optional[str] = None,
        source: str = "adk.reasoning",
        allow_cross_model: bool = False,
        local_only: bool = True,
    ) -> None:
        self.base_url = (base_url or default_scheduler_url()).rstrip("/")
        self.timeout = float(timeout)
        self.token = token if token is not None else os.environ.get("AITHER_NODE_TOKEN", "")
        self.source = source
        wanted = (model or os.environ.get(MODEL_VAR, "") or "auto").strip()
        self.model = wanted
        self.model_source = "explicit" if wanted != "auto" else "unresolved"
        self.calls = 0
        self.errors = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.usage_estimated = 0
        self.llm_s = 0.0
        self.allow_cross_model = bool(allow_cross_model)
        self.cross_model_replies = 0
        # Owner rule 2026-09-25: reasoning-loop and eval runs are LOCAL ONLY. The
        # scheduler honours metadata.local_only BEFORE any cloud hop (a busy local
        # slot queues or fails retryably), so refusing a cross-model reply after
        # the fact is the backstop, not the guard -- by then the call was paid.
        self.local_only = bool(local_only)
        self._client: Any = None

    # -- transport -------------------------------------------------------------
    def _http(self) -> Any:
        if self._client is None:
            import httpx

            from adk._tls import tls_verify

            self._client = httpx.Client(
                base_url=self.base_url, timeout=self.timeout, verify=tls_verify()
            )
        return self._client

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = "Bearer %s" % self.token
        return h

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        import httpx

        try:
            r = self._http().request(method, path, headers=self._headers(), **kw)
        except httpx.HTTPError as exc:
            raise SchedulerUnavailableError(
                "MicroScheduler at %s unreachable: %s: %s"
                % (self.base_url, type(exc).__name__, exc)
            ) from exc
        if r.status_code >= 500:
            raise SchedulerUnavailableError(
                "MicroScheduler %s %s returned %d: %s"
                % (method, path, r.status_code, r.text[:200]),
                status=r.status_code,
            )
        if r.status_code >= 400:
            raise RuntimeError(
                "MicroScheduler %s %s refused (%d): %s"
                % (method, path, r.status_code, r.text[:300])
            )
        try:
            return r.json()
        except ValueError as exc:
            raise SchedulerUnavailableError(
                "MicroScheduler %s %s returned non-JSON: %s" % (method, path, r.text[:200])
            ) from exc

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- discovery -------------------------------------------------------------
    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/health")

    def list_models(self) -> List[str]:
        data = self._request("GET", "/v1/models")
        return [str(m.get("id")) for m in (data.get("data") or []) if m.get("id")]

    def resolve_model(self) -> str:
        """Pin ``self.model`` (querying the model list when it is ``"auto"``)."""
        if self.model != "auto":
            return self.model
        served = self.list_models()
        for name in PREFERRED_MODELS:
            if name in served:
                self.model = name
                self.model_source = "auto: first preferred model in /v1/models"
                return name
        raise SchedulerUnavailableError(
            "MicroScheduler at %s serves none of %s (it lists %s); pass model="
            % (self.base_url, list(PREFERRED_MODELS), served[:12])
        )

    # -- the blocking call -----------------------------------------------------
    def chat(
        self,
        messages: Sequence[Union[Message, Dict[str, Any]]],
        max_tokens: int = 1000,
        temperature: float = 0.4,
        extra: Optional[Dict[str, Any]] = None,
    ) -> ChatReply:
        model = self.resolve_model()
        payload: Dict[str, Any] = {
            "model": model,
            "messages": _as_dicts(messages),
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "stream": False,
            "metadata": {"source": self.source},
        }
        if self.local_only:
            payload["metadata"]["local_only"] = True
        if extra:
            extra = dict(extra)
            # Merge, never replace: an `extra` carrying its own metadata must not
            # silently drop the local_only opt-out.
            extra_meta = extra.pop("metadata", None)
            payload.update(extra)
            if isinstance(extra_meta, dict):
                payload["metadata"].update(extra_meta)
                if self.local_only:
                    payload["metadata"]["local_only"] = True
        t0 = time.perf_counter()
        try:
            data = self._request("POST", "/v1/chat/completions", json=payload)
        except Exception:
            self.errors += 1
            raise
        dt = time.perf_counter() - t0
        choices = data.get("choices") or []
        if not choices:
            self.errors += 1
            raise SchedulerUnavailableError("MicroScheduler returned no choices: %r" % (data,))
        route = data.get("aither_route") if isinstance(data.get("aither_route"), dict) else {}
        served_by = str(route.get("served_by") or data.get("model") or model)
        if route.get("cross_model") or (route and served_by != model):
            self.cross_model_replies += 1
            if not self.allow_cross_model:
                self.errors += 1
                raise CrossModelRouteError(model, served_by)
        msg = choices[0].get("message") or {}
        content = str(msg.get("content") or "")
        usage = {k: int(v) for k, v in (data.get("usage") or {}).items() if isinstance(v, int)}
        if not usage.get("prompt_tokens") and not usage.get("completion_tokens"):
            prompt_chars = sum(len(str(m.get("content", ""))) for m in payload["messages"])
            usage = {
                "prompt_tokens": max(1, prompt_chars // 4),
                "completion_tokens": max(1, len(content) // 4),
            }
            self.usage_estimated += 1
        self.calls += 1
        self.llm_s += dt
        self.prompt_tokens += usage.get("prompt_tokens", 0)
        self.completion_tokens += usage.get("completion_tokens", 0)
        return ChatReply(
            content=content,
            usage=usage,
            latency_s=dt,
            model=served_by,
            finish_reason=choices[0].get("finish_reason"),
        )

    def stats(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "model_source": self.model_source,
            "llm_calls": self.calls,
            "llm_errors": self.errors,
            "llm_s": round(self.llm_s, 3),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "tokens": self.prompt_tokens + self.completion_tokens,
            "usage_estimated": self.usage_estimated,
            "cross_model_replies": self.cross_model_replies,
            "local_only": self.local_only,
        }

    # -- ModelBackend ----------------------------------------------------------
    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **opts: Any,
    ) -> ModelResponse:
        reply = await asyncio.to_thread(
            self.chat, messages, max_tokens or 1000, temperature, opts or None
        )
        return ModelResponse(
            text=reply.content,
            model=reply.model,
            finish_reason=reply.finish_reason,
            usage=dict(reply.usage),
        )

    async def stream(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **opts: Any,
    ) -> AsyncIterator[str]:
        resp = await self.generate(messages, temperature=temperature, max_tokens=max_tokens, **opts)
        yield resp.text
