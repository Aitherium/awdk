"""DeepSeek Coder pack — fill-in-the-middle, repo-level context, correct prompts.

Derived from https://github.com/deepseek-ai/DeepSeek-Coder (code: MIT; models
under DeepSeek's separate Model License, which permits commercial use). See NOTICE.

What this pack is FOR
---------------------
The platform's chat models answer questions about code. This family does two
things they cannot:

**Fill-in-the-middle.** Given the code before a gap and the code after it, the
model writes the middle. That is what an inline completion in an editor actually
needs, and no chat model does it — ask one to "fill this in" and it rewrites the
surrounding lines too.

**Repo-level completion.** These models were pre-trained on dependency-ordered
concatenations of whole projects, with ``#path`` file markers. Packed that way,
the model resolves a class defined in another file. Packed as a bare pile of
files, it does not.

Everything shape-related lives in :mod:`.formats`, which is pure and testable.
This module adds the thin execution layer over an OpenAI-compatible endpoint.

The traps are the point
-----------------------
Every way to misuse this family fails silently — a wrong FIM marker, the suffix
on the wrong side of the hole, or an instruct model left on its default stop
token all produce a confident, fluent, wrong answer with no error. That is why
:func:`dsc_traps` exists as a tool: the failure modes are worth more to an agent
than another wrapper around a POST.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from .formats import (
    EOS_COMPLETION,
    EOS_INSTRUCT,
    build_chat_prompt,
    build_fim_prompt,
    build_instruction_prompt,
    build_repo_context,
    dependency_graph,
    describe_traps,
    eos_token_id,
    order_by_dependency,
    parse_fim_completion,
)

logger = logging.getLogger("deepseek_coder_pack")

PACK_ID = "deepseek-coder"

#: The released family. `base` completes, `instruct` converses; both can infill.
MODELS: Dict[str, Dict[str, Any]] = {
    "1.3b": {
        "base": "deepseek-ai/deepseek-coder-1.3b-base",
        "instruct": "deepseek-ai/deepseek-coder-1.3b-instruct",
        "vram_gb_bf16": 3,
    },
    "5.7b": {
        "base": "deepseek-ai/deepseek-coder-5.7bmqa-base",
        "instruct": None,  # never released
        "vram_gb_bf16": 12,
    },
    "6.7b": {
        "base": "deepseek-ai/deepseek-coder-6.7b-base",
        "instruct": "deepseek-ai/deepseek-coder-6.7b-instruct",
        "vram_gb_bf16": 14,
    },
    "33b": {
        "base": "deepseek-ai/deepseek-coder-33b-base",
        "instruct": "deepseek-ai/deepseek-coder-33b-instruct",
        "vram_gb_bf16": 68,
    },
}

#: Context window. 16K via the extended pre-training stage.
CONTEXT_TOKENS = 16384

_TOOL_NAMES = [
    "dsc_infill",
    "dsc_repo_context",
    "dsc_prompt",
    "dsc_models",
    "dsc_traps",
]


def _endpoint() -> str:
    """The OpenAI-compatible endpoint serving this family, if configured."""
    return (
        os.environ.get("AITHER_DEEPSEEK_CODER_URL")
        or os.environ.get("AITHER_LOCAL_INFERENCE_URL")
        or ""
    )


def dsc_models(size: str = "") -> Dict[str, Any]:
    """List the DeepSeek Coder family, or one size.

    Sizes are 1.3b, 5.7b, 6.7b, 33b. Note 5.7b has NO instruct release — asking
    for one gets null rather than a silently substituted different size.
    """
    if not size:
        return {"ok": True, "models": MODELS, "context_tokens": CONTEXT_TOKENS}
    entry = MODELS.get(size.lower().strip())
    if entry is None:
        return {"ok": False, "reason": f"unknown size {size!r}", "sizes": list(MODELS)}
    return {"ok": True, "size": size, **entry, "context_tokens": CONTEXT_TOKENS}


def dsc_traps() -> Dict[str, Any]:
    """The silent failure modes of this model family, as data.

    Read this before driving the model directly. Every entry is a way to get a
    fluent wrong answer with nothing logged anywhere.
    """
    return {"ok": True, "traps": describe_traps()}


def dsc_prompt(
    mode: str = "chat",
    text: str = "",
    prefix: str = "",
    suffix: str = "",
    model: str = "deepseek-ai/deepseek-coder-6.7b-instruct",
) -> Dict[str, Any]:
    """Build a correctly-formatted prompt without sending it.

    Args:
        mode: "chat" | "instruct" | "fim" | "completion".
        text: The instruction or prompt, for chat/instruct/completion.
        prefix / suffix: The code either side of the gap, for fim.
        model: Used only to resolve the right stop-token id.

    Returns the prompt AND the ``eos_token_id`` it must be sent with — they are
    a pair, and sending the prompt with the wrong stop token is the failure this
    function exists to prevent.
    """
    mode = (mode or "chat").lower().strip()
    if mode == "fim":
        if not prefix and not suffix:
            return {"ok": False, "reason": "fim needs a prefix and/or a suffix"}
        prompt = build_fim_prompt(prefix, suffix)
    elif mode == "instruct":
        prompt = build_instruction_prompt(text)
    elif mode == "completion":
        prompt = text
    elif mode == "chat":
        prompt = build_chat_prompt([{"role": "user", "content": text}])
    else:
        return {
            "ok": False,
            "reason": f"unknown mode {mode!r}",
            "modes": ["chat", "instruct", "fim", "completion"],
        }

    return {
        "ok": True,
        "mode": mode,
        "prompt": prompt,
        "eos_token_id": eos_token_id(model, mode),
        "note": (
            "send the prompt with THIS eos_token_id; an instruct model on its "
            f"default ({EOS_INSTRUCT}) halts raw completion immediately, which "
            f"reads as a weak model rather than a wrong stop token ({EOS_COMPLETION})"
        ),
    }


def dsc_repo_context(
    files: Optional[Dict[str, str]] = None,
    root: str = "",
    pattern: str = "*.py",
    max_files: int = 40,
) -> Dict[str, Any]:
    """Pack a project into one dependency-ordered prompt with #path markers.

    Either pass ``files`` as ``{path: source}``, or a ``root`` to read from.
    Dependency cycles are REPORTED, not silently broken — which edge gets
    dropped changes the packed order, and a caller should know it is approximate.
    """
    if files is None:
        if not root:
            return {"ok": False, "reason": "pass either files={path: source} or root="}
        from pathlib import Path

        base = Path(root)
        if not base.is_dir():
            return {"ok": False, "reason": f"not a directory: {root}"}
        files = {}
        for path in sorted(base.rglob(pattern))[:max_files]:
            try:
                files[str(path.relative_to(base))] = path.read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError as exc:
                logger.debug("dsc_repo_context: skipping %s: %s", path, exc)

    if not files:
        return {"ok": False, "reason": "no files to pack", "context": ""}

    ordered, cycles = order_by_dependency(files)
    context = build_repo_context(files, ordered)
    graph = dependency_graph(files)
    return {
        "ok": True,
        "files": len(files),
        "order": ordered,
        "cycles": cycles,
        # The graph is returned alongside the packed string, not just used to
        # build it. It is the structural view of the codebase — which file
        # depends on which — and it cannot be recovered from the packed text.
        # A change-capture or world-model layer wants exactly this: the state
        # a code change is a transition between.
        "graph": graph["graphs"],
        "in_degree": graph["in_degree"],
        "approx_tokens": len(context) // 4,
        "context_limit_tokens": CONTEXT_TOKENS,
        "context": context,
    }


async def dsc_infill(
    prefix: str,
    suffix: str = "",
    model: str = "deepseek-ai/deepseek-coder-6.7b-base",
    max_tokens: int = 256,
    endpoint: str = "",
) -> Dict[str, Any]:
    """Fill in the code between ``prefix`` and ``suffix`` (fill-in-the-middle).

    This is the capability a chat model cannot provide: ask one to fill a gap
    and it rewrites your surrounding lines. Requires an endpoint serving a
    DeepSeek Coder model — set ``AITHER_DEEPSEEK_CODER_URL`` or pass one.
    """
    url = (endpoint or _endpoint()).rstrip("/")
    if not url:
        # Loud and actionable, never a silent empty completion.
        return {
            "ok": False,
            "reason": "no endpoint configured",
            "detail": "set AITHER_DEEPSEEK_CODER_URL to an OpenAI-compatible /v1 base",
        }

    prompt = build_fim_prompt(prefix, suffix)
    body = {
        "model": model,
        "prompt": prompt,
        "max_tokens": max_tokens,
        # FIM is raw completion: an instruct model on its default stop token
        # would halt at the first turn boundary.
        "stop": [],
        "temperature": 0.0,
    }

    try:
        import httpx

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(f"{url}/v1/completions", json=body)
        if response.status_code != 200:
            return {
                "ok": False,
                "reason": f"endpoint returned {response.status_code}",
                "detail": response.text[:300],
            }
        data = response.json()
    except Exception as exc:  # noqa: BLE001 - a tool must not sink the agent
        logger.warning("dsc_infill failed: %s", exc)
        return {"ok": False, "reason": exc.__class__.__name__, "detail": str(exc)[:300]}

    choices = data.get("choices") or []
    raw = str((choices[0] or {}).get("text") or "") if choices else ""
    return {
        "ok": True,
        "model": data.get("model") or model,
        "completion": parse_fim_completion(raw, prompt),
        "raw_chars": len(raw),
    }


def register(registry) -> int:
    """Register the pack's tools. One bad tool never sinks the pack."""
    registered = 0
    for name in _TOOL_NAMES:
        fn = globals().get(name)
        if not callable(fn):
            logger.debug("deepseek_coder: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            registered += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("deepseek_coder: skip tool %s: %s", name, exc)

    logger.info("deepseek-coder pack registered %d tools", registered)
    return registered


__all__ = [
    "PACK_ID", "MODELS", "CONTEXT_TOKENS", "register",
    "dsc_infill", "dsc_repo_context", "dsc_prompt", "dsc_models", "dsc_traps",
    "build_fim_prompt", "build_instruction_prompt", "build_chat_prompt",
    "build_repo_context", "order_by_dependency", "dependency_graph",
    "parse_fim_completion", "eos_token_id", "describe_traps",
]
