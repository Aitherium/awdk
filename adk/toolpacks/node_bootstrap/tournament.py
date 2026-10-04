"""Engine tournament: pick the fastest inference engine that still serves the SAME model.

Several recipes (inference engines) serving one model on one node are compared.
Quality parity is a HARD gate that runs before speed counts: an engine that is fast
because it is quietly computing something else (a different quant, a broken chat
template, a truncated context) must never win.

Everything here is stdlib only and speaks the OpenAI-compatible HTTP API
(``/v1/models``, ``/v1/chat/completions``), so any engine that exposes it can enter.

Why parity is measured against a NOISE FLOOR, not against byte-identity
------------------------------------------------------------------------
Two engines running the same weights rarely produce byte-identical greedy output.
Different matmul kernels, fused attention and accumulation order shift logits by tiny
amounts; greedy decoding turns the first near-tie that flips into an entirely
different continuation. Even ONE engine can do this between two runs (batching,
non-deterministic reductions). So the reference is run twice first: reference vs
reference is the noise floor, and a candidate passes when its agreement with the
reference is no worse than that floor minus a tolerance. A reference that disagrees
with itself too much cannot judge anyone, and the verdict is UNJUDGED.

Verdicts
--------
PASS      every gated metric is within tolerance of the floor.
FAIL      at least one gated metric is measurably worse than the floor.
UNJUDGED  an endpoint is unreachable, the model id is not served by both sides, a
          required metric could not be computed (logprobs missing on either side),
          or the reference is too non-deterministic to be a judge. An absent metric
          NEVER counts as a pass.

CLI exit codes follow the same contract: 0 pass/ok, 1 violation, 2 could not judge.
"""

from __future__ import annotations

import hashlib
import http.client
import http.server
import json
import os
import platform
import random
import re
import shlex
import socket
import ssl
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Union

TOURNAMENT_VERSION = "1"
SCHEMA_VERSION = 1

PASS = "PASS"
FAIL = "FAIL"
UNJUDGED = "UNJUDGED"

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_UNJUDGED = 2

DEFAULT_SEED = 1234
DEFAULT_MAX_TOKENS = 64
DEFAULT_TOP_LOGPROBS = 5
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_MAX_AGE_S = 30 * 24 * 3600  # a ranking older than this is ignored

ENV_TOURNAMENT_FILE = "AITHER_TOURNAMENT_FILE"

# Tolerances against the reference-vs-reference noise floor. Why each default:
#
# * exact_match_rate / divergence_median (text metrics, 0.40): coarse. Once one
#   near-tie flips, the rest of a greedy answer differs, so cross-engine exact match
#   over ~64 tokens is routinely far below a self-consistent reference even when the
#   engines are numerically equivalent. These catch gross breakage (wrong template,
#   wrong model, garbage), not subtle drift. When logprobs are measured on both sides
#   they are gated TIE-ADJUSTED: a prompt whose first divergence is a forgiven near-tie
#   (see tie_margin) counts as a match, because what follows a tie flip is a different
#   but equally valid greedy path. A divergence the reference did NOT see as a tie
#   still counts against the candidate. The raw values stay in the report.
# * top1_agreement (0.03): the sharp gate. It is only compared at positions where
#   both engines saw the IDENTICAL context (the matched prefix plus the first
#   divergent position), so an equivalent engine agrees on the argmax almost
#   everywhere; flips at genuine near-ties are forgiven (see tie_margin).
# * tie_margin (0.10 nats): a top-1 disagreement where the reference itself scored
#   the candidate's choice within this margin of its own top-1 is kernel noise, not a
#   different model.
# * topk_overlap (0.15): the tail of the top-k set reorders with small logit noise;
#   a different quantization or model changes the SET.
# * mean_abs_dlogprob (0.10 nats): the chosen token's logprob, compared where both
#   engines chose the same token from the same context. Numeric noise moves it by
#   hundredths; a different quant moves it by tenths.
# * min_ref_exact_match (0.50): used only when the reference returns NO logprobs: if
#   it disagrees with itself on more than half the prompts at temperature 0 with a
#   fixed seed, its outputs are not a stable target and the tournament refuses to judge.
# * min_ref_top1 (0.95): the same refusal when logprobs ARE available, judged on the
#   argmax instead of the text. A batching server (continuous batching, prefix cache,
#   fp8 KV) is not batch-invariant: measured on a production vLLM lane, two passes of
#   the same 12 prompts matched exactly on only 42% of answers while agreeing on the
#   top-1 token at 98.6% of positions (mean |dlogprob| 0.004). Text equality would
#   refuse every such server; the argmax is the stable signal.
#
# When logprob metrics are measured on BOTH sides the text metrics are ADVISORY:
# reported in ``checks`` but never the reason for a FAIL, for the same reason. Without
# logprobs they are the only signal and they gate.
DEFAULT_TOLERANCES: dict[str, float] = {
    "exact_match_rate": 0.40,
    "divergence_median": 0.40,
    "top1_agreement": 0.03,
    "topk_overlap": 0.15,
    "mean_abs_dlogprob": 0.10,
    "tie_margin": 0.10,
    "min_ref_exact_match": 0.50,
    "min_ref_top1": 0.95,
}

TEXT_METRICS = ("exact_match_rate", "divergence_median")
LOGPROB_METRICS = ("top1_agreement", "topk_overlap", "mean_abs_dlogprob")
LOWER_IS_BETTER = {"mean_abs_dlogprob"}


# ---------------------------------------------------------------------------
# Golden prompts
# ---------------------------------------------------------------------------

def _gp(pid: str, category: str, prompt: str) -> dict:
    return {"id": pid, "category": category, "prompt": prompt}


GOLDEN_PROMPTS: list[dict] = [
    _gp("prose-01", "prose", "Describe a quiet harbor at dawn in two sentences."),
    _gp("prose-02", "prose", "Explain why the sky looks blue to a ten-year-old."),
    _gp("prose-03", "prose", "Write a one-paragraph summary of how a bicycle works."),
    _gp("prose-04", "prose", "Give three reasons people keep houseplants."),
    _gp("prose-05", "prose", "Write a short, polite email declining a meeting invite."),
    _gp("prose-06", "prose", "What is the difference between weather and climate?"),
    _gp("code-01", "code", "Write a Python function that reverses a string."),
    _gp("code-02", "code", "Write a SQL query returning the 5 newest rows of table `orders`."),
    _gp("code-03", "code", "In JavaScript, how do you remove duplicates from an array?"),
    _gp("code-04", "code", "Write a bash one-liner that counts lines in all .txt files."),
    _gp("code-05", "code", "Explain what this does: `[x * x for x in range(5) if x % 2]`"),
    _gp("code-06", "code", "Write a Rust function that returns the max of a slice of i32."),
    _gp("math-01", "math", "What is 17 * 23? Answer with the number only."),
    _gp("math-02", "math", "Solve for x: 3x + 7 = 22. Show the steps briefly."),
    _gp("math-03", "math", "What is the sum of the integers from 1 to 100?"),
    _gp("math-04", "math", "A train travels 180 km in 2.5 hours. What is its average speed?"),
    _gp("math-05", "math", "Is 221 a prime number? Explain in one sentence."),
    _gp("math-06", "math", "Convert 0.375 to a fraction in lowest terms."),
    _gp("json-01", "extraction",
        'Extract JSON {"name","age"} from: "Maria is 34 years old." Output JSON only.'),
    _gp("json-02", "extraction",
        'Return a JSON array of the colors in: "red apples, green pears, yellow bananas".'),
    _gp("json-03", "extraction",
        'Output a tool call as JSON {"tool":"get_weather","args":{"city":...}} '
        "for: what's the weather in Lisbon?"),
    _gp("json-04", "extraction",
        'From "Order #4411 shipped on 2024-03-02 to Oslo", output JSON with keys '
        "order_id, date, city."),
    _gp("json-05", "extraction",
        "List the verbs in this sentence as a JSON array: The cat jumped and ran away."),
    _gp("multi-01", "multilingual", "Translate to French: The library opens at nine."),
    _gp("multi-02", "multilingual", "Translate to Spanish: I would like a glass of water."),
    _gp("multi-03", "multilingual", "Translate to German: Where is the train station?"),
    _gp("multi-04", "multilingual", "What does the Japanese word 'arigatou' mean?"),
    _gp("multi-05", "multilingual", "Translate to English: 'Il pleut depuis ce matin.'"),
    _gp("instr-01", "instruction", "List exactly three fruits, one per line, no numbering."),
    _gp("instr-02", "instruction", "Reply with the word 'yes' in uppercase and nothing else."),
    _gp("instr-03", "instruction", "Write a haiku about autumn. Do not add a title."),
    _gp("instr-04", "instruction",
        "Rewrite in passive voice: The committee approved the new budget."),
]


def load_prompts(path: Union[str, Path]) -> list[dict]:
    """Load prompts from a JSONL file: one ``{"id", "prompt", "category"?}`` per line."""
    out: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or not isinstance(row.get("prompt"), str):
                raise ValueError(f"{path}:{n}: each line needs a string 'prompt'")
            out.append({
                "id": str(row.get("id") or f"p{n:03d}"),
                "category": str(row.get("category") or "custom"),
                "prompt": row["prompt"],
            })
    if not out:
        raise ValueError(f"{path}: no prompts")
    return out


def _prompts_digest(prompts: list[dict]) -> str:
    h = hashlib.sha256()
    for p in prompts:
        h.update(p["prompt"].encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

class EndpointError(RuntimeError):
    """An endpoint could not be reached or returned something unusable."""


@dataclass
class Conn:
    """How to talk to the endpoints.

    ``token_env`` names an environment variable holding a bearer token; the token
    itself is never passed on the command line. TLS verification is ON by default
    (system store, or ``ca_bundle``); ``insecure`` turns it off and says so.
    """

    token_env: str = ""
    ca_bundle: str = ""
    insecure: bool = False
    timeout: float = DEFAULT_TIMEOUT_S
    _warned: bool = field(default=False, repr=False, compare=False)

    def token(self) -> str:
        return os.environ.get(self.token_env, "") if self.token_env else ""

    def ssl_context(self) -> ssl.SSLContext:
        if self.insecure:
            if not self._warned:
                print("WARNING: TLS certificate verification is DISABLED (--insecure); "
                      "results may come from an impersonated endpoint.", file=sys.stderr)
                self._warned = True
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return ssl.create_default_context(cafile=self.ca_bundle or None)


def _base(url: str) -> str:
    u = url.rstrip("/")
    if u.endswith("/v1"):
        u = u[:-3]
    return u


def _open(url: str, conn: Conn, body: Optional[dict] = None, timeout: Optional[float] = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Accept", "application/json, text/event-stream")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    tok = conn.token()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    ctx = conn.ssl_context() if url.lower().startswith("https:") else None
    try:
        return urllib.request.urlopen(req, timeout=timeout or conn.timeout, context=ctx)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(300).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 -- the status code alone still explains it
            detail = ""
        raise EndpointError(f"HTTP {e.code} from {url}: {detail}".strip()) from e
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        raise EndpointError(f"cannot reach {url}: {e}") from e


def _get_json(url: str, conn: Conn, body: Optional[dict] = None,
              timeout: Optional[float] = None) -> dict:
    with _open(url, conn, body, timeout) as resp:
        raw = resp.read()
    try:
        out = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise EndpointError(f"non-JSON response from {url}") from e
    if not isinstance(out, dict):
        raise EndpointError(f"unexpected JSON shape from {url}")
    return out


def list_models(url: str, conn: Optional[Conn] = None) -> list[str]:
    """Model ids served at ``url`` per ``/v1/models``."""
    conn = conn or Conn()
    data = _get_json(_base(url) + "/v1/models", conn)
    return [str(m.get("id")) for m in data.get("data", []) if isinstance(m, dict)]


# ---------------------------------------------------------------------------
# Completions
# ---------------------------------------------------------------------------

@dataclass
class Completion:
    text: str
    tokens: Optional[list[str]]          # None when the engine returned no logprobs
    chosen_logprobs: Optional[list[float]]
    top: Optional[list[list[tuple[str, float]]]]  # per position, top-k (token, logprob)


def complete(url: str, model: str, prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS,
             top_logprobs: int = DEFAULT_TOP_LOGPROBS, conn: Optional[Conn] = None,
             seed: int = DEFAULT_SEED) -> Completion:
    """One greedy chat completion (temperature 0, fixed seed) with per-token logprobs.

    If the engine rejects the logprobs parameters (HTTP 400), the request is retried
    without them and the result carries ``tokens=None`` -- the caller then decides
    whether logprob metrics were required.
    """
    conn = conn or Conn()
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "seed": seed,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if top_logprobs > 0:
        body["logprobs"] = True
        body["top_logprobs"] = top_logprobs
    endpoint = _base(url) + "/v1/chat/completions"
    try:
        data = _get_json(endpoint, conn, body)
    except EndpointError as e:
        if top_logprobs > 0 and "HTTP 400" in str(e):
            body.pop("logprobs", None)
            body.pop("top_logprobs", None)
            data = _get_json(endpoint, conn, body)
        else:
            raise
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise EndpointError(f"no choices in completion from {endpoint}")
    choice = choices[0]
    text = (choice.get("message") or {}).get("content") or ""
    lp = choice.get("logprobs")
    content = lp.get("content") if isinstance(lp, dict) else None
    if not content:
        return Completion(text=text, tokens=None, chosen_logprobs=None, top=None)
    tokens: list[str] = []
    chosen: list[float] = []
    top: list[list[tuple[str, float]]] = []
    for item in content:
        tok = str(item.get("token", ""))
        tokens.append(tok)
        chosen.append(float(item.get("logprob", 0.0)))
        alts = [(str(a.get("token", "")), float(a.get("logprob", 0.0)))
                for a in (item.get("top_logprobs") or []) if isinstance(a, dict)]
        top.append(alts)
    # top_logprobs requested but every position came back empty = not usable.
    if top_logprobs > 0 and not any(top):
        return Completion(text=text, tokens=tokens, chosen_logprobs=chosen, top=None)
    return Completion(text=text, tokens=tokens, chosen_logprobs=chosen, top=top)


# ---------------------------------------------------------------------------
# Parity metrics
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def _seq(c: Completion, use_tokens: bool) -> list[str]:
    if use_tokens and c.tokens is not None:
        return c.tokens
    return _WORD_RE.findall(c.text)


def _first_divergence(a: list[str], b: list[str]) -> int:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n if len(a) != len(b) else len(a)


def _top1(dist: list[tuple[str, float]]) -> Optional[str]:
    if not dist:
        return None
    return max(dist, key=lambda t: t[1])[0]


def _forgiven_tie(a: Completion, b: Completion, idx: int, tie_margin: float) -> bool:
    """True when position ``idx`` (the first divergence) is a near-tie flip.

    Both sides saw the same context up to ``idx``; the candidate's top-1 there must be
    in the reference's top-k within ``tie_margin`` nats of the reference's own top-1.
    """
    if a.top is None or b.top is None or a.text == b.text:
        return False
    if idx >= len(a.top) or idx >= len(b.top):
        return False  # one side simply ended earlier: a length difference, not a tie
    ta, tb = _top1(a.top[idx]), _top1(b.top[idx])
    if ta is None or tb is None or ta == tb:
        return False
    scores = dict(a.top[idx])
    return tb in scores and scores[ta] - scores[tb] <= tie_margin


def pair_metrics(ref: list[Completion], cand: list[Completion],
                 tie_margin: float = DEFAULT_TOLERANCES["tie_margin"]) -> dict:
    """Agreement metrics between two completion lists over the same prompts.

    Logprob metrics are ``None`` unless EVERY completion on BOTH sides carries
    top-k logprobs -- a partial measurement is not a measurement.
    """
    if len(ref) != len(cand) or not ref:
        raise ValueError("completion lists must be non-empty and the same length")
    n = len(ref)
    use_tokens = all(c.tokens is not None for c in ref + cand)
    exact = sum(1 for a, b in zip(ref, cand) if a.text == b.text) / n
    div_norm: list[float] = []
    divs: list[int] = []
    for a, b in zip(ref, cand):
        sa, sb = _seq(a, use_tokens), _seq(b, use_tokens)
        idx = _first_divergence(sa, sb)
        divs.append(idx)
        longest = max(len(sa), len(sb))
        div_norm.append(1.0 if longest == 0 else idx / longest)
    out: dict[str, Any] = {
        "prompts": n,
        "exact_match_rate": round(exact, 4),
        "divergence_median": round(statistics.median(div_norm), 4),
        "divergence_unit": "token" if use_tokens else "word",
        "top1_agreement": None,
        "topk_overlap": None,
        "mean_abs_dlogprob": None,
        "exact_match_rate_adj": None,
        "divergence_median_adj": None,
        "tie_forgiven": 0,
        "positions_compared": 0,
    }
    have_lp = all(c.top is not None and c.chosen_logprobs is not None for c in ref + cand)
    if not have_lp:
        return out
    agree = total = forgiven = 0
    jacc: list[float] = []
    dlp: list[float] = []
    for a, b, idx in zip(ref, cand, divs):
        assert a.top is not None and b.top is not None
        assert a.chosen_logprobs is not None and b.chosen_logprobs is not None
        # Positions 0..idx share an identical context on both sides.
        upto = min(idx + 1, len(a.top), len(b.top))
        for j in range(upto):
            da, db = a.top[j], b.top[j]
            ta, tb = _top1(da), _top1(db)
            if ta is None or tb is None:
                continue
            total += 1
            if ta == tb:
                agree += 1
            else:
                ref_scores = dict(da)
                best = ref_scores[ta]
                if tb in ref_scores and best - ref_scores[tb] <= tie_margin:
                    agree += 1
                    forgiven += 1
            sa_set, sb_set = {t for t, _ in da}, {t for t, _ in db}
            union = sa_set | sb_set
            jacc.append(len(sa_set & sb_set) / len(union) if union else 1.0)
        same = min(idx, len(a.chosen_logprobs), len(b.chosen_logprobs))
        for j in range(same):
            dlp.append(abs(a.chosen_logprobs[j] - b.chosen_logprobs[j]))
    # Tie-adjusted text metrics: a prompt whose FIRST divergence is a near-tie the
    # reference itself scored within tie_margin is treated as a full match.
    exact_adj = 0
    div_adj: list[float] = []
    for a, b, idx, dn in zip(ref, cand, divs, div_norm):
        tie = _forgiven_tie(a, b, idx, tie_margin)
        exact_adj += 1 if (a.text == b.text or tie) else 0
        div_adj.append(1.0 if tie else dn)
    out["exact_match_rate_adj"] = round(exact_adj / n, 4)
    out["divergence_median_adj"] = round(statistics.median(div_adj), 4)
    out["positions_compared"] = total
    out["tie_forgiven"] = forgiven
    if total:
        out["top1_agreement"] = round(agree / total, 4)
        out["topk_overlap"] = round(statistics.fmean(jacc), 4)
    if dlp:
        out["mean_abs_dlogprob"] = round(statistics.fmean(dlp), 4)
    return out


@dataclass
class ParityResult:
    verdict: str
    reasons: list[str]
    model: str
    ref_url: str
    cand_url: str
    floor: Optional[dict] = None
    candidate: Optional[dict] = None
    checks: list[dict] = field(default_factory=list)
    tolerances: dict = field(default_factory=dict)
    require_logprobs: bool = True

    @property
    def exit_code(self) -> int:
        return {PASS: EXIT_OK, FAIL: EXIT_VIOLATION}.get(self.verdict, EXIT_UNJUDGED)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["exit_code"] = self.exit_code
        return d


def _tol(overrides: Optional[dict]) -> dict:
    t = dict(DEFAULT_TOLERANCES)
    for k, v in (overrides or {}).items():
        if k not in t:
            raise ValueError(f"unknown tolerance {k!r}; known: {sorted(t)}")
        t[k] = float(v)
    return t


def _run_prompts(url: str, model: str, prompts: list[dict], max_tokens: int,
                 top_logprobs: int, conn: Conn) -> list[Completion]:
    return [complete(url, model, p["prompt"], max_tokens, top_logprobs, conn)
            for p in prompts]


def _check_model(url: str, model: str, conn: Conn) -> Optional[str]:
    """None when ``model`` is served at ``url``; else the UNJUDGED reason."""
    try:
        ids = list_models(url, conn)
    except EndpointError as e:
        return f"unreachable: {e}"
    if model not in ids:
        return f"model {model!r} not served at {url} (serves {ids[:8]})"
    return None


@dataclass
class _ReferenceRun:
    passes: Optional[tuple[list[Completion], list[Completion]]]
    floor: Optional[dict]
    problem: Optional[str]


def _reference_floor(ref_url: str, model: str, prompts: list[dict], max_tokens: int,
                     top_logprobs: int, conn: Conn, tol: dict) -> _ReferenceRun:
    why = _check_model(ref_url, model, conn)
    if why:
        return _ReferenceRun(None, None, f"reference {why}")
    try:
        p1 = _run_prompts(ref_url, model, prompts, max_tokens, top_logprobs, conn)
        p2 = _run_prompts(ref_url, model, prompts, max_tokens, top_logprobs, conn)
    except EndpointError as e:
        return _ReferenceRun(None, None, f"reference failed mid-run: {e}")
    floor = pair_metrics(p1, p2, tol["tie_margin"])
    if floor.get("top1_agreement") is not None:
        if floor["top1_agreement"] < tol["min_ref_top1"]:
            return _ReferenceRun((p1, p2), floor, (
                f"reference is non-deterministic: top-1 agreement against itself is "
                f"{floor['top1_agreement']:.3f} < {tol['min_ref_top1']:.3f} at "
                "temperature 0 with a fixed seed; it cannot judge parity"))
        return _ReferenceRun((p1, p2), floor, None)
    if floor["exact_match_rate"] < tol["min_ref_exact_match"]:
        return _ReferenceRun((p1, p2), floor, (
            f"reference is non-deterministic: exact-match against itself is "
            f"{floor['exact_match_rate']:.2f} < {tol['min_ref_exact_match']:.2f} at "
            "temperature 0 with a fixed seed; it cannot judge parity"))
    return _ReferenceRun((p1, p2), floor, None)


def _judge(floor: dict, cand: dict, tol: dict, require_logprobs: bool
           ) -> tuple[str, list[str], list[dict]]:
    checks: list[dict] = []
    reasons: list[str] = []
    missing: list[str] = []
    gated = list(TEXT_METRICS) + list(LOGPROB_METRICS)
    have_logprobs = all(floor.get(m) is not None and cand.get(m) is not None
                        for m in ("top1_agreement", "topk_overlap"))
    for m in gated:
        fv, cv = floor.get(m), cand.get(m)
        basis = "raw"
        if m in TEXT_METRICS:
            fa, ca = floor.get(m + "_adj"), cand.get(m + "_adj")
            if fa is not None and ca is not None:
                fv, cv, basis = fa, ca, "tie-adjusted"
        if m in TEXT_METRICS:
            required = not have_logprobs
        else:
            required = require_logprobs
        if fv is None or cv is None:
            side = "reference" if fv is None else "candidate"
            if cv is None and fv is None:
                side = "both sides"
            checks.append({"metric": m, "floor": fv, "candidate": cv, "threshold": None,
                           "ok": None, "required": required,
                           "note": f"not measurable ({side})"})
            if required:
                missing.append(f"{m} not measurable on {side} (logprobs absent?)")
            continue
        if m in LOWER_IS_BETTER:
            threshold = fv + tol[m]
            ok = cv <= threshold
            rel = "<="
        else:
            threshold = fv - tol[m]
            ok = cv >= threshold
            rel = ">="
        checks.append({"metric": m, "floor": fv, "candidate": cv,
                       "threshold": round(threshold, 4), "ok": ok, "required": required,
                       "basis": basis})
        if not ok and required:
            label = m if basis == "raw" else f"{m}({basis})"
            reasons.append(f"{label}={cv} fails {rel} {threshold:.4f} (floor {fv})")
        elif not ok:
            checks[-1]["note"] = "advisory: logprob metrics decide when measured"
    if reasons:
        return FAIL, reasons + missing, checks
    if missing:
        return UNJUDGED, missing, checks
    return PASS, ["all gated metrics within tolerance of the reference noise floor"], checks


def parity(ref_url: str, cand_url: str, model: str, prompts: Optional[list[dict]] = None,
           max_tokens: int = DEFAULT_MAX_TOKENS, top_logprobs: int = DEFAULT_TOP_LOGPROBS,
           tolerances: Optional[dict] = None, require_logprobs: bool = True,
           conn: Optional[Conn] = None, _ref: Optional[_ReferenceRun] = None) -> ParityResult:
    """Is the candidate engine serving the same model as the reference, within noise?"""
    conn = conn or Conn()
    prompts = prompts or GOLDEN_PROMPTS
    tol = _tol(tolerances)
    res = ParityResult(verdict=UNJUDGED, reasons=[], model=model, ref_url=ref_url,
                       cand_url=cand_url, tolerances=tol, require_logprobs=require_logprobs)
    if require_logprobs and top_logprobs <= 0:
        res.reasons = ["logprob metrics required but top_logprobs <= 0"]
        return res
    ref = _ref or _reference_floor(ref_url, model, prompts, max_tokens, top_logprobs,
                                   conn, tol)
    res.floor = ref.floor
    if ref.problem or ref.passes is None or ref.floor is None:
        res.reasons = [ref.problem or "reference produced no measurement"]
        return res
    why = _check_model(cand_url, model, conn)
    if why:
        res.reasons = [f"candidate {why}"]
        return res
    try:
        cand = _run_prompts(cand_url, model, prompts, max_tokens, top_logprobs, conn)
    except EndpointError as e:
        res.reasons = [f"candidate failed mid-run: {e}"]
        return res
    res.candidate = pair_metrics(ref.passes[0], cand, tol["tie_margin"])
    res.verdict, res.reasons, res.checks = _judge(ref.floor, res.candidate, tol,
                                                  require_logprobs)
    return res


# ---------------------------------------------------------------------------
# Speed
# ---------------------------------------------------------------------------

# Chunks closer together than this (5000+/s) are a buffer being parsed, not decoding.
MIN_PLAUSIBLE_CHUNK_GAP_S = 2e-4

BENCH_PROMPT = ("Write a detailed, multi-paragraph explanation of how a refrigerator "
                "moves heat out of its interior.")


def _stream_once(url: str, model: str, prompt: str, max_tokens: int, conn: Conn) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "seed": DEFAULT_SEED,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    t0 = time.perf_counter()
    t_first: Optional[float] = None
    t_last: Optional[float] = None
    chunks = 0
    usage_tokens: Optional[int] = None
    with _open(_base(url) + "/v1/chat/completions", conn, body) as resp:
        while True:
            raw = resp.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                ev = json.loads(payload)
            except ValueError:
                continue
            usage = ev.get("usage")
            if isinstance(usage, dict) and usage.get("completion_tokens") is not None:
                usage_tokens = int(usage["completion_tokens"])
            for ch in ev.get("choices") or []:
                delta = ch.get("delta") or {}
                piece = delta.get("content") or delta.get("reasoning_content") or ""
                if piece:
                    now = time.perf_counter()
                    if t_first is None:
                        t_first = now
                    t_last = now
                    chunks += 1
    if t_first is None or t_last is None:
        raise EndpointError("stream produced no content")
    tokens = usage_tokens if usage_tokens is not None else chunks
    window = t_last - t_first
    # The first-to-last chunk window is decode time only if chunks arrived as they were
    # generated. A buffering proxy, or an engine that flushes in bursts, delivers them
    # all at once and the window collapses to parse time -- an absurd tok/s that would
    # win the ranking. Detect it (implausibly few chunks, or chunks arriving faster
    # than any engine decodes) and fall back to end-to-end tokens / total time, which
    # under-reports rather than over-reports.
    buffered = tokens > 1 and (
        chunks < max(2, tokens / 4)
        or (chunks > 1 and window / (chunks - 1) < MIN_PLAUSIBLE_CHUNK_GAP_S))
    if tokens <= 1:
        tps, method = None, "none"
    elif buffered or window <= 1e-6:
        total = t_last - t0
        tps, method = (tokens / total if total > 1e-6 else None), "end_to_end"
    else:
        tps, method = (tokens - 1) / window, "chunk_window"
    return {"ttft_s": t_first - t0, "decode_tps": tps, "tokens": tokens, "chunks": chunks,
            "token_source": "usage" if usage_tokens is not None else "chunks",
            "tps_method": method}


def _spread(vals: list[float]) -> dict:
    if not vals:
        return {"min": None, "max": None, "stdev": None}
    return {"min": round(min(vals), 4), "max": round(max(vals), 4),
            "stdev": round(statistics.pstdev(vals), 4)}


def bench(url: str, model: str, runs: int = 5, max_tokens: int = 128, warmup: int = 1,
          prompt: str = BENCH_PROMPT, conn: Optional[Conn] = None) -> dict:
    """Streaming speed: TTFT and decode tok/s, median and spread over ``runs``.

    Token counts prefer the engine's ``usage`` (exact); otherwise one content chunk
    is counted as one token, and ``token_source`` records which was used.
    ``tps_method`` records whether tok/s came from the chunk window or, for a
    buffered stream, from end-to-end time. ``ok`` needs a MAJORITY of the measured
    runs to succeed: a median of one sample out of five is not a measurement.
    """
    conn = conn or Conn()
    runs = max(1, runs)
    errors: list[str] = []
    for _ in range(max(0, warmup)):
        try:
            _stream_once(url, model, prompt, max_tokens, conn)
        except EndpointError as e:
            errors.append(f"warmup: {e}")
    samples: list[dict] = []
    run_errors = 0
    for _ in range(runs):
        try:
            samples.append(_stream_once(url, model, prompt, max_tokens, conn))
        except EndpointError as e:
            run_errors += 1
            errors.append(str(e))
    ttfts = [s["ttft_s"] for s in samples]
    tpss = [s["decode_tps"] for s in samples if s["decode_tps"] is not None]
    sources = sorted({s["token_source"] for s in samples})
    methods = sorted({s["tps_method"] for s in samples})
    min_ok = (runs + 1) // 2
    ok = len(samples) >= min_ok and len(tpss) >= min_ok
    if samples and not ok:
        errors.insert(0, f"only {len(samples)}/{runs} runs succeeded "
                         f"({len(tpss)} with a tok/s); need {min_ok}")
    return {
        "ok": ok,
        "url": url,
        "model": model,
        "runs": len(samples),
        "runs_requested": runs,
        "run_errors": run_errors,
        "tps_method": methods[0] if len(methods) == 1 else ("mixed" if methods else None),
        "ttft_s_median": round(statistics.median(ttfts), 4) if ttfts else None,
        "ttft_s_spread": _spread(ttfts),
        "decode_tps_median": round(statistics.median(tpss), 2) if tpss else None,
        "decode_tps_spread": _spread(tpss),
        "token_source": sources[0] if len(sources) == 1 else ("mixed" if sources else None),
        "errors": errors[:5],
    }


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------

def _coherent(text: str) -> bool:
    return bool(text.strip()) and any(ch.isalnum() for ch in text)


def run_action(action: Union[str, Callable[[], Any], None]) -> Optional[subprocess.Popen]:
    if action is None:
        return None
    if callable(action):
        action()
        return None
    argv = action if os.name == "nt" else shlex.split(action)
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cold_start(launch: Union[str, Callable[[], Any]], url: str, model: str,
               timeout: float = 600.0, stop: Union[str, Callable[[], Any], None] = None,
               poll_s: float = 1.0, conn: Optional[Conn] = None,
               stop_after: bool = False) -> dict:
    """Seconds from launch to the first COHERENT completion (not merely /health 200).

    ``launch``/``stop`` are a shell-style command or a callable. ``stop`` runs only when
    ``stop_after`` is true (a tournament stops an entrant after measuring it).

    A short-timeout ``/v1/models`` probe only detects "nothing listening yet"; the
    completion itself gets the caller's full ``conn.timeout`` (capped by the time left),
    so a slow engine is never declared dead just because one completion takes a while,
    and no poll abandons a generation the engine is still working on.
    """
    conn = conn or Conn()
    quick = Conn(token_env=conn.token_env, ca_bundle=conn.ca_bundle,
                 insecure=conn.insecure, timeout=min(conn.timeout, max(2.0, poll_s * 5)))
    t0 = time.perf_counter()
    try:
        run_action(launch)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "seconds": None, "error": f"launch failed: {e}"}
    last_err = ""
    result: dict[str, Any] = {"ok": False, "seconds": None, "error": ""}
    while time.perf_counter() - t0 < timeout:
        try:
            try:
                list_models(url, quick)
            except EndpointError as e:
                if not str(e).startswith("HTTP "):
                    raise  # not listening yet: poll again, no completion attempted
                # an HTTP answer (even an error) means something is serving: go on
            left = max(1.0, timeout - (time.perf_counter() - t0))
            full = Conn(token_env=conn.token_env, ca_bundle=conn.ca_bundle,
                        insecure=conn.insecure, timeout=min(conn.timeout, left))
            full._warned = quick._warned or conn._warned  # one --insecure warning
            c = complete(url, model, "Reply with the single word: ready", 8, 0, full)
            if _coherent(c.text):
                result = {"ok": True, "seconds": round(time.perf_counter() - t0, 3),
                          "error": ""}
                break
            last_err = f"incoherent completion {c.text!r}"
        except EndpointError as e:
            last_err = str(e)
        time.sleep(poll_s)
    else:
        result = {"ok": False, "seconds": None,
                  "error": f"no coherent completion within {timeout}s: {last_err}"}
    if stop_after and stop is not None:
        try:
            run_action(stop)
        except Exception as e:  # noqa: BLE001
            result["stop_error"] = str(e)
    return result


# ---------------------------------------------------------------------------
# Tournament
# ---------------------------------------------------------------------------

def tournament_file(path: Union[str, Path, None] = None) -> Path:
    if path:
        return Path(path).expanduser()
    env = os.environ.get(ENV_TOURNAMENT_FILE, "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".aither" / "node-bootstrap" / "tournament.json"


def _host_hash() -> str:
    return hashlib.sha256(socket.gethostname().encode("utf-8")).hexdigest()[:16]


def node_fingerprint() -> dict:
    gpu = ""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=5)
        if r.returncode == 0:
            gpu = "; ".join(x.strip() for x in r.stdout.splitlines() if x.strip())
    except (OSError, subprocess.SubprocessError):
        gpu = ""  # no NVIDIA tooling on this node: the fingerprint just omits the GPU
    return {"host_sha256": _host_hash(), "gpu": gpu, "os": platform.system(),
            "machine": platform.machine()}


def _versions() -> dict:
    v = {"tournament": TOURNAMENT_VERSION, "python": platform.python_version()}
    try:
        from importlib.metadata import version
        v["awdk"] = version("awdk")
    except Exception:  # noqa: BLE001 -- running from a source tree, not an install
        v["awdk"] = "unknown"
    return v


def rank_entrants(results: list[dict], reference: str) -> list[str]:
    """PASS entrants with a working bench, fastest first.

    The reference is eligible without a candidate-vs-reference comparison, but only
    when its own floor run was judged (verdict PASS). A reference that was
    unreachable, served the wrong model or was non-deterministic judged nothing, so
    no ranking can come out of that run.

    Order: decode tok/s desc, then TTFT asc, then cold start asc (unmeasured last).
    """
    eligible = []
    for r in results:
        b = r.get("bench") or {}
        if not b.get("ok") or b.get("decode_tps_median") is None:
            continue
        if (r.get("parity") or {}).get("verdict") != PASS:
            continue
        cs = (r.get("cold_start") or {}).get("seconds")
        eligible.append((-float(b["decode_tps_median"]),
                         float(b.get("ttft_s_median") or float("inf")),
                         float(cs) if cs is not None else float("inf"),
                         r["recipe_id"]))
    eligible.sort()
    return [e[3] for e in eligible]


def run_tournament(entrants: list[dict], reference: str, model: str,
                   prompts: Optional[list[dict]] = None, runs: int = 5,
                   max_tokens: int = DEFAULT_MAX_TOKENS, bench_max_tokens: int = 128,
                   top_logprobs: int = DEFAULT_TOP_LOGPROBS,
                   tolerances: Optional[dict] = None, require_logprobs: bool = True,
                   conn: Optional[Conn] = None, out_path: Union[str, Path, None] = None,
                   write: bool = True, cold_start_timeout: float = 600.0) -> dict:
    """Run parity + bench (+ optional cold start) for every entrant and rank them.

    ``entrants``: ``[{"recipe_id", "url", "launch"?, "stop"?}]``. The reference must be
    one of them. Writes the verdict file unless ``write`` is false.

    Entrants are measured ONE AT A TIME, reference first: launch (if given) -> parity
    -> bench -> stop (if given). Two engines for the same model are therefore never
    resident together because of the tournament itself, which on a single-GPU node
    would mean OOM or contention skewing the speed numbers. The reference's two
    floor passes are cached, so it does not need to stay up for the candidates.
    """
    conn = conn or Conn()
    prompts = prompts or GOLDEN_PROMPTS
    tol = _tol(tolerances)
    ids = [e["recipe_id"] for e in entrants]
    if reference not in ids:
        raise ValueError(f"reference {reference!r} is not among entrants {ids}")
    if len(set(ids)) != len(ids):
        raise ValueError(f"duplicate entrant ids in {ids}")
    started = time.time()
    by_id = {e["recipe_id"]: e for e in entrants}
    results: dict[str, dict] = {}

    ref_url = by_id[reference]["url"]
    ref_run: Optional[_ReferenceRun] = None
    order = [by_id[reference]] + [e for e in entrants if e["recipe_id"] != reference]
    for e in order:
        rid = e["recipe_id"]
        cold = (cold_start(e["launch"], e["url"], model, timeout=cold_start_timeout,
                           conn=conn) if e.get("launch") else None)
        if rid == reference:
            ref_run = _reference_floor(ref_url, model, prompts, max_tokens, top_logprobs,
                                       conn, tol)
            par = {"verdict": PASS if not ref_run.problem else UNJUDGED,
                   "reasons": ["reference entrant" if not ref_run.problem
                               else ref_run.problem],
                   "floor": ref_run.floor}
        else:
            assert ref_run is not None  # the reference is always measured first
            par = parity(ref_url, e["url"], model, prompts, max_tokens, top_logprobs,
                         tol, require_logprobs, conn, _ref=ref_run).to_dict()
        results[rid] = {"recipe_id": rid, "url": e["url"], "parity": par,
                        "bench": bench(e["url"], model, runs, bench_max_tokens, conn=conn),
                        "cold_start": cold}
        if e.get("stop"):
            try:
                run_action(e["stop"])
            except Exception as ex:  # noqa: BLE001
                results[rid]["stop_error"] = str(ex)
    assert ref_run is not None

    ordered = [results[i] for i in ids]
    ranking = rank_entrants(ordered, reference)
    verdicts = [r["parity"]["verdict"] for r in ordered if r["recipe_id"] != reference]
    if FAIL in verdicts:
        exit_code = EXIT_VIOLATION
    elif UNJUDGED in verdicts or ref_run.problem or not ranking:
        exit_code = EXIT_UNJUDGED
    else:
        exit_code = EXIT_OK
    report = {
        "schema": SCHEMA_VERSION,
        "model": model,
        "reference": reference,
        "started_at": started,
        "finished_at": time.time(),
        "node": node_fingerprint(),
        "versions": _versions(),
        "prompts": {"count": len(prompts), "sha256_16": _prompts_digest(prompts)},
        "tolerances": tol,
        "require_logprobs": require_logprobs,
        "entrants": ordered,
        "ranking": ranking,
        "exit_code": exit_code,
    }
    if write:
        report["written_to"] = str(_write_report(report, out_path))
    return report


def _write_report(report: dict, out_path: Union[str, Path, None]) -> Path:
    path = tournament_file(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {"schema": SCHEMA_VERSION, "models": {}}
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(old, dict) and isinstance(old.get("models"), dict):
            doc["models"] = old["models"]
    except (OSError, ValueError):
        doc["models"] = {}  # no previous file, or unreadable: start fresh
    doc["models"][report["model"]] = {k: v for k, v in report.items() if k != "written_to"}
    doc["latest_model"] = report["model"]
    doc["updated_at"] = report["finished_at"]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_ranking(model: Optional[str] = None, max_age_s: float = DEFAULT_MAX_AGE_S,
                 path: Union[str, Path, None] = None) -> list[str]:
    """Measured recipe order for ``model`` (default: the latest tournament).

    Empty when there is no file, it is unreadable, stale, or was measured on another
    host -- a ranking only means something on the machine that produced it.
    """
    try:
        doc = json.loads(tournament_file(path).read_text(encoding="utf-8"))
        runs = doc.get("models") or {}
        key = model or doc.get("latest_model")
        run = runs.get(key) if key else None
        if not isinstance(run, dict):
            return []
        if time.time() - float(run.get("finished_at", 0)) > max_age_s:
            return []
        if (run.get("node") or {}).get("host_sha256") != _host_hash():
            return []
        ranking = run.get("ranking") or []
        return [str(r) for r in ranking if isinstance(r, str)]
    except Exception:  # noqa: BLE001 -- a broken file must never break resolution
        return []


# ---------------------------------------------------------------------------
# Fake OpenAI-compatible engines (self-test and tests)
# ---------------------------------------------------------------------------

_VOCAB = ("the", "a", "cat", "river", "light", "moves", "quickly", "under", "stone",
          "bright", "and", "then", "quiet", "north", "number", "seven", "code", "runs",
          "over", "blue", "field", "small", "house", "warm", "wind", "is", "of", "to",
          "green", "night", "clock", "paper")


def _h(*parts: Any) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16)


class FakeEngine:
    """A tiny in-process OpenAI-compatible server with controllable behaviour.

    modes: ``identical`` (canonical generator), ``perturbed`` (same until
    ``diverge_after`` tokens, then different; logprobs shifted throughout),
    ``nologprobs`` (canonical text, no logprobs), ``nondeterministic`` (random
    divergence on every request), ``tieflip`` (canonical until ``tie_at``, where it
    takes the runner-up of a near-tie and continues differently -- numerically the
    same model; pair it with a reference built with the same ``tie_at``).

    ``tie_at`` makes position ``tie_at`` a near-tie (runner-up 0.02 nats behind) in
    every mode. ``completion_delay`` slows non-streaming completions; ``buffered``
    generates a stream and then sends every chunk in one write; ``fail_mod`` > 0 makes
    a request fail with HTTP 500 unless its sequence number is a multiple of it.
    """

    def __init__(self, mode: str = "identical", model: str = "test-model",
                 token_delay: float = 0.0, ttft_delay: float = 0.0, length: int = 24,
                 diverge_after: int = 3, stream_usage: bool = True,
                 require_token: str = "", ready: bool = True,
                 tie_at: Optional[int] = None, completion_delay: float = 0.0,
                 buffered: bool = False, fail_mod: int = 0):
        self.mode = mode
        self.tie_at = tie_at
        self.completion_delay = completion_delay
        self.buffered = buffered
        self.fail_mod = fail_mod
        self._requests = 0
        self._lock = threading.Lock()
        self.model = model
        self.token_delay = token_delay
        self.ttft_delay = ttft_delay
        self.length = length
        self.diverge_after = diverge_after
        self.stream_usage = stream_usage
        self.require_token = require_token
        self.ready = ready
        self._rng = random.Random()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeEngine":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def start(self) -> "FakeEngine":
        self._thread.start()
        return self

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def generate(self, prompt: str, max_tokens: int, k: int) -> list[tuple[str, float, list]]:
        n = max(1, min(self.length, max_tokens))
        salt = ""
        cut = n + 1
        shift = 0.0
        if self.mode == "perturbed":
            salt, cut, shift = "perturbed", self.diverge_after, 0.3
        elif self.mode == "nondeterministic":
            salt, cut = str(self._rng.random()), self._rng.randint(0, max(0, n - 1))
        elif self.mode == "batchnoise":
            # A batching server: same argmax almost everywhere, yet the text drifts at
            # the tail, so exact match against itself is ~0 while top-1 agreement is high.
            salt, cut = str(self._rng.random()), n - 1
        if self.mode == "tieflip" and self.tie_at is not None:
            salt, cut = "tieflip", self.tie_at + 1
        out = []
        for i in range(n):
            s = salt if i >= cut else ""
            hi = _h(prompt, i, s)
            tok = " " + _VOCAB[hi % len(_VOCAB)]
            lp = -0.05 - (hi % 20) / 100.0 - shift
            alts = [(tok, lp)]
            if i == self.tie_at:
                j = 0
                while True:  # a runner-up distinct from the top-1, 0.02 nats behind
                    j += 1
                    runner = " " + _VOCAB[_h(prompt, i, "tie", j) % len(_VOCAB)]
                    if runner != tok:
                        break
                alts.append((runner, lp - 0.02))
                if self.mode == "tieflip":
                    alts = [(runner, lp), (tok, lp - 0.02)]
                    tok = runner
            for j in range(1, max(1, k)):
                alt = " " + _VOCAB[_h(prompt, i, s, "alt", j) % len(_VOCAB)]
                if all(alt != a for a, _ in alts):
                    alts.append((alt, lp - 1.0 - 0.5 * j))
            out.append((tok, lp, alts[:k] if k else []))
        return out

    def _handler(self):
        engine = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                return None  # keep test output quiet

            def _send(self, code: int, obj: dict) -> None:
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _authed(self) -> bool:
                if not engine.require_token:
                    return True
                if self.headers.get("Authorization") == f"Bearer {engine.require_token}":
                    return True
                self._send(401, {"error": "unauthorized"})
                return False

            def do_GET(self) -> None:
                if self.path.rstrip("/") == "/health":
                    return self._send(200, {"status": "ok"})
                if not self._authed():
                    return
                if self.path.rstrip("/") == "/v1/models":
                    return self._send(200, {"object": "list",
                                            "data": [{"id": engine.model}]})
                self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                if not self._authed():
                    return
                if self.path.rstrip("/") != "/v1/chat/completions":
                    return self._send(404, {"error": "not found"})
                if not engine.ready:
                    return self._send(503, {"error": "loading"})
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                with engine._lock:
                    engine._requests += 1
                    seq = engine._requests
                if engine.fail_mod > 0 and seq % engine.fail_mod != 0:
                    return self._send(500, {"error": "flaky"})
                prompt = "".join(m.get("content", "") for m in req.get("messages", []))
                k = int(req.get("top_logprobs") or 0) if req.get("logprobs") else 0
                mt = req.get("max_tokens")
                gen = engine.generate(prompt, 16 if mt is None else int(mt), k)
                if req.get("stream"):
                    return self._stream(gen, req)
                time.sleep(engine.completion_delay)
                text = "".join(t for t, _, _ in gen)
                choice: dict[str, Any] = {"index": 0, "finish_reason": "length",
                                          "message": {"role": "assistant", "content": text}}
                if req.get("logprobs") and engine.mode != "nologprobs":
                    choice["logprobs"] = {"content": [
                        {"token": t, "logprob": lp,
                         "top_logprobs": [{"token": a, "logprob": b} for a, b in alts]}
                        for t, lp, alts in gen]}
                self._send(200, {"id": "x", "object": "chat.completion",
                                 "model": engine.model, "choices": [choice],
                                 "usage": {"completion_tokens": len(gen)}})

            def _stream(self, gen: list, req: dict) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                time.sleep(engine.ttft_delay)
                held: list[bytes] = []  # buffered mode: one write at the end
                for i, (tok, _, _) in enumerate(gen):
                    if i:
                        time.sleep(engine.token_delay)
                    ev = {"choices": [{"index": 0, "delta": {"content": tok}}]}
                    line = f"data: {json.dumps(ev)}\n\n".encode()
                    if engine.buffered:
                        held.append(line)
                    else:
                        self.wfile.write(line)
                        self.wfile.flush()
                want_usage = (req.get("stream_options") or {}).get("include_usage")
                if want_usage and engine.stream_usage:
                    ev = {"choices": [], "usage": {"completion_tokens": len(gen)}}
                    held.append(f"data: {json.dumps(ev)}\n\n".encode())
                held.append(b"data: [DONE]\n\n")
                self.wfile.write(b"".join(held))
                self.wfile.flush()
                self.close_connection = True

        return H


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def self_test(verbose: bool = True) -> int:
    """Prove each verdict path against fake engines. Exit 0 only if ALL hold."""
    import tempfile

    failures: list[str] = []
    prompts = GOLDEN_PROMPTS[:8]
    conn = Conn(timeout=10.0)

    def expect(name: str, cond: bool, detail: Any = "") -> None:
        if verbose:
            note = f" -- {detail}" if not cond else ""
            print(f"  [{'ok' if cond else 'FAIL'}] {name}{note}", file=sys.stderr)
        if not cond:
            failures.append(name)

    engines = {
        "ref": FakeEngine("identical"),
        "same": FakeEngine("identical"),
        "pert": FakeEngine("perturbed"),
        "nolp": FakeEngine("nologprobs"),
        "nondet": FakeEngine("nondeterministic"),
        "other": FakeEngine("identical", model="another-model"),
        "tieref": FakeEngine("identical", tie_at=8),
        "tieflip": FakeEngine("tieflip", tie_at=8),
    }
    for e in engines.values():
        e.start()
    try:
        u = {k: v.url for k, v in engines.items()}
        m = "test-model"
        r = parity(u["ref"], u["same"], m, prompts, max_tokens=16, conn=conn)
        expect("identical engine -> PASS", r.verdict == PASS, r.reasons)
        r = parity(u["ref"], u["pert"], m, prompts, max_tokens=16, conn=conn)
        expect("perturbed engine -> FAIL", r.verdict == FAIL, r.reasons)
        r = parity(u["tieref"], u["tieflip"], m, prompts, max_tokens=16, conn=conn)
        expect("near-tie flip (same model, different greedy path) -> PASS",
               r.verdict == PASS, r.reasons)
        r = parity(u["ref"], u["nolp"], m, prompts, max_tokens=16, conn=conn)
        expect("no-logprobs engine, logprobs required -> UNJUDGED",
               r.verdict == UNJUDGED, r.reasons)
        r = parity(u["nondet"], u["same"], m, prompts, max_tokens=16, conn=conn)
        expect("non-deterministic reference -> UNJUDGED",
               r.verdict == UNJUDGED and "non-deterministic" in " ".join(r.reasons),
               r.reasons)
        r = parity(u["ref"], u["other"], m, prompts, max_tokens=16, conn=conn)
        expect("model id mismatch -> UNJUDGED", r.verdict == UNJUDGED, r.reasons)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead = f"http://127.0.0.1:{s.getsockname()[1]}"
        r = parity(u["ref"], dead, m, prompts, max_tokens=16, conn=conn)
        expect("unreachable candidate -> UNJUDGED", r.verdict == UNJUDGED, r.reasons)
    finally:
        for e in engines.values():
            e.close()

    fast = FakeEngine("identical", token_delay=0.005)
    slow = FakeEngine("identical", token_delay=0.04)
    cheat = FakeEngine("perturbed", token_delay=0.0)
    for e in (fast, slow, cheat):
        e.start()
    try:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "t.json"
            rep = run_tournament(
                [{"recipe_id": "slow-ref", "url": slow.url},
                 {"recipe_id": "fast", "url": fast.url},
                 {"recipe_id": "cheat", "url": cheat.url}],
                reference="slow-ref", model="test-model", prompts=prompts, runs=2,
                max_tokens=16, bench_max_tokens=12, conn=conn, out_path=out)
            expect("ranking = PASS entrants by speed, FAIL excluded",
                   rep["ranking"] == ["fast", "slow-ref"], rep["ranking"])
            expect("tournament with a FAIL exits 1", rep["exit_code"] == EXIT_VIOLATION,
                   rep["exit_code"])
            expect("load_ranking reads the written file",
                   load_ranking(path=out) == ["fast", "slow-ref"], load_ranking(path=out))
            expect("load_ranking ignores a stale file",
                   load_ranking(path=out, max_age_s=-1) == [])
            rep = run_tournament(
                [{"recipe_id": "ref", "url": fast.url}, {"recipe_id": "b", "url": slow.url}],
                reference="ref", model="not-served", prompts=prompts[:2], runs=1,
                max_tokens=8, bench_max_tokens=8, conn=conn, out_path=out)
            expect("unjudged reference -> exit 2 and an empty ranking on disk",
                   rep["exit_code"] == EXIT_UNJUDGED and rep["ranking"] == []
                   and load_ranking(model="not-served", path=out) == [],
                   (rep["exit_code"], rep["ranking"]))
    finally:
        for e in (fast, slow, cheat):
            e.close()

    if verbose:
        summary = f"FAILED {failures}" if failures else "all checks held"
        print(f"self-test: {summary}", file=sys.stderr)
    return EXIT_VIOLATION if failures else EXIT_OK
