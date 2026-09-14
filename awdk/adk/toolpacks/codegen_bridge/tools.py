"""Codegen bridge — codegen_* agent tools.

ONE tool: `codegen_generate`. Invokes Qwen3.8-27B-IQ1_S for a single, self-contained
code-generation request, WITHOUT asking it to drive an agentic tool-calling loop —
that role stays with Bonsai (or whichever model called this tool), which is what this
session's measurements showed each model is actually good at:

  Bonsai   -- reliable native tool-calling, 5/5 on the agentic_tasks ruler, but its
              own PrismML card states agentic coding (long-horizon, multi-file,
              run-test-and-repair) is not yet a strength.
  Qwen3.8  -- real code-writing ability (fizzbuzz/is_palindrome/merge_intervals
              executed and scored correct, not just plausible-looking), but its own
              agentic tool-calling reliability lagged badly (0/5 text-protocol,
              2-3/5 native tools) even after fixing the launch config.

Design rules (same doctrine as llm_serving / node_bootstrap):
  * Fail soft -- codegen_generate never raises; every path returns a dict with a
    `status` field the caller MUST check.
  * The swap-load cost is real and disclosed (`elapsed_s`), not hidden.
  * The caller's own backend (default: Bonsai) is ALWAYS restored, even on failure --
    wrapped in try/finally, because a live-proof caller that stops responding after
    calling a "sub-tool" is worse than a failed generation.
  * The launch flags are the ones this session measured, not guesses:
    `--reasoning-format deepseek` + a `--reasoning-budget-message` (the
    content/reasoning-leak bug was a real repetition-loop degeneracy at IQ1_S,
    root-caused via transcript -- see .PLANS/bonsai-27b-awdk-coder-2026-08-22.md
    phase 10) and a MODEST `--repeat-penalty` (phase 11: higher values fix tool-call
    stalls but corrupt exact character-level output -- this stays at the measured
    sweet spot, not pushed higher).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger("codegen_bridge_pack")

# ── memoization via awm (real, local, no server dependency) ──────────────
# Every codegen_generate call costs a real 60-200s GPU swap-load on shared
# hardware -- an identical repeated prompt paying that twice is pure waste.
# awm (AitherOS/packages/awm, "a portable, scoped agent memory") is a plain
# SQLite-backed local library with no server to reach, unlike awreason (which
# needs a remote reasoning-shaped service -- none is running in this fleet,
# checked live 2026-08-24, so that integration is NOT built here; faking a
# local-only substitute that never talks to anything would be the "gate that
# always passes" trap this codebase's own doctrine warns against repeatedly).
# Import is guarded and failure is silent -- a missing/broken awm degrades the
# tool to "always regenerate", never breaks it (same fail-soft contract as the
# rest of this file).
try:
    import awm as _awm
except Exception:  # noqa: BLE001 — optional dependency, never fatal
    _awm = None

_MEMORY_DB_PATH = Path.home() / ".aitheros" / "codegen_bridge_memory.db"
_MEMORY_SCOPE_TENANT = "codegen-bridge"
_memory_store = None  # lazy singleton, built on first real use


def _get_memory_store():
    """Return a cached awm.MemoryStore, or None if awm is unavailable/broken.

    Never raises -- a corrupt or unwritable DB degrades to "no cache", not a
    tool failure.
    """
    global _memory_store
    if _awm is None:
        return None
    if _memory_store is not None:
        return _memory_store
    try:
        _MEMORY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _memory_store = _awm.MemoryStore(_MEMORY_DB_PATH)
        return _memory_store
    except Exception as exc:  # noqa: BLE001 — degrade, don't fail the tool
        logger.debug("codegen_bridge: memory store unavailable: %s", exc)
        return None


def _cache_key(prompt: str, max_tokens: int, repeat_penalty: float) -> str:
    """Stable key for one (prompt, params) combination — same inputs, same
    key, so an exact repeat is a cache hit and any parameter change is a
    genuine cache miss (never serves a result generated under different
    settings)."""
    raw = f"{prompt}|{max_tokens}|{repeat_penalty}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# ── failure diagnosis via awprism (real, local, no server dependency) ────
# awprism ("turn a failure into ranked hypotheses -- and say what would
# confirm each one") pairs directly with this file: every strategy below is
# grounded in an ACTUAL incident this session root-caused via a live
# transcript, not a generic placeholder. A caller that gets a bare error
# string re-derives the same investigation from scratch; one that gets ranked,
# falsifiable hypotheses can check the cheapest one first.
try:
    import awprism as _awprism
except Exception:  # noqa: BLE001 — optional dependency, never fatal
    _awprism = None

_prism_registry = None  # lazy singleton


# ── persistent REPL via awrepl (real, local, no server dependency) ───────
# "A REPL an agent can actually use — state that survives between turns."
# Bonsai's built-in tools are one-shot subprocess calls; a REPL session lets
# it INSPECT state across multiple tool calls in the same run (check whether
# codegen_generate's output actually parses, run a quick calculation, hold a
# variable between steps) instead of guessing or re-deriving it in prose.
try:
    import awrepl as _awrepl
except Exception:  # noqa: BLE001 — optional dependency, never fatal
    _awrepl = None

_repl_pool = None  # lazy singleton


def _get_repl_pool():
    """Return a cached awrepl.SessionPool, or None if awrepl is unavailable.
    Never raises."""
    global _repl_pool
    if _awrepl is None:
        return None
    if _repl_pool is not None:
        return _repl_pool
    try:
        _repl_pool = _awrepl.SessionPool()
        return _repl_pool
    except Exception as exc:  # noqa: BLE001 — degrade, don't fail the tool
        logger.debug("codegen_bridge: repl pool unavailable: %s", exc)
        return None


def repl_exec(code: str, session_id: str = "bonsai-coder-default") -> dict:
    """Execute Python in a PERSISTENT session — variables and imports survive
    across separate calls with the same session_id, unlike the one-shot
    file/shell tools. Use this to actually CHECK a value (parse output, run
    a calculation, hold intermediate state across steps) instead of guessing
    what it would be.

    Returns a dict, never raises:
      status: "ok" | "unavailable" | "error"
      stdout / stderr: captured output
      value: repr of the last expression's value, if any
      exception: the exception message, if the code raised
      truncated: True if output was cut off at the session's byte limit
      error: populated on any non-"ok" status
    """
    result = {"status": "error", "stdout": "", "stderr": "", "value": None,
              "exception": None, "truncated": False, "error": ""}
    pool = _get_repl_pool()
    if pool is None:
        result["status"] = "unavailable"
        result["error"] = "awrepl is not installed or failed to initialize"
        return result
    try:
        try:
            sess = pool.get_session(session_id)
        except Exception:
            sess = None
        if sess is None:
            created_id = pool.create_session(session_id)
            sess = pool.get_session(created_id)
        exec_result = sess.execute(code)
        result.update({
            "status": "ok",
            "stdout": exec_result.stdout, "stderr": exec_result.stderr,
            "value": exec_result.value, "exception": exec_result.exception,
            "truncated": exec_result.truncated,
        })
        return result
    except Exception as exc:  # noqa: BLE001 — fail soft, never raise
        result["status"] = "error"
        result["error"] = f"repl execution failed: {exc}"
        return result


def repl_reset(session_id: str = "bonsai-coder-default") -> dict:
    """Reset a REPL session — clears all variables/imports for a fresh start.
    Returns {status, error}, never raises."""
    result = {"status": "error", "error": ""}
    pool = _get_repl_pool()
    if pool is None:
        result["status"] = "unavailable"
        result["error"] = "awrepl is not installed or failed to initialize"
        return result
    try:
        sess = pool.get_session(session_id)
        if sess is not None:
            sess.reset()
        result["status"] = "ok"
        return result
    except Exception as exc:  # noqa: BLE001 — fail soft, never raise
        result["status"] = "error"
        result["error"] = f"repl reset failed: {exc}"
        return result


# ── recursive long-context query via awrecurse (real, no dedicated server) ─
# "Answer a question over a context far larger than the window — recursively,
# with the trace kept." awrecurse's registry entry: "speaks_to: any
# completion-shaped endpoint" — unlike awreason it does NOT need a
# reasoning-shaped microservice, just a plain complete_fn callable. Wired
# directly to MicroScheduler (the mandated LLM routing layer — CLAUDE.md:
# "All LLM calls route through MicroScheduler, never bypass it"), reusing the
# aither-orchestrator model since this is a text-chunking/synthesis task, not
# a code-generation one — codegen_generate stays the only caller of Bonsai's
# swap-load path.
try:
    import awrecurse as _awrecurse
except Exception:  # noqa: BLE001 — optional dependency, never fatal
    _awrecurse = None

_MICROSCHEDULER_URL = "https://127.0.0.1:8150/v1/chat/completions"
_RECURSE_MODEL = "aither-orchestrator"
_RECURSE_TIMEOUT_S = 60


def _microscheduler_ssl_context():
    """Build the SSL context from `adk._tls.tls_verify()` — the SAME
    resolver `adk.llm.openai_compat`'s vllm/genesis/llamacpp providers
    already use (see `switch_backend("vllm", ...)` in adk/llm/__init__.py).

    Found live 2026-08-24 while adding this: a hand-rolled version had a
    HARDCODED CA path (`C:/AitherOS-Data/Library/Data/tls/ca-chain.pem`,
    dated 2026-03-16) that diverged from what `tls_verify()` actually
    resolves on this host (`~/.aither/aithernet-ca-bundle.pem`, dated
    2026-08-06 — newer, and the one every other adk LLM call already trusts).
    Both files happen to exist here, so the divergence was silent; on a host
    where only the canonical one exists, the hand-rolled version would have
    fallen through to the system trust store, which does NOT contain the
    internal CA and would have failed every call. Reuse the tested resolver
    instead of a second, drifting copy of the same decision.
    """
    import ssl

    from adk._tls import tls_verify
    verify = tls_verify()
    if verify is False:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if isinstance(verify, str):
        return ssl.create_default_context(cafile=verify)
    return ssl.create_default_context()


def _microscheduler_complete(prompt: str) -> str:
    """A plain str->str completion function over MicroScheduler, the shape
    awrecurse.RecursionEngine's complete_fn contract needs. Trusts the
    internal CA via the canonical `adk._tls.tls_verify()` resolver — never a
    second hand-rolled copy of that decision (see `_microscheduler_ssl_context`).
    """
    body = json.dumps({
        "model": _RECURSE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1024,
    }).encode("utf-8")
    req = urllib.request.Request(
        _MICROSCHEDULER_URL, data=body,
        headers={"Content-Type": "application/json"})
    ctx = _microscheduler_ssl_context()
    with urllib.request.urlopen(req, timeout=_RECURSE_TIMEOUT_S, context=ctx) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"] or ""


def recurse_query(context: str, query: str, chunk_size: int = 2000,
                   max_iterations: int = 10) -> dict:
    """Answer a question over a context far larger than one model turn can
    hold, recursively — chunking, querying each slice, and synthesizing —
    with a trace of which slices were actually read. Use this instead of
    pasting a huge document into a normal prompt and hoping the middle wasn't
    silently dropped (a context overflow does not raise; it answers fluently
    from the ends).

    Returns a dict, never raises:
      status: "ok" | "unavailable" | "error"
      final_answer, slices_read, iterations, tokens: from the underlying RLM
      error: populated on any non-"ok" status
    """
    result = {"status": "error", "final_answer": "", "slices_read": [],
              "iterations": 0, "tokens": 0, "error": ""}
    if _awrecurse is None:
        result["status"] = "unavailable"
        result["error"] = "awrecurse is not installed"
        return result
    try:
        engine = _awrecurse.RecursionEngine(
            complete_fn=_microscheduler_complete,
            chunk_size=chunk_size, max_iterations=max_iterations)
        rlm_result = engine.recurse(context, query)
        result.update({
            "status": "ok" if rlm_result.success else "error",
            "final_answer": rlm_result.final_answer,
            "slices_read": rlm_result.slices_read or [],
            "iterations": rlm_result.iterations,
            "tokens": rlm_result.tokens,
            "error": rlm_result.error or "",
        })
        return result
    except Exception as exc:  # noqa: BLE001 — fail soft, never raise
        result["status"] = "error"
        result["error"] = f"recursion failed: {exc}"
        return result


# ── structured reasoning trace, LOCAL (awreason taxonomy, no server) ─────
# Real AitherReasoning exists (lib/faculties/AitherReasoningBridge.py,
# lib/clients/reasoning.py, port 8093) but its standalone service is
# MASKED fleet-wide (`systemctl status aither-reasoning` -> "Loaded: masked")
# — the compose definition explains why: a ~10GB local reasoning model
# co-resident with the orchestrator on a single 5090 starves the
# orchestrator's KV cache and disconnects live chats. Unmasking it to build
# this would risk recreating that exact documented outage on a shared,
# already GPU-contended fleet (this session's own codegen_bridge swap-load
# is a second consumer of the same GPU) — a real production risk, not a
# hypothetical one, so it stays masked and this does NOT unmask it.
#
# What's built instead is genuinely useful and carries none of that risk:
# codegen_generate's own SITUATION/ANALYSIS/SYNTHESIS/EXECUTION/COMPLETE
# phases (the real SASE taxonomy from awreason.SASEPhase, not invented) are
# recorded locally via the SAME awm cache already wired above, and a new
# tool below reads them back as a trace — the same "phases + tool calls
# instead of one paragraph" value awreason's own tagline promises, with zero
# new server dependency.
try:
    import awreason as _awreason
except Exception:  # noqa: BLE001 — optional dependency, never fatal
    _awreason = None

_REASONING_TRACE_SCOPE_TENANT = "codegen-bridge-reasoning"


def _record_reasoning_phase(trace_id: str, phase, note: str) -> None:
    """Append one SASE-phase entry to a trace, stored via awm. Best-effort —
    a tracing failure must never affect the actual generation it describes."""
    if _awreason is None or _awm is None:
        return
    try:
        store = _get_memory_store()
        if store is None:
            return
        scope = _awm.Scope(tenant=_REASONING_TRACE_SCOPE_TENANT)
        phase_value = phase.value if hasattr(phase, "value") else str(phase)
        existing = store.recall(scope, query=trace_id, kind="reasoning_trace", limit=1)
        entries = json.loads(existing[0].value) if existing else []
        entries.append({"phase": phase_value, "note": note, "ts": time.time()})
        store.remember(scope, trace_id, json.dumps(entries), kind="reasoning_trace")
    except Exception as exc:  # noqa: BLE001 — tracing must never break generation
        logger.debug("codegen_bridge: reasoning trace write failed: %s", exc)


def codegen_reasoning_trace(trace_id: str) -> dict:
    """Read back the SASE-phase trace (situation/analysis/synthesis/
    execution/complete) codegen_generate recorded for one call, so a caller
    can inspect WHY a result came out the way it did instead of trusting a
    single paragraph. `trace_id` is the cache_key codegen_generate computed
    for that call (visible in its own internals; primarily useful when
    chained with codegen_diagnose_failure for a failed call).

    Returns a dict, never raises:
      status: "ok" | "not_found" | "unavailable"
      phases: [{phase, note, ts}, ...] in the order they were recorded
      error: populated on any non-"ok" status
    """
    result = {"status": "error", "phases": [], "error": ""}
    if _awreason is None or _awm is None:
        result["status"] = "unavailable"
        result["error"] = "awreason and/or awm is not installed"
        return result
    store = _get_memory_store()
    if store is None:
        result["status"] = "unavailable"
        result["error"] = "memory store unavailable"
        return result
    try:
        scope = _awm.Scope(tenant=_REASONING_TRACE_SCOPE_TENANT)
        hits = store.recall(scope, query=trace_id, kind="reasoning_trace", limit=1)
        exact = [m for m in hits if m.key == trace_id]
        if not exact:
            result["status"] = "not_found"
            result["error"] = f"no reasoning trace recorded for {trace_id!r}"
            return result
        result["status"] = "ok"
        result["phases"] = json.loads(exact[0].value)
        return result
    except Exception as exc:  # noqa: BLE001 — fail soft, never raise
        result["status"] = "error"
        result["error"] = f"trace read failed: {exc}"
        return result


def _codegen_diagnostic_strategies() -> list:
    """One DiagnosticStrategy per failure `status` this tool can return, each
    built from a real incident, not a guess:

      load_timeout            -- Qwen3.8 never answered /health within 120s.
      caller_restore_failed   -- live-measured 2026-08-24: Bonsai actually came
                                  back healthy, just past the (then 60s, now
                                  120s) verify window -- a false-negative, not
                                  necessarily a real outage, so that is ranked
                                  ABOVE "Bonsai crashed" rather than below it.
      generation_failed       -- covers two real, DIFFERENT causes found this
                                  session: a transport-level curl failure, and
                                  a valid response with trailing data after it
                                  (fixed via raw_decode, but the underlying WSL
                                  stdout+stderr noise source was never fully
                                  root-caused -- see the comment at the parse
                                  site -- so it can still recur in a form this
                                  fix tolerates but a caller should still know
                                  to check for).
      no_code_extracted       -- the model answered without a fenced code
                                  block in either content or reasoning_content.
    """
    hyp = _awprism.Hypothesis
    strat = _awprism.DiagnosticStrategy

    def _load_timeout_hyps(symptom: str, context: str) -> list:
        return [
            hyp(claim="Shared-fleet GPU contention delayed the swap-load past "
                      "the timeout budget", score=0.6,
                falsifier="check `podman ps` / `nvidia-smi` on the fleet box "
                          "for other GPU-resident containers at the time of "
                          "this call",
                rationale="this fleet runs concurrent sessions sharing one "
                          "5090 -- measured this session under real "
                          "contention (agentic_tasks ruler ran 10-12x slower "
                          "than baseline with the GPU at 95% util)"),
            hyp(claim="The Qwen3.8-27B-IQ1_S GGUF is not present at the "
                      "expected library mount path", score=0.25,
                falsifier="check that _QWEN_LIBRARY_MOUNT resolves inside "
                          "the WSL distro and the .gguf file exists there"),
            hyp(claim="A stale qwen38-codegen-bridge container from a prior "
                      "crashed run is holding the port", score=0.15,
                falsifier="`podman ps -a --filter name=qwen38-codegen-bridge` "
                          "-- codegen_generate's own launch_cmd already runs "
                          "`podman rm -f` first, so this should self-heal, "
                          "but a a stuck (not merely stopped) container can "
                          "still hold the port past that"),
        ]

    def _caller_restore_hyps(symptom: str, context: str) -> list:
        return [
            hyp(claim="Bonsai actually came back healthy -- this is a "
                      "restore-VERIFY timing false-negative, not a real "
                      "outage", score=0.55,
                falsifier="probe aither-llamacpp-bonsai's health in-network "
                          "right now (podman exec any peer container: "
                          "curl http://aither-llamacpp-bonsai:8090/health) "
                          "-- live-measured 2026-08-24: this was exactly "
                          "what happened, Bonsai was healthy 2 minutes after "
                          "the tool reported this status",
                evidence_for=["_RESTORE_TIMEOUT_S was 60s when this class "
                               "was first found; a cold 27B Q1_0 reload can "
                               "genuinely take longer than that"]),
            hyp(claim="Bonsai's systemctl unit genuinely failed to restart "
                      "(crash-looping, OOM, GPU held by another process)",
                score=0.3,
                falsifier="`systemctl status aither-llamacpp-bonsai` and "
                          "`journalctl -u aither-llamacpp-bonsai -n 50` -- if "
                          "it shows failed/activating (not active/running), "
                          "this is the real cause, not the timing one above"),
            hyp(claim="The restore-verify probe itself is broken (wrong "
                      "in-network hostname, MicroScheduler down), not the "
                      "caller", score=0.15,
                falsifier="the probe goes through aitheros-microscheduler -- "
                          "check that container's own health independently"),
        ]

    def _generation_failed_hyps(symptom: str, context: str) -> list:
        return [
            hyp(claim="A complete, valid response arrived with unrelated "
                      "trailing text after it (curl diagnostics or a stray "
                      "log line riding the WSL stdout+stderr hop)", score=0.4,
                falsifier="check whether the error text contains a COMPLETE "
                          "JSON object ending in `}` before wherever the "
                          "parse actually failed -- live-measured 2026-08-24 "
                          "at char 12962 of a real, correct response; fixed "
                          "via raw_decode(), but the noise SOURCE itself was "
                          "never root-caused, so it can still occur"),
            hyp(claim="The generation call genuinely failed at the transport "
                      "level (curl error, connection reset, timeout)",
                score=0.35,
                falsifier="the error field's prefix distinguishes these -- "
                          "'generation call failed:' is a transport failure, "
                          "'could not parse generation response:' is the "
                          "trailing-data class above"),
            hyp(claim="Qwen3.8 itself crashed or hung mid-generation (OOM at "
                      "a 32768 context window on a shared GPU)", score=0.25,
                falsifier="check the qwen38-codegen-bridge container's own "
                          "logs (`podman logs qwen38-codegen-bridge`) for a "
                          "CUDA OOM or a segfault around the call's timestamp"),
        ]

    def _no_code_hyps(symptom: str, context: str) -> list:
        return [
            hyp(claim="The model answered with code but no fenced ``` block "
                      "-- present as plain text in content or "
                      "reasoning_content", score=0.5,
                falsifier="read result['raw_content'] and "
                          "result['reasoning_excerpt'] directly for def/class "
                          "keywords outside any fence"),
            hyp(claim="The prompt was ambiguous or conversational rather "
                      "than a concrete code-generation request", score=0.3,
                falsifier="re-read the prompt against this tool's own "
                          "contract: 'a single, self-contained code-"
                          "generation request', not a question or a "
                          "multi-step task"),
            hyp(claim="max_tokens was too small for the model's reasoning "
                      "trace to complete before emitting the code",
                score=0.2,
                falsifier="live-measured 2026-08-24 in the SAME class for "
                          "Bonsai's own tool-calling: a 400-token budget "
                          "burned entirely on reasoning with finish_reason "
                          "'length' and never reached the payload -- check "
                          "whether this response also has finish_reason "
                          "'length' rather than 'stop'"),
        ]

    return [
        strat(name="codegen_load_timeout",
              description="Qwen3.8 never became ready within _LOAD_TIMEOUT_S",
              pattern_check=lambda s: "load_timeout" in s or "did not become ready" in s,
              generate_hypotheses=_load_timeout_hyps),
        strat(name="codegen_caller_restore_failed",
              description="the quiesced caller (Bonsai) did not verify as "
                          "restored",
              pattern_check=lambda s: "caller_restore_failed" in s or "restart" in s,
              generate_hypotheses=_caller_restore_hyps),
        strat(name="codegen_generation_failed",
              description="the generation HTTP call failed or its response "
                          "would not parse",
              pattern_check=lambda s: "generation_failed" in s or "could not parse" in s,
              generate_hypotheses=_generation_failed_hyps),
        strat(name="codegen_no_code_extracted",
              description="a response arrived but no fenced code block was "
                          "found in it",
              pattern_check=lambda s: "no_code_extracted" in s or "no fenced code" in s,
              generate_hypotheses=_no_code_hyps),
    ]


def _get_prism():
    """Return a cached awprism.Prism seeded with codegen_bridge's own
    strategies, or None if awprism is unavailable. Never raises."""
    global _prism_registry
    if _awprism is None:
        return None
    if _prism_registry is not None:
        return _prism_registry
    try:
        registry = _awprism.StrategyRegistry()
        for strat in _codegen_diagnostic_strategies():
            registry.register(strat)
        _prism_registry = _awprism.Prism(registry=registry)
        return _prism_registry
    except Exception as exc:  # noqa: BLE001 — degrade, don't fail the tool
        logger.debug("codegen_bridge: prism unavailable: %s", exc)
        return None


def codegen_diagnose_failure(status: str, error: str) -> dict:
    """Turn a non-'ok' codegen_generate result into ranked, falsifiable
    hypotheses instead of a bare error string.

    Call this with the `status` and `error` fields from a codegen_generate
    result that was NOT "ok". Every hypothesis carries a falsifier -- the one
    cheap check that would confirm or rule it out -- built from real
    incidents this session root-caused, not generic guesses.

    Returns a dict, never raises:
      status: "ok" | "no_strategy_matched" | "unavailable"
      hypotheses: [{claim, score, falsifier, rationale}, ...], ranked highest
                  score first, or [] if status != "ok"
      error: populated on any non-"ok" status
    """
    result = {"status": "error", "hypotheses": [], "error": ""}
    if not status or status == "ok":
        result["error"] = "nothing to diagnose -- status was 'ok' or empty"
        return result

    prism = _get_prism()
    if prism is None:
        result["status"] = "unavailable"
        result["error"] = "awprism is not installed or failed to initialize"
        return result

    try:
        symptom = f"codegen_generate returned status={status}: {error}"
        diagnosis = prism.diagnose(symptom, context="codegen_bridge toolpack", k=5)
        if not diagnosis.hypotheses:
            result["status"] = "no_strategy_matched"
            result["error"] = (
                f"no diagnostic strategy recognized status {status!r} -- "
                "this may be a NEW failure class not yet root-caused")
            return result
        result["status"] = "ok"
        result["hypotheses"] = [
            {"claim": h.claim, "score": h.score, "falsifier": h.falsifier,
             "rationale": h.rationale}
            for h in sorted(diagnosis.hypotheses, key=lambda h: -h.score)
        ]
        return result
    except Exception as exc:  # noqa: BLE001 — fail soft, never raise
        result["status"] = "error"
        result["error"] = f"diagnosis failed: {exc}"
        return result


# ── proven configuration, from this session's live measurements ──────────
# .PLANS/bonsai-27b-awdk-coder-2026-08-22.md phases 4c/10/11.

_QWEN_MODEL_PATH = "/models/Qwen3.8-27B-UD-IQ1_S.gguf"
_QWEN_LIBRARY_MOUNT = "/mnt/c/AitherOS-Data/Library/qwen38-27b:/models:ro"
_QWEN_ALIAS = "qwen38-27b-iq1s"
_QWEN_PORT = 8092
_QWEN_CONTAINER = "qwen38-codegen-bridge"
_QWEN_IMAGE = "ghcr.io/ggml-org/llama.cpp:server-cuda"

# Default caller backend to quiesce/restore. Only Bonsai is proven — this is a
# parameter, not a hardcoded assumption, but changing it needs the same "does the
# caller's server actually come back" verification this file does for Bonsai.
_DEFAULT_CALLER_UNIT = "aither-llamacpp-bonsai"
_DEFAULT_CALLER_HEALTH_URL = "http://localhost:8090/health"
# In-network name MicroScheduler/other containers use to reach the caller — used
# for the restore-verification round trip, since the caller's port is not
# published to the WSL host (measured this session: connection refused from the
# host even when the container is healthy).
_DEFAULT_CALLER_INNETWORK = "aither-llamacpp-bonsai:8090"

_LOAD_POLL_INTERVAL_S = 5
_LOAD_TIMEOUT_S = 120
_TEARDOWN_TIMEOUT_S = 20
# Was 60 -- live-measured 2026-08-24: aither-llamacpp-bonsai (27B Q1_0, -ngl 99)
# came back and served correctly, but past the 60s window, so this reported
# "caller_restore_failed" on a caller that was actually fine -- a false-negative
# outage report. A cold reload of a 27B GGUF is the same class of cost as
# Qwen3.8's own load (_LOAD_TIMEOUT_S), so match it instead of guessing shorter.
_RESTORE_TIMEOUT_S = 120

_CODE_BLOCK = re.compile(r"```(?:python|py|\w*)?\s*\n(.*?)```", re.S)


def _wsl(cmd: str, timeout: int = 60) -> tuple[int, str]:
    """Run a command inside the fleet's Debian WSL2 distro as root.

    Fail-soft: never raises. Returns (returncode, combined_output). This is the
    ONLY way this box reaches podman/systemctl for the fleet — see
    `.claude/rules/aitheros-dispatch.md` and CLAUDE.md's podman ground truth.
    """
    try:
        p = subprocess.run(
            ["wsl", "-d", "Debian", "-u", "root", "bash", "-c", cmd],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
        # NOT truncated: the generation call's output must parse as complete JSON.
        # A max_tokens=4096 response can exceed a few KB; truncating from the end
        # (the `[-N:]` pattern used elsewhere in this codebase for debug logging)
        # would slice INTO the JSON body and corrupt it — caught live: it produced
        # "Expecting value: line 1 column 1" because the truncated string no
        # longer started with the opening brace.
        out = ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
        return p.returncode, out
    except FileNotFoundError:
        return 127, "wsl.exe not found on PATH — this tool requires the Windows host"
    except subprocess.TimeoutExpired:
        return 124, f"wsl command timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001 — fail soft, never raise
        return 1, f"unexpected error invoking wsl: {exc}"


def _http_json(url: str, body: dict | None, timeout: float) -> tuple[bool, dict | str]:
    """POST (body given) or GET (body None). Returns (ok, parsed_or_error_str)."""
    try:
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"})
        else:
            req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        return False, str(exc)
    except (TimeoutError, ConnectionError, OSError) as exc:
        return False, str(exc)
    except json.JSONDecodeError as exc:
        return False, f"non-JSON response: {exc}"


def _extract_code(content: str, reasoning: str) -> tuple[str, str]:
    """Prefer a fenced code block in `content`; fall back to one in `reasoning`
    (the model sometimes derives the right answer there and never repeats it
    cleanly in content — measured this session, phase 10). Returns (code, source).
    """
    m = _CODE_BLOCK.search(content or "")
    if m:
        return m.group(1).strip(), "content"
    m = _CODE_BLOCK.search(reasoning or "")
    if m:
        return m.group(1).strip(), "reasoning_content(fallback)"
    return "", "none"


def codegen_generate(
    prompt: str,
    max_tokens: int = 4096,
    repeat_penalty: float = 1.08,
    caller_unit: str = _DEFAULT_CALLER_UNIT,
    caller_health_url: str = _DEFAULT_CALLER_HEALTH_URL,
    skip_caller_swap: bool = False,
    use_cache: bool = True,
) -> dict:
    """Generate ONE piece of code via Qwen3.8-27B-IQ1_S, swap-loaded on demand.

    `prompt` should be a single, self-contained code-generation request — "write a
    Python function that does X" — not a conversation and not a multi-step task.
    This is a ONE-SHOT sub-tool: it does not manage files, does not call other
    tools, and does not retry against test output. That loop stays with the
    caller (Bonsai).

    Returns a dict, never raises:
      status: "ok" | "no_code_extracted" | "load_timeout" | "generation_failed"
              | "caller_restore_failed" | "error"
      code: the extracted code, or "" if status != "ok"
      raw_content / reasoning_excerpt: for diagnosis when extraction fails
      elapsed_s: real wall time, so the caller can see what this actually cost
      error: populated on any non-"ok" status
      from_cache: True if this came from a prior identical call, skipping the
                  swap-load entirely (see `use_cache`)

    `skip_caller_swap=True` skips quiescing/restoring `caller_unit` — only correct
    if the caller already knows Qwen3.8 has room (e.g. a second GPU), which is not
    true on this fleet's single 5090 today. Default False is the safe assumption.

    `use_cache=True` (default): an exact repeat of (prompt, max_tokens,
    repeat_penalty) that previously returned status "ok" is served from a local
    cache (via awm, scoped tenant="codegen-bridge") WITHOUT touching Bonsai or
    Qwen3.8 at all — no quiesce, no swap-load, no restore. Every call is this
    expensive on shared hardware (60-200s measured live), so paying it twice for
    an identical request is pure waste. Pass False to force a fresh generation.
    """
    t0 = time.time()
    caller_was_quiesced = False
    result = {
        "status": "error", "code": "", "raw_content": "", "reasoning_excerpt": "",
        "elapsed_s": 0.0, "error": "", "from_cache": False,
    }

    cache_key = _cache_key(prompt, max_tokens, repeat_penalty)
    _phase = _awreason.SASEPhase if _awreason is not None else None
    _record_reasoning_phase(
        cache_key, _phase.SITUATION if _phase else "situation",
        f"codegen_generate called, prompt={prompt[:80]!r}")
    store = _get_memory_store() if use_cache else None
    if store is not None:
        try:
            scope = _awm.Scope(tenant=_MEMORY_SCOPE_TENANT)
            hits = store.recall(scope, query=cache_key, kind="codegen_result", limit=5)
            # recall() does substring/relevance matching, not exact lookup, so
            # confirm the key really matches before trusting a hit — a loose
            # match here would silently serve the WRONG generated code.
            exact = [m for m in hits if m.key == cache_key]
            if exact:
                _record_reasoning_phase(
                    cache_key, _phase.COMPLETE if _phase else "complete",
                    "cache HIT — served without touching the caller or GPU")
                cached = json.loads(exact[0].value)
                cached["from_cache"] = True
                cached["elapsed_s"] = round(time.time() - t0, 2)
                return cached
        except Exception as exc:  # noqa: BLE001 — cache read failure = miss
            logger.debug("codegen_bridge: cache lookup failed (%s), regenerating", exc)

    _record_reasoning_phase(
        cache_key, _phase.ANALYSIS if _phase else "analysis",
        "cache miss — a real swap-load generation is required")

    try:
        if not skip_caller_swap:
            rc, out = _wsl(f"systemctl stop {caller_unit}")
            if rc != 0:
                result["error"] = f"could not quiesce {caller_unit}: {out}"
                result["status"] = "error"
                return result
            caller_was_quiesced = True

        _record_reasoning_phase(
            cache_key, _phase.SYNTHESIS if _phase else "synthesis",
            f"caller quiesced (skip_caller_swap={skip_caller_swap}); "
            f"launching {_QWEN_ALIAS} swap-load")

        # Launch Qwen3.8 with the proven flags. --rm: no orphaned container on a
        # crash. Container name is fixed (not per-call unique) so a stuck prior
        # run is visible/cleanable rather than silently multiplying.
        launch_cmd = (
            f"podman rm -f {_QWEN_CONTAINER} 2>/dev/null; "
            f"podman run -d --rm --name {_QWEN_CONTAINER} "
            f"--device nvidia.com/gpu=all "
            f"-e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=compute,utility "
            f"--network aither-network -p {_QWEN_PORT}:{_QWEN_PORT} "
            f"-v {_QWEN_LIBRARY_MOUNT} {_QWEN_IMAGE} "
            f"-m {_QWEN_MODEL_PATH} --alias {_QWEN_ALIAS} "
            f"--host 0.0.0.0 --port {_QWEN_PORT} -ngl 99 -c 32768 -np 1 "
            f"--no-webui -fa on -ctk q4_0 -ctv q4_0 "
            f"--reasoning-budget 2048 --reasoning-format deepseek "
            f"--reasoning-budget-message "
            f"\"I have thought enough. Time to write the final answer now.\" "
            f"--jinja --reasoning-preserve"
        )
        rc, out = _wsl(launch_cmd, timeout=30)
        if rc != 0:
            result["error"] = f"failed to launch {_QWEN_CONTAINER}: {out}"
            result["status"] = "error"
            return result

        # Poll for readiness.
        deadline = time.time() + _LOAD_TIMEOUT_S
        ready = False
        while time.time() < deadline:
            rc, out = _wsl(
                f"curl -s http://localhost:{_QWEN_PORT}/health", timeout=10)
            if rc == 0 and '"status":"ok"' in out:
                ready = True
                break
            time.sleep(_LOAD_POLL_INTERVAL_S)
        if not ready:
            result["error"] = (
                f"{_QWEN_ALIAS} did not become ready within {_LOAD_TIMEOUT_S}s")
            result["status"] = "load_timeout"
            return result

        _record_reasoning_phase(
            cache_key, _phase.EXECUTION if _phase else "execution",
            f"{_QWEN_ALIAS} ready — sending the real generation request")

        # The actual generation call, from the WSL host (Qwen3.8's container DOES
        # publish its port, unlike Bonsai's — measured this session).
        payload = {
            "model": _QWEN_ALIAS,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "repeat_penalty": repeat_penalty,
            "repeat_last_n": 256,
        }
        gen_url = f"http://localhost:{_QWEN_PORT}/v1/chat/completions"
        gen_cmd = (
            f"curl -s --max-time 150 {gen_url} "
            f"-H 'Content-Type: application/json' "
            f"-d {json.dumps(json.dumps(payload))}"
        )
        rc, out = _wsl(gen_cmd, timeout=170)
        if rc != 0:
            result["error"] = f"generation call failed: {out}"
            result["status"] = "generation_failed"
            return result

        try:
            # NOT json.loads(out) -- live-measured 2026-08-24: a real generation
            # response failed with "Extra data: line 2 column 1 (char 12962)".
            # The first 12962 chars WERE a complete, valid, correct response
            # (verified by reading the raw text: a real is_palindrome solution);
            # something (curl diagnostics, a stray container-log line riding
            # along stdout+stderr through the WSL hop) trailed after it on a
            # second line. json.loads demands the ENTIRE string be one value and
            # rejects trailing bytes outright, so a perfectly good response was
            # thrown away over noise after it. raw_decode() takes the first
            # complete JSON value from the front and ignores what follows --
            # the correct contract for "parse the response", not "reject unless
            # nothing rides along after it". Leftover text is logged, not hidden.
            decoder = json.JSONDecoder()
            data, end = decoder.raw_decode(out)
            leftover = out[end:].strip()
            if leftover:
                logger.debug(
                    "codegen_generate: %d bytes of trailing data after a valid "
                    "JSON response, ignored: %r", len(leftover), leftover[:200])
            msg = data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError) as exc:
            result["error"] = f"could not parse generation response: {exc}: {out[:500]}"
            result["status"] = "generation_failed"
            return result

        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or ""
        code, source = _extract_code(content, reasoning)
        result["raw_content"] = content[:2000]
        result["reasoning_excerpt"] = reasoning[-1000:]

        if not code:
            result["status"] = "no_code_extracted"
            result["error"] = "no fenced code block in content or reasoning_content"
            return result

        result["code"] = code
        result["status"] = "ok"
        result["source"] = source

        _record_reasoning_phase(
            cache_key, _phase.COMPLETE if _phase else "complete",
            f"real generation succeeded, source={source}, "
            f"{len(code)} chars of code extracted")

        # Cache the GENERATION outcome now, independent of whatever happens to
        # caller_unit afterward in `finally` — the code is correct-or-not on
        # its own merits, and a future identical call should not inherit an
        # unrelated Bonsai-restore hiccup from THIS run. json.dumps reads the
        # dict content immediately, so later mutations to `result` (elapsed_s,
        # a possible status downgrade to caller_restore_failed) cannot corrupt
        # the already-serialized cached copy.
        if store is not None:
            try:
                cache_payload = {
                    "status": "ok", "code": code, "raw_content": result["raw_content"],
                    "reasoning_excerpt": result["reasoning_excerpt"], "error": "",
                    "source": source,
                }
                scope = _awm.Scope(tenant=_MEMORY_SCOPE_TENANT)
                store.remember(scope, cache_key, json.dumps(cache_payload),
                                kind="codegen_result",
                                meta={"prompt_preview": prompt[:120]})
            except Exception as exc:  # noqa: BLE001 — cache write failure, not a tool failure
                logger.debug("codegen_bridge: cache write failed: %s", exc)

        return result

    except Exception as exc:  # noqa: BLE001 — fail soft, always fall through to finally
        result["status"] = "error"
        result["error"] = f"unexpected exception: {exc}"
        return result

    finally:
        # Two INDEPENDENT cleanup steps, each defended against its own
        # failure separately and on purpose: an orphaned Qwen3.8 container is
        # an annoyance (next call's `podman rm -f` clears it); the caller
        # (Bonsai) not coming back is a fleet outage. Conflating the two
        # status codes would tell an operator to go check on Bonsai when the
        # real problem was a stray container, or vice versa. Neither step may
        # raise past this block — a bad mock in testing proved that trusting
        # `_wsl`'s own fail-soft contract is not enough; this block defends
        # itself too.
        try:
            _wsl(f"podman stop {_QWEN_CONTAINER}", timeout=_TEARDOWN_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — teardown must never raise
            result["error"] = (
                result.get("error", "") +
                f" | Qwen3.8 teardown raised unexpectedly (non-fatal, next "
                f"call self-heals via podman rm -f): {exc}")

        if caller_was_quiesced:
            try:
                rc, out = _wsl(f"systemctl start {caller_unit}", timeout=15)
                if rc != 0:
                    result["status"] = "caller_restore_failed"
                    result["error"] = (result.get("error", "") +
                                        f" | ALSO failed to restart {caller_unit}: {out}")
                else:
                    # Verify it actually comes back, in-network (its port is
                    # not published to the host — connection-refused there is
                    # expected and NOT a failure signal, measured this
                    # session).
                    probe_payload = {
                        "model": "bonsai-27b",
                        "messages": [{"role": "user", "content": "ok"}],
                        "max_tokens": 5,
                    }
                    probe_cmd = (
                        "podman exec aitheros-microscheduler curl -sk "
                        "--max-time 8 "
                        "https://localhost:8150/v1/chat/completions "
                        "-H 'Content-Type: application/json' "
                        f"-d {json.dumps(json.dumps(probe_payload))}"
                    )
                    deadline = time.time() + _RESTORE_TIMEOUT_S
                    restored = False
                    while time.time() < deadline:
                        rc, out = _wsl(probe_cmd, timeout=12)
                        if rc == 0 and '"model":"bonsai-27b"' in out:
                            restored = True
                            break
                        time.sleep(5)
                    if not restored:
                        result["status"] = "caller_restore_failed"
                        result["error"] = (
                            result.get("error", "") +
                            f" | {caller_unit} restarted but did not verify "
                            f"serving within {_RESTORE_TIMEOUT_S}s")
            except Exception as exc:  # noqa: BLE001 — restore must never raise
                result["status"] = "caller_restore_failed"
                result["error"] = (
                    result.get("error", "") +
                    f" | caller restore raised unexpectedly: {exc}")

        result["elapsed_s"] = round(time.time() - t0, 1)
