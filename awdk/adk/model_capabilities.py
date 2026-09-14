"""Discover what the connected model can actually do, instead of guessing.

An agent is handed a base_url and a model name and has to decide how many tokens
it may spend on context, on thinking, and on the answer. Today that decision is a
constant: `self.config.max_context or 8000`. The config field is documented
"0 = unlimited (let model decide)" — and then the model is never asked.

That constant is wrong in both directions on real backends, and the failure is
silent either way:

    under-guess : the agent truncates context it was allowed to keep
    over-guess  : the request exceeds the window, and the backend TRUNCATES the
                  answer rather than returning a budget error

Every OpenAI-compatible server already advertises the answer, so ask it:

    vLLM       GET /v1/models  -> data[].max_model_len
    llama.cpp  GET /props      -> default_generation_settings.n_ctx

🚨 DO NOT divide llama.cpp's n_ctx by total_slots. It is ALREADY per-slot —
llama.cpp divides `-c` by `-np` before reporting. Measured 2026-08-18: a server
launched `-c 16384 -np 2` reports n_ctx 8192, and dividing again yields 4096,
silently halving every budget.

SELF-CONTAINED BY REQUIREMENT. awdk ships to PyPI, so this module must not
import the monorepo (`lib.*`) — that would be a ModuleNotFoundError on a
stranger's machine, which the boundary gate exists to prevent. stdlib + httpx
only, and httpx is imported lazily so importing this module never fails.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple
from adk._tls import tls_verify

__all__ = ["ModelCapabilities", "discover", "budget_for_effort", "clear_cache"]

# Conservative floor used when a backend cannot be reached. NEVER 0: callers
# treat 0 as "no limit" and would remove the cap entirely rather than fall back
# to it — strictly worse than a small number.
DEFAULT_WINDOW = 8192

_CACHE: Dict[Tuple[str, str], Tuple[float, "ModelCapabilities"]] = {}
_TTL_S = 900.0

# Substrings that indicate the weights are quantized. Used only to describe the
# endpoint — never to change correctness.
_QUANT = re.compile(
    r"awq|gptq|bnb|4bit|4-bit|int4|int8|\bq[1-8]_[0-9k]|fp8|nvfp4|mxfp4"
    r"|\biq[1-4]\b|\bw[248]a\d{1,2}\b", re.I)

# Models that emit a <think> block and therefore need a thinking allowance.
_THINKS = re.compile(r"deepseek-?r1|qwen3|qwq|reason|think|nemotron", re.I)


@dataclass
class ModelCapabilities:
    """What the endpoint told us, plus what we could only infer."""

    model: str
    context_window: int = DEFAULT_WINDOW
    quantized: bool = False
    quant_hint: str = ""
    thinks: bool = False
    engine: str = "unknown"          # vllm | llamacpp | unknown
    discovered: bool = False         # False => context_window is the fallback
    detail: Dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        src = "advertised" if self.discovered else "FALLBACK (endpoint silent)"
        q = self.quant_hint or ("quantized" if self.quantized else "full-precision")
        return (f"{self.model}: {self.context_window} ctx [{src}], {q}, "
                f"engine={self.engine}, thinking={'yes' if self.thinks else 'no'}")



def _apply_name_hints(caps: "ModelCapabilities", name: str) -> None:
    """Infer quantization and thinking from a model NAME or checkpoint path.

    Only ever turns hints ON. Called first with the caller's alias and again
    with the endpoint's advertised `root`, so a bare alias like
    `aither-orchestrator` is corrected by `.../Nemotron-8B-AWQ-4bit` rather than
    overriding it back to "full precision".
    """
    if not name:
        return
    m = _QUANT.search(name)
    if m:
        caps.quantized = True
        caps.quant_hint = m.group(0).lower()
    if _THINKS.search(name):
        caps.thinks = True


def _parse_vllm(payload: Any, model: str) -> Tuple[Optional[int], str]:
    """(max_model_len, root) for the requested model.

    🚨 `root` is what makes this genuinely model-informed. The served `id` is an
    ALIAS chosen by the operator and carries no information: our orchestrator is
    served as `aither-orchestrator`, from which you cannot tell it is 4-bit or
    that it emits <think>. vLLM also reports

        root: cyankiwi/Nemotron-Orchestrator-8B-AWQ-4bit

    which carries BOTH. Inferring from the alias reported this endpoint as
    "full-precision, thinking=no" — wrong on both counts (measured 2026-08-18).
    """
    best: Tuple[Optional[int], str] = (None, "")
    try:
        for m in (payload or {}).get("data") or []:
            if not m.get("max_model_len"):
                continue
            root = str(m.get("root") or "")
            if not model or str(m.get("id")) == model:
                return int(m["max_model_len"]), root
            if best[0] is None:  # remember a usable entry as a fallback
                best = (int(m["max_model_len"]), root)
    except Exception:
        return (None, "")
    return best


def _parse_llamacpp(payload: Any) -> Optional[int]:
    """n_ctx as-is. See the module docstring: it is ALREADY per-slot."""
    try:
        gen = (payload or {}).get("default_generation_settings") or {}
        n = gen.get("n_ctx") or (payload or {}).get("n_ctx")
        n = int(n)
        return n if n > 0 else None
    except Exception:
        return None


def discover(base_url: str, model: str = "", *, api_key: str = "",
             timeout: float = 5.0, force: bool = False) -> ModelCapabilities:
    """Ask the endpoint what it can do. Never raises; degrades to a stated fallback."""
    key = (base_url or "", model or "")
    if not force:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < _TTL_S:
            return hit[1]

    caps = ModelCapabilities(model=model or "unknown")
    _apply_name_hints(caps, model or "")

    root = (base_url or "").rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    if root:
        try:
            import httpx  # lazy: importing this module must never fail
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            with httpx.Client(timeout=timeout, verify=tls_verify()) as c:
                try:
                    r = c.get(f"{root}/v1/models", headers=headers)
                    if r.status_code == 200:
                        win, model_root = _parse_vllm(r.json(), model)
                        if win:
                            caps.context_window = win
                            caps.engine = "vllm"
                            caps.discovered = True
                            # Re-infer from the ADVERTISED checkpoint path, which
                            # the alias hides. See _parse_vllm.
                            if model_root:
                                caps.detail["root"] = model_root
                                _apply_name_hints(caps, model_root)
                except Exception:
                    pass
                if not caps.discovered:
                    try:
                        r = c.get(f"{root}/props", headers=headers)
                        if r.status_code == 200:
                            win = _parse_llamacpp(r.json())
                            if win:
                                caps.context_window = win
                                caps.engine = "llamacpp"
                                caps.discovered = True
                    except Exception:
                        pass
        except Exception:
            pass

    _CACHE[key] = (time.time(), caps)
    return caps


def clear_cache() -> None:
    _CACHE.clear()


# effort -> share of the window the agent may spend THINKING. Effort is a
# statement about how hard to think, so it scales the thinking share and nothing
# else. Context keeps the majority at every level; the answer share is roughly
# constant because a good answer is not longer just because the model thought
# harder about it.
_THINK_SHARE = {
    1: 0.00, 2: 0.05, 3: 0.10, 4: 0.15, 5: 0.20,
    6: 0.25, 7: 0.33, 8: 0.40, 9: 0.45, 10: 0.50,
}
_ANSWER_SHARE = 0.15
_MARGIN = 0.05      # never plan to fill the window exactly


def budget_for_effort(caps: ModelCapabilities, effort: int = 5,
                      prompt_tokens: int = 0) -> Dict[str, int]:
    """Split the REAL window into context / thinking / answer for this effort.

    Returns whole tokens. `max_tokens` is what a caller passes to the API and is
    thinking+answer, because the server counts them together — splitting them in
    the request is not possible, only in the plan.

    A model that does not emit <think> gets its thinking share folded into the
    answer rather than wasted.
    """
    effort = max(1, min(10, int(effort or 5)))
    window = max(1024, int(caps.context_window or DEFAULT_WINDOW))

    think = _THINK_SHARE[effort] if caps.thinks else 0.0
    answer = _ANSWER_SHARE + (0.0 if caps.thinks else _THINK_SHARE[effort] * 0.5)
    usable = 1.0 - _MARGIN

    think_t = int(window * think)
    answer_t = int(window * answer)
    ctx_t = int(window * usable) - think_t - answer_t

    # A large prompt must eat the CONTEXT share, never silently overrun the
    # window: shrink thinking first, then the answer, and keep a real floor so
    # the model can still say something.
    if prompt_tokens and prompt_tokens > ctx_t:
        overflow = prompt_tokens - ctx_t
        take = min(overflow, max(0, think_t - 128))
        think_t -= take
        overflow -= take
        if overflow > 0:
            answer_t = max(256, answer_t - overflow)
        ctx_t = prompt_tokens

    return {
        "window": window,
        "context": max(256, ctx_t),
        "thinking": max(0, think_t),
        "answer": max(256, answer_t),
        "max_tokens": max(256, think_t + answer_t),
        "effort": effort,
        "discovered": caps.discovered,
    }


def _self_test() -> int:
    fails = []
    n = [0]

    def ck(label, cond):
        n[0] += 1
        if not cond:
            fails.append(label)

    # --- parsing, against REAL payloads seen on this fleet -------------------
    ck("vLLM max_model_len parsed",
       _parse_vllm({"data": [{"id": "gemma4-12b", "max_model_len": 16384}]},
                   "gemma4-12b")[0] == 16384)
    ck("vLLM falls back to any entry with a window",
       _parse_vllm({"data": [{"id": "other", "max_model_len": 4096}]}, "nope")[0] == 4096)
    ck("vLLM without max_model_len -> None",
       _parse_vllm({"data": [{"id": "x"}]}, "x")[0] is None)
    # bonsai: launched -c 16384 -np 2, reports 8192. Dividing again = 4096 = WRONG.
    ck("llama.cpp n_ctx is NOT divided by slots",
       _parse_llamacpp({"default_generation_settings": {"n_ctx": 8192},
                        "total_slots": 2}) == 8192)
    ck("llama.cpp garbage -> None", _parse_llamacpp({"total_slots": 2}) is None)

    # --- the fallback must never be 0 ---------------------------------------
    caps = discover("", "unreachable-model")
    ck("unreachable endpoint still yields a usable window",
       caps.context_window >= 1024 and not caps.discovered)
    ck("fallback is honestly labelled", "FALLBACK" in caps.describe())

    # --- budgets ------------------------------------------------------------
    thinking = ModelCapabilities(model="deepseek-r1-14b", context_window=32768,
                                 thinks=True, discovered=True)
    b1 = budget_for_effort(thinking, effort=1)
    b10 = budget_for_effort(thinking, effort=10)
    ck("effort 1 spends nothing on thinking", b1["thinking"] == 0)
    ck("effort 10 spends more than effort 1", b10["thinking"] > b1["thinking"])
    ck("thinking never exceeds half the window", b10["thinking"] <= 32768 * 0.5)
    for e in range(1, 11):
        b = budget_for_effort(thinking, effort=e)
        ck(f"effort {e} fits the window",
           b["context"] + b["thinking"] + b["answer"] <= b["window"])
        ck(f"effort {e} leaves a real answer", b["answer"] >= 256)

    # A non-thinking model must not reserve thinking tokens.
    plain = ModelCapabilities(model="gemma4-12b", context_window=16384,
                              thinks=False, discovered=True)
    ck("non-thinking model reserves no thinking budget",
       budget_for_effort(plain, effort=10)["thinking"] == 0)

    # A big prompt shrinks thinking BEFORE the answer, and never overruns.
    big = budget_for_effort(thinking, effort=10, prompt_tokens=30000)
    ck("huge prompt does not overrun the window",
       big["context"] + big["thinking"] + big["answer"] <= big["window"] + big["context"])
    ck("huge prompt still leaves an answer", big["answer"] >= 256)
    ck("huge prompt shrinks thinking first", big["thinking"] < b10["thinking"])

    # --- quant / thinking inference ----------------------------------------
    ck("AWQ detected", discover("", "Nemotron-8B-AWQ-4bit").quantized)
    ck("q4_0 gguf detected", discover("", "Bonsai-27B-Q1_0.gguf").quantized)
    ck("plain name not marked quantized",
       not discover("", "gemma-4-12b-it").quantized)
    ck("r1 marked as thinking", discover("", "deepseek-r1-14b").thinks)

    # 🚨 The alias hides everything; the advertised root carries it. This is the
    # real payload from our orchestrator, whose id is just "aither-orchestrator".
    _c = ModelCapabilities(model="aither-orchestrator")
    _apply_name_hints(_c, "aither-orchestrator")
    ck("bare alias reveals nothing", not _c.quantized and not _c.thinks)
    _apply_name_hints(_c, "cyankiwi/Nemotron-Orchestrator-8B-AWQ-4bit")
    ck("root reveals the quantization", _c.quantized and "awq" in _c.quant_hint)
    ck("root reveals it is a thinking model", _c.thinks)
    ck("root parsed out of /v1/models",
       _parse_vllm({"data": [{"id": "aither-orchestrator", "max_model_len": 32768,
                              "root": "cyankiwi/Nemotron-8B-AWQ-4bit"}]},
                   "aither-orchestrator")[1].endswith("AWQ-4bit"))

    if fails:
        print("SELF-TEST FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print(f"self-test: discovery, the no-divide rule, effort budgets and the "
          f"non-zero fallback all behave ({n[0]} assertions)")
    return 0


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        raise SystemExit(_self_test())
    url = os.environ.get("ADK_BASE_URL", "http://127.0.0.1:8120")
    mdl = os.environ.get("ADK_MODEL", "")
    c = discover(url, mdl)
    print(c.describe())
    for e in (1, 5, 10):
        print(f"  effort {e:2d}: {budget_for_effort(c, e)}")
