"""Zero-libc / ``awzeal`` LLM provider (``asm-llm`` subprocess & loopback HTTP).

Supports two local execution modes without hard dependencies on ``lib.*``,
``awseal``, ``awrun``, or unmerged external trees:
1. Subprocess mode (``AWZEAL_BINARY`` / ``ASM_LLM_BINARY``): runs a zero-libc
   inference binary with ChatML prompt on stdin and completion on stdout,
   passing ``--shared-ro-mmap`` and ``--cpu-pin`` when configured.
2. Loopback HTTP mode (``AWZEAL_URL`` / ``AWZEAL_ENDPOINT``): calls a local
   OpenAI-compatible ``/v1/chat/completions`` endpoint via ``httpx``.

When ``model_sha256`` (or ``AWZEAL_MODEL_SHA256``) is provided, the local
model file is verified fail-closed before inference runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator

from .base import (
    LLMProvider,
    LLMResponse,
    Message,
    ProviderCapabilities,
    StreamChunk,
    ToolCall,
    _timer,
    messages_to_dicts,
)

logger = logging.getLogger("adk.llm.awzeal")

_MODEL_FILE_EXTS = frozenset(
    {".gguf", ".bin", ".awzeal", ".asm", ".pq2", ".ptq", ".safetensors"}
)


def _validate_safe_cli_value(
    value: str,
    label: str,
    *,
    allow_leading_dash: bool = False,
) -> str:
    if "\x00" in value:
        raise ValueError(f"{label} must not contain null bytes: {value!r}")
    if not allow_leading_dash and value.startswith("-"):
        raise ValueError(f"{label} must not start with '-': {value!r}")
    return value


def _check_model_dir_boundary(candidate: str) -> None:
    model_dir = os.getenv("AWZEAL_MODEL_DIR", "").strip()
    if not model_dir:
        return
    try:
        root_resolved = Path(model_dir).expanduser().resolve()
        cand_resolved = Path(candidate).expanduser().resolve()
    except OSError as exc:
        raise ValueError(f"Invalid Awzeal model path {candidate!r}: {exc}") from exc
    if not cand_resolved.is_relative_to(root_resolved):
        raise ValueError(
            f"Awzeal model path {candidate!r} escapes AWZEAL_MODEL_DIR ({model_dir!r})"
        )


def _is_allowed_model_file(candidate: str) -> bool:
    if not candidate or "\x00" in candidate or candidate.startswith("-"):
        return False
    p = Path(candidate)
    if p.suffix.lower() not in _MODEL_FILE_EXTS:
        return False
    if not os.path.isfile(candidate):
        return False
    _check_model_dir_boundary(candidate)
    return True


def _build_chatml(messages: list[Message]) -> str:
    """Convert ``Message`` objects into ChatML format for zero-libc stdin."""
    parts: list[str] = []
    for msg in messages:
        if isinstance(msg.content, str):
            text = msg.content
        else:
            text = json.dumps(msg.content, ensure_ascii=False)
        parts.append(f"<|im_start|>{msg.role}\n{text}<|im_end|>")
    parts.append("<|im_start|>assistant\n")
    return "\n".join(parts)


def _coerce_args_dict(raw_args: Any) -> dict[str, Any]:
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {}


def _parse_subprocess_tool_calls(stdout_text: str) -> tuple[list[ToolCall], str, str]:
    content = stdout_text
    tool_calls: list[ToolCall] = []
    finish_reason = "stop"
    stripped = content.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                if isinstance(parsed.get("tool_calls"), list):
                    for idx, tc in enumerate(parsed["tool_calls"]):
                        if not isinstance(tc, dict):
                            continue
                        fn = tc.get("function", tc)
                        if not isinstance(fn, dict):
                            continue
                        name = str(fn.get("name") or "")
                        if not name:
                            continue
                        tool_calls.append(
                            ToolCall(
                                id=str(tc.get("id") or f"call_{name}_{idx}"),
                                name=name,
                                arguments=_coerce_args_dict(
                                    fn.get("arguments", fn.get("parameters", {}))
                                ),
                            )
                        )
                    if tool_calls:
                        content = str(parsed.get("content") or "")
                        finish_reason = "tool_calls"
                    elif "content" in parsed:
                        raw_content = parsed.get("content")
                        content = "" if raw_content is None else str(raw_content)
                elif parsed.get("name"):
                    name = str(parsed["name"])
                    tool_calls.append(
                        ToolCall(
                            id=str(parsed.get("id") or name),
                            name=name,
                            arguments=_coerce_args_dict(
                                parsed.get("arguments", parsed.get("parameters", {}))
                            ),
                        )
                    )
                    content = ""
                    finish_reason = "tool_calls"
        except json.JSONDecodeError:
            pass
    return tool_calls, content, finish_reason


def _parse_openai_tool_calls(raw_calls: Any) -> list[ToolCall]:
    if not isinstance(raw_calls, list):
        return []
    out: list[ToolCall] = []
    for idx, tc in enumerate(raw_calls):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function", tc)
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "")
        if not name:
            continue
        out.append(
            ToolCall(
                id=str(tc.get("id") or f"call_{name}_{idx}"),
                name=name,
                arguments=_coerce_args_dict(fn.get("arguments", {})),
            )
        )
    return out


async def _terminate_and_reap(proc: asyncio.subprocess.Process | None) -> None:
    """Kill and reap a subprocess and close its transport pipes cleanly."""
    if proc is None:
        return
    needs_wait = proc.returncode is None
    if needs_wait:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    if proc.stdin is not None:
        try:
            if not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        try:
            transport.close()
        except Exception:
            pass
    if needs_wait:
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass


class AwzealZeroLibcProvider(LLMProvider):
    """Zero-libc inference provider for ``awzeal`` / ``asm-llm`` and loopback endpoints."""

    def __init__(
        self,
        binary: str = "",
        model: str = "",
        *,
        model_path: str = "",
        endpoint: str = "",
        base_url: str = "",
        url: str = "",
        model_sha256: str | None = None,
        shared_ro_mmap: bool | None = None,
        target_surface: str | None = None,
        threads: int | None = None,
        cpu_pin: str = "",
        timeout: float = 120.0,
        api_key: str = "",
        transport: Any = None,
        allow_model_path_coercion: bool = True,
    ) -> None:
        self.binary = _validate_safe_cli_value(
            (binary or os.getenv("AWZEAL_BINARY", "") or os.getenv("ASM_LLM_BINARY", "")).strip(),
            "Awzeal binary",
            allow_leading_dash=False,
        )
        raw_ep = (
            endpoint
            or base_url
            or url
            or os.getenv("AWZEAL_URL", "")
            or os.getenv("AWZEAL_ENDPOINT", "")
        )
        self.endpoint = raw_ep.strip().rstrip("/")
        self.base_url = self.endpoint

        env_model_name = _validate_safe_cli_value(
            os.getenv("AWZEAL_MODEL", "").strip(),
            "AWZEAL_MODEL",
        )
        env_model_path = _validate_safe_cli_value(
            os.getenv("AWZEAL_MODEL_PATH", "").strip(),
            "AWZEAL_MODEL_PATH",
        )
        model_arg = _validate_safe_cli_value((model or "").strip(), "Awzeal model")
        explicit_path = _validate_safe_cli_value(
            (model_path or "").strip(),
            "Awzeal model_path",
        )

        if explicit_path:
            _check_model_dir_boundary(explicit_path)
            self.model_path = explicit_path
        elif model_arg and _is_allowed_model_file(model_arg):
            if not allow_model_path_coercion:
                raise ValueError(
                    f"Awzeal model {model_arg!r} cannot coerce a host file path; "
                    "configure AWZEAL_MODEL_PATH instead"
                )
            self.model_path = model_arg
        elif model_arg and os.path.exists(model_arg):
            raise ValueError(
                f"Awzeal model {model_arg!r} is not an allowed model file path; "
                "configure AWZEAL_MODEL_PATH instead"
            )
        elif env_model_path:
            _check_model_dir_boundary(env_model_path)
            self.model_path = env_model_path
        elif env_model_name and _is_allowed_model_file(env_model_name):
            self.model_path = env_model_name
        else:
            self.model_path = ""

        self.model = model_arg or env_model_name or env_model_path
        if model_arg and self.model_path == model_arg:
            self.default_model = os.path.basename(model_arg)
        elif model_arg:
            self.default_model = model_arg
        elif (
            env_model_name
            and not os.path.isfile(env_model_name)
            and "/" not in env_model_name
            and "\\" not in env_model_name
        ):
            self.default_model = env_model_name
        elif self.model_path:
            self.default_model = os.path.basename(self.model_path)
        else:
            self.default_model = "awzeal"

        raw_sha = (
            model_sha256
            if model_sha256 is not None
            else os.getenv("AWZEAL_MODEL_SHA256", "")
        )
        self.model_sha256: str | None = (str(raw_sha or "").strip().lower()) or None

        if shared_ro_mmap is not None:
            self.shared_ro_mmap = bool(shared_ro_mmap)
        else:
            self.shared_ro_mmap = (
                os.getenv("AWZEAL_SHARED_RO_MMAP", "").strip().lower()
                in ("1", "true", "yes", "on")
            )

        self.target_surface = (
            target_surface or os.getenv("AWZEAL_TARGET_SURFACE", "") or "standard"
        ).strip().lower()

        if threads is not None:
            self.threads = int(threads)
        else:
            raw_threads = os.getenv("AWZEAL_THREADS", "").strip()
            try:
                self.threads = int(raw_threads) if raw_threads else 4
            except ValueError:
                self.threads = 4

        self.cpu_pin = _validate_safe_cli_value(
            (cpu_pin or os.getenv("AWZEAL_CPU_PIN", "")).strip(),
            "Awzeal cpu_pin",
        )
        self.timeout = float(timeout)
        self.api_key = (api_key or os.getenv("AWZEAL_API_KEY", "")).strip()
        self._transport = transport

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(prompt_cache="none", batch=False)

    def verify_model_sha256(self) -> None:
        """Fail-closed SHA-256 check when ``model_sha256`` is configured."""
        if not self.model_sha256:
            return
        if not self.model_path or not os.path.isfile(self.model_path):
            raise RuntimeError(
                "Awzeal model_sha256 verification failed: model file missing "
                f"({self.model_path or self.model!r})"
            )
        digest = hashlib.sha256()
        with open(self.model_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest().lower()
        if actual != self.model_sha256:
            raise RuntimeError(
                f"Awzeal model_sha256 mismatch for {self.model_path}: "
                f"expected {self.model_sha256}, got {actual}"
            )

    def _endpoint_urls(self) -> tuple[str, str]:
        ep = self.endpoint.rstrip("/")
        if ep.endswith("/v1"):
            return ep[:-3], ep
        return ep, f"{ep}/v1"

    async def _chat_subprocess(
        self,
        messages: list[Message],
        *,
        model: str | None,
        temperature: float,
        max_tokens: int,
        tools: list[dict] | None,
    ) -> LLMResponse:
        target_model = self.model_path or self.model
        if not target_model:
            raise RuntimeError(
                "Awzeal model not configured. Set AWZEAL_MODEL / AWZEAL_MODEL_PATH."
            )
        _validate_safe_cli_value(target_model, "Awzeal model")
        self.verify_model_sha256()

        chatml = _build_chatml(messages)
        cmd = [
            self.binary,
            target_model,
            "-n",
            str(max_tokens),
            "-t",
            str(temperature),
            "-j",
            str(self.threads),
        ]
        if self.shared_ro_mmap:
            cmd.append("--shared-ro-mmap")
        if self.cpu_pin:
            cmd.extend(["--cpu-pin", self.cpu_pin])
        if tools:
            cmd.append("--json")

        proc: asyncio.subprocess.Process | None = None
        start = _timer()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=chatml.encode("utf-8")),
                timeout=self.timeout,
            )
            stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            if proc.returncode != 0:
                raise RuntimeError(
                    f"Awzeal exited with code {proc.returncode}: {stderr_text}"
                )
            if stderr_text:
                logger.debug("Awzeal stderr: %s", stderr_text)
        except asyncio.TimeoutError as exc:
            await _terminate_and_reap(proc)
            raise RuntimeError(f"Awzeal timed out after {self.timeout}s") from exc
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Awzeal binary not found at '{self.binary}'. "
                "Ensure AWZEAL_BINARY or ASM_LLM_BINARY points to the executable."
            ) from exc
        except OSError as exc:
            await _terminate_and_reap(proc)
            raise RuntimeError(f"Awzeal subprocess failed: {exc}") from exc
        except BaseException:
            await _terminate_and_reap(proc)
            raise

        latency = _timer() - start
        content = stdout_text
        tool_calls: list[ToolCall] = []
        finish_reason = "stop"
        if tools:
            tool_calls, content, finish_reason = _parse_subprocess_tool_calls(stdout_text)

        est_prompt = len(chatml) // 4
        est_completion = len(content) // 4
        chosen_model = (model or "").strip() or self.default_model
        return LLMResponse(
            content=content,
            model=chosen_model,
            tokens_used=est_prompt + est_completion,
            prompt_tokens=est_prompt,
            completion_tokens=est_completion,
            latency_ms=latency,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )

    async def _chat_http(
        self,
        messages: list[Message],
        *,
        model: str | None,
        temperature: float,
        max_tokens: int,
        tools: list[dict] | None,
        tool_choice: str | dict | None,
        top_p: float | None,
        kwargs: dict[str, Any],
    ) -> LLMResponse:
        if self.model_sha256 and self.model_path:
            self.verify_model_sha256()

        import httpx

        _, v1_base = self._endpoint_urls()
        url = f"{v1_base}/chat/completions"
        chosen_model = (model or "").strip() or self.default_model
        payload: dict[str, Any] = {
            "model": chosen_model,
            "messages": messages_to_dicts(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if top_p is not None:
            payload["top_p"] = top_p
        if tools:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if kwargs.get("response_format") is not None:
            payload["response_format"] = kwargs["response_format"]

        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        start = _timer()
        client_kwargs: dict[str, Any] = {"timeout": self.timeout}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Awzeal HTTP request failed ({url}): {exc}") from exc

        latency = _timer() - start
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Awzeal HTTP {resp.status_code} from {url}: {resp.text[:300]}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"Awzeal HTTP returned invalid JSON: {exc}") from exc

        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("Awzeal HTTP response missing 'choices'")
        choice = choices[0] if isinstance(choices[0], dict) else {}
        msg = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        content = str(msg.get("content") or "")
        tool_calls = _parse_openai_tool_calls(msg.get("tool_calls"))
        finish_reason = str(
            choice.get("finish_reason") or ("tool_calls" if tool_calls else "stop")
        )
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        tt = int(usage.get("total_tokens") or (pt + ct))
        return LLMResponse(
            content=content,
            model=str(data.get("model") or chosen_model),
            tokens_used=tt,
            prompt_tokens=pt,
            completion_tokens=ct,
            latency_ms=latency,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )

    async def chat(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        top_p: float | None = None,
        repetition_penalty: float | None = None,
        cache: bool = False,
        **kwargs,
    ) -> LLMResponse:
        if self.binary:
            return await self._chat_subprocess(
                messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tools,
            )
        if self.endpoint:
            return await self._chat_http(
                messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tools,
                tool_choice=tool_choice,
                top_p=top_p,
                kwargs=kwargs,
            )
        raise RuntimeError(
            "Awzeal zero-libc backend not configured. "
            "Set AWZEAL_BINARY / ASM_LLM_BINARY or AWZEAL_URL."
        )

    async def chat_stream(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        top_p: float | None = None,
        repetition_penalty: float | None = None,
        cache: bool = False,
        **kwargs,
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
        yield StreamChunk(
            content=resp.content,
            done=True,
            model=resp.model,
            tool_calls=resp.tool_calls,
            finish_reason=resp.finish_reason,
        )

    async def list_models(self) -> list[str]:
        if self.binary:
            if self.model or self.model_path:
                return [self.default_model]
            return []
        if self.endpoint:
            import httpx

            _, v1_base = self._endpoint_urls()
            headers: dict[str, str] = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            client_kwargs: dict[str, Any] = {"timeout": min(self.timeout, 10.0)}
            if self._transport is not None:
                client_kwargs["transport"] = self._transport
            try:
                async with httpx.AsyncClient(**client_kwargs) as client:
                    resp = await client.get(f"{v1_base}/models", headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("data") if isinstance(data, dict) else None
                    if isinstance(items, list):
                        ids = [
                            str(item["id"])
                            for item in items
                            if isinstance(item, dict) and item.get("id")
                        ]
                        if ids:
                            return ids
            except Exception:
                pass
            if self.model or self.model_path:
                return [self.default_model]
        return []

    async def health_check(self) -> bool:
        if self.binary:
            if not os.path.isfile(self.binary):
                return False
            if self.model_sha256:
                try:
                    self.verify_model_sha256()
                except RuntimeError:
                    return False
                return True
            if self.model_path:
                return os.path.isfile(self.model_path)
            return bool(self.model)
        if self.endpoint:
            import httpx

            base, v1_base = self._endpoint_urls()
            headers: dict[str, str] = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            client_kwargs: dict[str, Any] = {"timeout": min(self.timeout, 10.0)}
            if self._transport is not None:
                client_kwargs["transport"] = self._transport
            try:
                async with httpx.AsyncClient(**client_kwargs) as client:
                    for probe_url in (f"{base}/health", f"{v1_base}/models"):
                        resp = await client.get(probe_url, headers=headers)
                        if resp.status_code == 200:
                            return True
            except Exception:
                return False
            return False
        return False
