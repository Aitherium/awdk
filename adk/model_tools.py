"""Stronger models as TOOLS of the orchestrator.

The agent's own model is small and fast: it drives the turn, picks tools and talks. A step
that needs careful reasoning, exact syntax it is unsure of, or eyes on an image is handed
to a bigger model through the same LLM route every call takes (MicroScheduler when it is
configured), and the answer comes back as a tool result the orchestrator acts on.

Measured 2026-10-05 on the owner's desk: the 8B orchestrator asked PowerShell for a
``FreeSpace`` property that does not exist, 5 runs out of 5. deepseek-v4-flash wrote
``Get-PSDrive C | Select-Object Used,Free`` every time, but running deepseek as the WHOLE
agent made every turn wait on a shared pool (203 s average under load). Owner, same day:
"the agent/orchestrator should call the deepseek/gemma models as vision/reasoning as
TOOLS". This module is that.

Models come from config (``AITHER_REASONING_MODEL`` / ``AITHER_PERCEPTION_MODEL``), with
defaults for this fleet. A failure is returned as ``{"error": ...}``, never raised, so the
orchestrator can say what went wrong instead of the turn dying.
"""

from __future__ import annotations

import asyncio
import json
import os

REASONING_DEFAULT = "deepseek-v4-flash"
VISION_DEFAULT = "bonsai2-27b"
_TIMEOUT_S = float(os.getenv("AITHER_MODEL_TOOL_TIMEOUT_S", "180"))

_router = None


def _get_router():
    global _router
    if _router is None:
        from adk.config import Config
        from adk.llm import LLMRouter

        _router = LLMRouter(config=Config.from_env())
    return _router


def _model(env: str, default: str) -> str:
    try:
        from adk.config import Config

        cfg = Config.from_env()
        attr = "reasoning_model" if env == "AITHER_REASONING_MODEL" else "perception_model"
        return (getattr(cfg, attr, "") or os.getenv(env, "") or default).strip()
    except Exception:  # noqa: BLE001 -- config trouble must not hide the tool
        return (os.getenv(env, "") or default).strip()


async def _ask(messages: list, model: str, max_tokens: int) -> str:
    router = _get_router()
    resp = await asyncio.wait_for(
        router.chat(messages, model=model, max_tokens=max_tokens, temperature=0.2),
        timeout=_TIMEOUT_S,
    )
    content = getattr(resp, "content", "") or ""
    if isinstance(content, list):
        content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return str(content).strip()


async def ask_reasoner(question: str, context: str = "") -> str:
    """Ask a stronger reasoning model and get its answer back. Use it when a step needs
    careful reasoning, math, code, or EXACT command syntax you are not sure of (for
    example the right PowerShell to read a value), instead of guessing.

    question: What you need answered, self-contained.
    context: Optional facts it needs: tool output, errors, the user's OS or shell.
    """
    q = (question or "").strip()
    if not q:
        return json.dumps({"error": "question is empty"})
    from adk.llm.base import Message

    model = _model("AITHER_REASONING_MODEL", REASONING_DEFAULT)
    msgs = [
        Message(role="system", content=(
            "You are the reasoning tool of a smaller agent. Answer exactly and briefly. When "
            "asked for a command, give the one command that works, then one line on why.")),
        Message(role="user", content=q + (
            f"\n\nContext:\n{context.strip()[:6000]}" if context else "")),
    ]
    try:
        answer = await _ask(msgs, model, 1200)
    except asyncio.TimeoutError:
        return json.dumps({"error": f"{model} did not answer within {int(_TIMEOUT_S)} s",
                           "model": model})
    except Exception as e:  # noqa: BLE001 -- surfaced to the agent, not raised
        return json.dumps({"error": f"{type(e).__name__}: {e}", "model": model})
    if not answer:
        return json.dumps({"error": f"{model} returned an empty answer", "model": model})
    return json.dumps({"model": model, "answer": answer})


async def look_at(image: str, question: str = "Describe what is in this image.") -> str:
    """Look at an image with a vision model and answer a question about it.

    image: A local file path or an http(s) URL of the image.
    question: What to find out from the image.
    """
    src = (image or "").strip()
    if not src:
        return json.dumps({"error": "no image given"})
    if not src.lower().startswith(("http://", "https://", "data:")) and not os.path.isfile(src):
        return json.dumps({"error": f"image not found: {src}"})
    from adk.llm.multimodal import image_message

    model = _model("AITHER_PERCEPTION_MODEL", VISION_DEFAULT)
    try:
        answer = await _ask([image_message(question or "Describe this image.", src)], model, 800)
    except asyncio.TimeoutError:
        return json.dumps({"error": f"{model} did not answer within {int(_TIMEOUT_S)} s",
                           "model": model})
    except Exception as e:  # noqa: BLE001 -- surfaced to the agent, not raised
        return json.dumps({"error": f"{type(e).__name__}: {e}", "model": model})
    if not answer:
        return json.dumps({"error": f"{model} returned an empty answer", "model": model})
    return json.dumps({"model": model, "answer": answer})


__all__ = ["ask_reasoner", "look_at", "REASONING_DEFAULT", "VISION_DEFAULT"]
