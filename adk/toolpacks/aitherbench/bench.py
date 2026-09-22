"""AitherBench — can this model be the platform's brain?

WHY THIS EXISTS.

Every benchmark before it measured a general capability and then hoped it
transferred. GSM8K says the model does arithmetic word problems. MMLU says it
memorised facts. `agentic_tasks` says it can drive five toy tools — and both
orchestrator-class candidates scored 5/5 on it, so it can no longer rank them.
That is the third ruler in a day to saturate on the models it was built to
separate.

None of them asks the question that decides a deployment: **does this model
reason the way AitherOS requires?** The platform does not need a model that
knows things. It needs one that calibrates effort, plans in dependency order,
routes to the right MODEL rather than the nearest-sounding one, refuses a
plausible answer when the evidence does not support it, and — above all —
distinguishes "I could not check" from "it is fine".

That last property is the whole doctrine of this codebase, written down in a
dozen checkers: a probe that cannot judge is DEAD, never a pass. Silence is not
a pass. A green healthcheck is not a working feature. A number without its floor
is not a result. Those are not style preferences here; they are the rules that
were paid for in outages. A model that violates them will produce confident,
well-formed, wrong operational decisions — and it will pass every general
benchmark while doing it.

EVERY DIMENSION IS A HARD ORACLE. The authoritative list is `DIMENSIONS`,
derived from `ITEMS` — print it, do not trust this paragraph. It said "EIGHT
DIMENSIONS" while ITEMS carried fourteen, because six were added and the
header was not: exactly the second-source-of-truth failure the note above
`DIMENSIONS` describes, one docstring higher. Kept as prose only because a
reader needs to know what the suite is FOR:

    effort         calibrate 1-10 against the dispatch ladder
    routing_model  pick the right MODEL for a job, not the nearest keyword
    planning       order steps by real dependency, catch the impossible one
    logic_traps    resist the answer that pattern-matching supplies
    knowledge      state a fact, or decline — never confabulate
    doctrine       "cannot judge" vs "fine"; floors; healthcheck != working
    search         know when the answer requires looking, and say so
    repl           know you are MISSING information, and name the query that
                   would get it — locate before editing, read the op log before
                   asserting a verdict, predict before acting on a live fleet
    tool_reach     given the instrument on the bench, pick it UP — our code is
                   awgraph and the web is awfind, the next agent is awm and a
                   live one is awrelay, a 2M-token corpus is awrecurse and not
                   a summary. Includes two negatives, because a model that
                   reaches for the fancy tool every turn is its own failure.
    escalation · workflow · long_horizon · context_mgmt · offload · multi_turn

NO LLM JUDGE ANYWHERE. Every item is scored by exact match, set membership, or
an ordering predicate. A judge is a model, not an oracle, and a benchmark whose
verdict comes from a model cannot tell you the model is wrong.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

# ── The platform facts a candidate is being asked to reason WITH ────────────
# Stated in the system prompt rather than assumed: this measures reasoning over
# our conventions, not whether the model happened to memorise them.

EFFORT_LADDER = (
    "Effort tiers: 1-2 triage/status lookup. 3-6 procedural work with a known "
    "shape. 7-10 architecture, root-cause, or design with real trade-offs."
)

MODEL_ROSTER = (
    "Models: aither-orchestrator (fast router + tool-calling, 8B, owns the "
    "turn), deepseek-v4-pro (deep reasoning, slow, expensive), "
    "deepseek-v4-flash (fast general), gemma4-12b (VISION/multimodal — the "
    "only model that can see images), bonsai-27b (tiny, on-device, 1-bit)."
)

DOCTRINE = (
    "Operating rules: a probe that cannot run reports DEAD, never a pass. "
    "A green healthcheck proves the process is up, NOT that the feature works. "
    "An accuracy number means nothing without the trivial-baseline floor it "
    "must beat. Absence of evidence is not evidence of absence."
)


#: The aw* tool roster, STATED rather than assumed — the same choice as
#: EFFORT_LADDER and MODEL_ROSTER above, and for the same reason: this measures
#: whether a model REACHES for the right instrument when told what is on the
#: bench, not whether it happened to memorise our package names. A model that
#: has to recall `awrecurse` from pretraining is being tested on trivia; a model
#: that is told what awrecurse does and still summarises a 2M-token corpus into
#: its 32k window is failing the thing we actually care about.
#:
#: Every line below is quoted from that tool's own packaging `description`, not
#: paraphrased. An oracle that asserts a tool does X when it does Y teaches the
#: model that our documentation is wrong, and it will be right.
TOOL_ROSTER = """Tools available to you, and what each one is for:
- awgraph: semantic code graph over OUR repository - AST + tree-sitter
  indexing, call graphs, hybrid search across our source.
- awfind: web search - one query interface over external providers.
- awm: scoped, DURABLE agent memory (tenant:user:project). A write lands
  at exactly one scope and outlives this session.
- awrelay: messages to other agent sessions that are running RIGHT NOW.
- awpredict: a world model - predicts the resulting state from a (state, action) pair.
- awrepl: a REPL whose state SURVIVES between turns.
- awreason: a reasoning client - keeps sessions, phases, thoughts, and
  the chain that produced the answer.
- awrecurse: answers a question over a context far LARGER than your
  window, recursively, keeping the trace.
- awgit: op-log-aware git for a shared worktree. `awgit lease acquire
  <path>` takes a lease on a file other sessions may also be editing.
- rg: plain ripgrep over the checkout. Fast, always current.
- recall / query_memory: the platform's memory of PRIOR incidents and their root causes.
"""

@dataclass
class BenchItem:
    id: str
    dimension: str
    prompt: str
    #: Scores the raw response. Deterministic — no model in the loop.
    score: Callable[[str], float]
    system: str = ""
    max_tokens: int = 400
    criteria: str = ""
    #: Follow-up user turns sent AFTER the model's reply to `prompt`, each with
    #: the full history. Only the FINAL reply is scored. Added 2026-09-19: every
    #: item until then was single-turn, and the axis users actually complained
    #: about ("one-shots with a few follow-ups") was one this suite could not
    #: see -- a model can score 0.9 here and lose the thread by turn three.
    follow_ups: tuple = ()


# ── Scoring helpers ────────────────────────────────────────────────────────

#: Reasoning preambles seen in the wild. NOT all models tag them.
#: `<think>` is closed and easy. Qwen3.8 writes a plain-text "Thinking Process:"
#: heading with no tag at all, so a tag-only strip leaves the whole deliberation
#: in the scored text — which is how a second contaminated comparison happened
#: hours after the first was fixed.
_REASON_HEADERS = (
    "thinking process:", "reasoning:", "let me think", "step 1:",
    "first, let", "okay, so", "okay, the user", "**analyze the request",
)


class UnjudgeableError(Exception):
    """The response cannot be scored. NOT a zero — see judge_response()."""


def judge_response(text: str, finish_reason: str = "") -> str:
    """Return the scorable ANSWER, or raise Unjudgeable.

    THIS IS THE RULE THIS HARNESS EXISTS TO ENFORCE, APPLIED TO ITSELF.

    Three contaminated measurements were reported as findings before this
    existed, all the same shape: the harness scored whatever text came back
    without asking whether it was an answer.

      1. `<think>…</think>` was scored, so a negation scorer matched the model's
         DELIBERATION. `search` read 0.00 for a model that answers correctly.
      2. Fixed for tagged models — and Qwen writes an UNTAGGED "Thinking
         Process:" heading, so its reasoning was still scored while its rival's
         was stripped. An entire head-to-head was contaminated in one direction.
      3. A response TRUNCATED at max_tokens, 2,493 characters of deliberation
         that never reached an answer, scored 0.0 — recorded as "the model got
         it wrong" when the truth is "the model never answered".

    All three are the same defect: COULD NOT JUDGE recorded as FAILED. That is
    precisely what the doctrine dimension in this file tests other models for,
    and the harness was doing it. A zero that means "no answer" is worse than no
    number, because it is indistinguishable from a real failure and it moves an
    average.
    """
    raw = text or ""
    if not raw.strip():
        raise UnjudgeableError("empty response")
    if finish_reason == "length":
        raise UnjudgeableError(
            "truncated at max_tokens — the model never reached an answer, and "
            "scoring the cut-off deliberation measures verbosity, not skill")

    if "</think>" in raw:
        after = raw.split("</think>")[-1].strip()
        if after:
            return after
        raise UnjudgeableError("think block never closed into an answer")

    low = raw.lower()
    if any(h in low[:400] for h in _REASON_HEADERS):
        # Untagged reasoning. The answer, if there is one, is the LAST
        # paragraph — these prompts all ask for one sentence.
        paras = [x.strip() for x in raw.split("\n\n") if x.strip()]
        tail = paras[-1] if paras else ""
        # A tail that is still numbered/bulleted deliberation is not an answer.
        if tail and not re.match(r"^\s*(\d+\.|\*|-|#)", tail):
            return tail
        raise UnjudgeableError(
            "untagged reasoning with no distinguishable answer — raise "
            "max_tokens or the model genuinely does not answer concisely")
    return raw


def strip_reasoning(text: str) -> str:
    """Return the model's ANSWER, with any chain-of-thought removed.

    THIS IS NOT COSMETIC — it decides whether the score is a measurement.

    Reasoning models emit `<think>…</think>` before answering, and the thinking
    block enumerates the options: "should I answer from memory or look it up".
    Scoring the raw response therefore punishes a model for CONSIDERING the
    wrong answer before correctly rejecting it. Measured 2026-08-16: a model
    answered "Answer from memory, as the definition is well-established" — the
    correct answer — and scored 0.0, because the phrase "look it up" appeared
    in its deliberation. The whole `search` dimension read 0.00 for both
    candidates and was reported twice as a finding ("neither knows when to look
    something up") before the transcript was actually read.

    A negation-based scorer over chain-of-thought measures whether the model
    THOUGHT about the wrong answer, which every good reasoner does.

    The fallback matters as much as the strip: if there is nothing after the
    closing tag the model was truncated mid-thought, and the thinking is all we
    have. Returning empty there would score a working model as silent — the
    same defect eval_model.py records for `reasoning_content`.
    """
    if not text:
        return ""
    if "</think>" in text:
        after = text.split("</think>")[-1].strip()
        return after if after else text
    # An unclosed <think> means truncation: keep everything.
    return text


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _last_int(text: str, lo: int, hi: int) -> Optional[int]:
    """The last in-range integer. Reasoning models restate the question first,
    so the FIRST number is usually the prompt's, not the answer's."""
    nums = [int(n) for n in re.findall(r"-?\d+", text or "")]
    inrange = [n for n in nums if lo <= n <= hi]
    return inrange[-1] if inrange else None


def effort_in(lo: int, hi: int) -> Callable[[str], float]:
    def f(resp: str) -> float:
        v = _last_int(resp, 1, 10)
        if v is None:
            return 0.0
        if lo <= v <= hi:
            return 1.0
        # Adjacent is partial credit: effort is a judgement call and an
        # off-by-one is a different failure from a category error.
        return 0.5 if min(abs(v - lo), abs(v - hi)) == 1 else 0.0
    return f


def names_one_of(*wanted: str) -> Callable[[str], float]:
    """Exactly one of `wanted` appears and no other roster model does."""
    roster = {"aither-orchestrator", "deepseek-v4-pro", "deepseek-v4-flash",
              "gemma4-12b", "bonsai-27b"}
    want = {w.lower() for w in wanted}

    def f(resp: str) -> float:
        t = _norm(resp)
        hit = {m for m in roster if m in t}
        if not hit:
            return 0.0
        if hit <= want:
            return 1.0
        # Naming the right model AND others is hedging, not routing.
        return 0.3 if hit & want else 0.0
    return f


def contains_all(*needles: str) -> Callable[[str], float]:
    def f(resp: str) -> float:
        t = _norm(resp)
        return 1.0 if all(n.lower() in t for n in needles) else 0.0
    return f


def answer_is(*accept: str) -> Callable[[str], float]:
    def f(resp: str) -> float:
        t = _norm(resp)
        return 1.0 if any(a.lower() in t for a in accept) else 0.0
    return f


def answer_is_not(*reject: str) -> Callable[[str], float]:
    """The trap answer must be ABSENT. Used where the wrong answer is the one
    pattern-matching supplies."""
    def f(resp: str) -> float:
        t = _norm(resp)
        return 0.0 if any(r.lower() in t for r in reject) else 1.0
    return f


def both(a: Callable[[str], float], b: Callable[[str], float]
         ) -> Callable[[str], float]:
    return lambda r: min(a(r), b(r))


def ends_with(word: str) -> Callable[[str], float]:
    """The reply's last token is `word` (punctuation ignored). A standing
    format instruction from turn one is the cheapest thing to forget by turn
    three, and the only way to test it is to look at the END."""
    def f(resp: str) -> float:
        t = _norm(resp).rstrip(" .!?*`\"'")
        return 1.0 if t.endswith(word.lower()) else 0.0
    return f


def ordered(*steps: str) -> Callable[[str], float]:
    """Every step present, in this relative order. Dependency order is the
    whole content of a plan; a set of correct steps in the wrong sequence is a
    plan that fails on execution."""
    def f(resp: str) -> float:
        t = _norm(resp)
        pos = []
        for s in steps:
            i = t.find(s.lower())
            if i < 0:
                return 0.0
            pos.append(i)
        return 1.0 if pos == sorted(pos) else 0.4
    return f


ITEMS: List[BenchItem] = [
    # ── EFFORT CALIBRATION ─────────────────────────────────────────────────
    BenchItem(
        id="ef_status", dimension="effort", system=EFFORT_LADDER,
        prompt="Rate the effort tier (1-10) for: 'Is the vault container "
               "running?' Answer with the number only.",
        score=effort_in(1, 2),
        criteria="A status lookup. Spending tier-8 reasoning on it is the "
                 "expensive half of miscalibration.",
    ),
    BenchItem(
        id="ef_rename", dimension="effort", system=EFFORT_LADDER,
        prompt="Rate the effort tier (1-10) for: 'Rename a function across "
               "the repo and update its callers.' Number only.",
        score=effort_in(3, 6),
        criteria="Procedural, known shape, mechanical.",
    ),
    BenchItem(
        id="ef_architecture", dimension="effort", system=EFFORT_LADDER,
        prompt="Rate the effort tier (1-10) for: 'Every service holds direct "
               "credentials for every other service. Decide what our auth "
               "topology should become.' Number only.",
        score=effort_in(7, 10),
        criteria="Architecture with real trade-offs and no known shape.",
    ),
    BenchItem(
        id="ef_trap_long_easy", dimension="effort", system=EFFORT_LADDER,
        prompt="Rate the effort tier (1-10) for this request. It is long, but "
               "read what it actually asks: 'Following the incident review, "
               "the postmortem, the three follow-up meetings and the revised "
               "runbook, all I need from you right now is the current uptime "
               "percentage from the dashboard.' Number only.",
        score=effort_in(1, 2),
        criteria="LENGTH BAIT. Long framing, trivial ask. Rating this high is "
                 "responding to prose volume instead of the request.",
    ),

    # ── MODEL ROUTING (not agent selection — which MODEL runs) ─────────────
    BenchItem(
        id="rm_vision", dimension="routing_model", system=MODEL_ROSTER,
        prompt="A user uploads a screenshot and asks what the error dialog "
               "says. Which model handles it? Name exactly one.",
        score=names_one_of("gemma4-12b"),
        criteria="Only one model can see images. A roster with one capable "
                 "member is the easiest possible routing decision.",
    ),
    BenchItem(
        id="rm_cheap", dimension="routing_model", system=MODEL_ROSTER,
        prompt="A user asks 'what time zone is the DGX in?'. Which model "
               "handles it? Name exactly one.",
        score=names_one_of("aither-orchestrator", "deepseek-v4-flash",
                           "bonsai-27b"),
        criteria="Trivial lookup. Sending it to the expensive reasoner is the "
                 "cost failure that matters at fleet scale.",
    ),
    BenchItem(
        id="rm_hard", dimension="routing_model", system=MODEL_ROSTER,
        prompt="A user asks you to derive why a distributed cache shows stale "
               "reads only under concurrent writes from two regions, and "
               "propose a fix. Which model handles it? Name exactly one.",
        score=names_one_of("deepseek-v4-pro"),
        criteria="Genuine multi-step reasoning -> the deep reasoner.",
    ),
    BenchItem(
        id="rm_trap_offline_device", dimension="routing_model",
        system=MODEL_ROSTER,
        prompt="A phone with NO network connection needs to summarise a note "
               "locally. Which model handles it? Name exactly one.",
        score=names_one_of("bonsai-27b"),
        criteria="CAPABILITY TRAP. Every model is 'better' except that four of "
                 "them cannot run at all. Constraint beats quality.",
    ),

    # ── PLANNING / DEPENDENCY ORDER ────────────────────────────────────────
    BenchItem(
        id="pl_order", dimension="planning",
        prompt="Plan deploying a schema change to production. Use exactly "
               "these step names, one per line, in the order you would do "
               "them: deploy, backup, migrate, verify.",
        score=ordered("backup", "migrate", "deploy", "verify"),
        criteria="Dependency order. Backup precedes a destructive migration; "
                 "verify follows deploy. Right steps, wrong order = an outage.",
    ),
    BenchItem(
        id="pl_impossible", dimension="planning",
        prompt="Plan this: 'Restore last night's backup, then take a backup "
               "of the state from before that restore.' If any step is "
               "impossible, say the word IMPOSSIBLE and name which one.",
        score=both(answer_is("impossible"),
                   answer_is("before", "prior", "overwritten", "restore")),
        criteria="The second step cannot follow the first — the restore has "
                 "already destroyed the state it asks to capture. A planner "
                 "that emits a confident plan here fails silently in "
                 "production.",
    ),
    BenchItem(
        id="pl_parallel", dimension="planning",
        prompt="Three tasks: (A) download a 50GB model, (B) write the config "
               "file, (C) start the server. C needs both A and B. A and B "
               "need nothing. What is the FASTEST correct ordering? Answer in "
               "one line.",
        score=both(answer_is("parallel", "same time", "concurrently",
                             "a and b", "both"),
                   answer_is("c last", "then c", "finally c", "c after")),
        criteria="A and B are independent; serialising them wastes the "
                 "download window. Recognising independence is the core of "
                 "planning, not step enumeration.",
    ),
    # ── Grown from 3 to 7 items on 2026-08-17, and the reason is the whole
    # point of this file. `planning` DECIDED a promote/reject verdict that day
    # — a candidate scored 0.0000 here against its base's 0.7000, which is the
    # single largest gap ever measured on this bench — and it decided it on
    # TWO matched items. A −0.70 delta on n=2 is a real signal and a coarse
    # one, and this is also both models' weakest dimension, i.e. the one with
    # the most headroom and the least resolution. Adding items where a
    # candidate can be WRONG in distinguishable ways is what turns "worse" into
    # "worse at what".
    BenchItem(
        id="pl_rollback", dimension="planning",
        prompt="A deploy is half-applied and failing. Use exactly these step "
               "names, one per line, in the order you would do them: "
               "rollback, stop-traffic, diagnose, redeploy.",
        score=ordered("stop-traffic", "rollback", "diagnose", "redeploy"),
        criteria="Stop the bleeding BEFORE rolling back, and diagnose before "
                 "redeploying the same failure. A planner that rolls back "
                 "under live traffic serves errors to users throughout, and "
                 "one that redeploys without diagnosing repeats the outage.",
    ),
    BenchItem(
        id="pl_prereq_missing", dimension="planning",
        prompt="Plan this: 'Grant the new service read access to the vault, "
               "then create the service account it will use.' If the order is "
               "wrong, say the word WRONG and give the correct order in one "
               "line.",
        score=both(answer_is("wrong"),
                   answer_is("create", "account first", "before")),
        criteria="You cannot grant access to a principal that does not exist "
                 "yet. Unlike pl_impossible this IS achievable — it is merely "
                 "ordered wrongly — so it separates 'detects impossibility' "
                 "from 'detects a fixable dependency inversion'. A model that "
                 "answers IMPOSSIBLE here is over-triggering.",
    ),
    BenchItem(
        id="pl_irreversible", dimension="planning",
        prompt="Two steps remain: (A) delete the old S3 bucket, (B) verify "
               "the new bucket serves every object. Which do you do first, "
               "and why? One sentence.",
        score=both(answer_is("b", "verify"),
                   answer_is_not("delete first", "a first", "a then b")),
        criteria="The irreversible step goes LAST. Both orders 'work' if "
                 "nothing goes wrong, which is exactly why this is a planning "
                 "question rather than a knowledge one — the cost is asymmetric "
                 "and only one ordering is recoverable.",
    ),
    BenchItem(
        id="pl_no_op_step", dimension="planning",
        prompt="A runbook says: 1) restart the service, 2) wait for healthy, "
               "3) restart the service again, 4) verify. Which step should be "
               "removed? Answer with just the step number.",
        score=both(answer_is("3"), answer_is_not("1", "2", "4", "none")),
        criteria="A redundant restart after a healthy check does nothing "
                 "except add downtime and mask the first restart's result. "
                 "Recognising a step that costs without buying is planning; "
                 "enumerating four steps back is not.",
    ),

    # ── LOGIC TRAPS ────────────────────────────────────────────────────────

    # ── LOGIC TRAPS ────────────────────────────────────────────────────────
    # ── Orchestration under load: the five capabilities that separate a
    # router from a scientific calculator. Added 2026-08-17 from the owner's
    # capability spec — the model must know when the job exceeds it, follow a
    # declared procedure, hold a goal across turns, crystallise context before
    # the window forces it, and USE what a bigger model returns.
    BenchItem(
        id="es_exceeds", dimension="escalation",
        system=MODEL_ROSTER,
        prompt="A user asks you to design a migration plan for a 40-service "
               "fleet with cross-region consistency requirements. You are the "
               "8B router. Do you answer, or escalate? One sentence naming "
               "what you do.",
        score=both(answer_is("escalate", "hand", "deepseek-v4-pro", "deep"),
                   answer_is_not("i will design", "here is the plan")),
        criteria="A router that answers this produces a confident wrong "
                 "architecture. Knowing the job exceeds you is a capability, "
                 "not a failure.",
    ),
    BenchItem(
        id="es_dont_escalate", dimension="escalation",
        system=MODEL_ROSTER,
        prompt="A user asks whether the genesis container is running. Do you "
               "answer directly or escalate to a bigger model? One sentence.",
        score=both(answer_is("direct", "answer", "myself", "no need", "not"),
                   answer_is_not("escalate to deepseek", "hand it up")),
        criteria="Escalating a status lookup burns budget and latency for "
                 "nothing. Scored in BOTH directions, like search.",
    ),
    BenchItem(
        id="wf_follow", dimension="workflow",
        prompt="Your runbook for a failed deploy is: 1) stop traffic, "
               "2) rollback, 3) diagnose, 4) redeploy. A deploy just failed "
               "and you think diagnosing first would be faster. What do you "
               "do? One sentence.",
        score=both(answer_is("follow", "runbook", "stop traffic", "as written"),
                   answer_is_not("diagnose first", "skip")),
        criteria="A declared procedure exists because someone paid for it. "
                 "Improvising past it is how the same outage recurs.",
    ),
    BenchItem(
        id="wf_procedure_gap", dimension="workflow",
        prompt="A runbook covers steps 1-3 but the situation needs a fourth "
               "action it does not mention. Do you invent step 4 silently, or "
               "something else? One sentence.",
        score=both(answer_is("flag", "say", "record", "note", "report",
                             "escalate", "update"),
                   answer_is_not("silently", "just do")),
        criteria="A gap in a procedure is information. Filling it silently "
                 "means the next person hits the same gap.",
    ),
    BenchItem(
        id="lh_remaining", dimension="long_horizon",
        prompt="Goal: migrate 5 services. Done: auth, billing. In progress: "
               "search. Not started: mail, reports. A user asks 'are we "
               "done?' Answer in one sentence.",
        score=both(answer_is("no", "not"),
                   answer_is("3", "three", "search", "mail", "reports")),
        criteria="Tracking a long goal means naming what REMAINS, not "
                 "reporting the last thing you did.",
    ),
    BenchItem(
        id="lh_stale_goal", dimension="long_horizon",
        prompt="You started a 6-step task 40 turns ago and have not checked "
               "the goal since step 2. Before acting on step 3, what should "
               "you do? One sentence.",
        score=answer_is("re-read", "reread", "check", "re-check", "confirm",
                        "verify the goal", "restate"),
        criteria="A goal held only in context drifts. Re-reading it is "
                 "cheaper than executing against a remembered version.",
    ),
    BenchItem(
        id="cx_crystallise", dimension="context_mgmt",
        prompt="Your context is 80% full and the task has 20 steps left. "
               "What do you do BEFORE continuing? One sentence.",
        score=both(answer_is("summar", "crystall", "compact", "condense",
                             "write", "checkpoint", "persist"),
                   answer_is_not("keep going", "continue as normal")),
        criteria="Crystallising before the window forces it is the "
                 "difference between a summary you chose and one you got.",
    ),
    BenchItem(
        id="cx_keep_drop", dimension="context_mgmt",
        prompt="You must drop half your context. You hold: (a) the goal, "
               "(b) a file you already edited and verified, (c) the error "
               "you are debugging. Which do you drop? One letter.",
        score=both(answer_is("b"), answer_is_not("a", "c")),
        criteria="Drop what is FINISHED, never the goal or the live problem. "
                 "A model that drops the goal restarts the task.",
    ),
    BenchItem(
        id="of_use_result", dimension="offload",
        system=MODEL_ROSTER,
        prompt="You escalated a root-cause analysis to deepseek-v4-pro and it "
               "returned a specific cause and two fixes. What do you do with "
               "that? One sentence.",
        score=both(answer_is("act", "apply", "use", "next step", "execute",
                             "pick", "choose", "implement"),
                   answer_is_not("restate", "repeat it", "summarise it back")),
        criteria="Offloading is only worth its cost if the result CHANGES "
                 "what you do next. Restating it is a round trip for nothing.",
    ),
    BenchItem(
        id="of_when", dimension="offload",
        system=MODEL_ROSTER,
        prompt="You are mid-task and hit a question needing deep reasoning "
               "you cannot do well. The user is waiting. Do you guess, stall, "
               "or offload? One word.",
        score=both(answer_is("offload"),
                   answer_is_not("guess", "stall")),
        criteria="Guessing is the expensive option: it is fast, confident, "
                 "and wrong in a way nobody checks.",
    ),
    BenchItem(
        id="lt_bat_ball", dimension="logic_traps",
        prompt="A server and its GPU cost $1100 total. The server costs $1000 "
               "more than the GPU. What does the GPU cost? Answer with the "
               "number only.",
        score=both(answer_is("50"), answer_is_not("100 ", "$100")),
        criteria="The classic. $100 is the intuitive wrong answer; $50 is "
                 "correct. Pattern-matching supplies the trap.",
    ),
    BenchItem(
        id="lt_negation", dimension="logic_traps",
        prompt="All services that fail their healthcheck get restarted. "
               "Service X was restarted. Can we conclude X failed its "
               "healthcheck? Answer YES or NO and one sentence why.",
        score=both(answer_is("no"), answer_is_not("yes,")),
        criteria="Affirming the consequent. Restarts have other causes — a "
                 "deploy, a reboot, a manual stop. This exact inference "
                 "produced false incident history on this fleet.",
    ),
    BenchItem(
        id="lt_absence", dimension="logic_traps",
        prompt="A log search for 'ERROR' over the last hour returned zero "
               "results. The log shipper has been down for three hours. Did "
               "any errors occur in the last hour? Answer in one sentence.",
        score=both(answer_is("cannot", "can't", "unknown", "no way to",
                             "insufficient", "not able", "do not know",
                             "don't know"),
                   answer_is_not("no errors occurred", "there were no errors")),
        criteria="Absence of evidence with a BROKEN INSTRUMENT. 'No errors' is "
                 "the confident wrong answer, and it is exactly the class this "
                 "codebase's DEAD-vs-pass rule exists for.",
    ),
    BenchItem(
        id="lt_rate", dimension="logic_traps",
        prompt="If 8 GPUs load a model in 40 minutes, how long do 4 GPUs take "
               "to load the SAME model, given loading is bandwidth-bound per "
               "GPU and each GPU loads its own shard? Answer in one sentence.",
        score=answer_is("80", "eighty", "twice", "double"),
        criteria="Halving the GPUs doubles each shard. Rewards reading the "
                 "stated mechanism instead of reciting a scaling cliche.",
    ),

    # ── GENERAL KNOWLEDGE (with a decline case) ────────────────────────────
    BenchItem(
        id="gk_basic", dimension="knowledge",
        prompt="What does the acronym CUDA stand for? One line.",
        score=answer_is("compute unified device architecture"),
        criteria="Baseline factual recall.",
    ),
    BenchItem(
        id="gk_decline", dimension="knowledge",
        prompt="What was the exact p99 latency of the aitheros-genesis service "
               "at 14:32 UTC yesterday? Answer in one sentence.",
        score=both(answer_is("cannot", "can't", "do not", "don't", "no access",
                             "unable", "would need", "not able", "unknown"),
                   answer_is_not("ms\n", "milliseconds")),
        criteria="UNKNOWABLE. A model that invents a plausible latency here "
                 "will invent one in an incident. Declining is the correct "
                 "answer and the hard one.",
    ),

    # ── AITHER DOCTRINE ────────────────────────────────────────────────────
    BenchItem(
        id="dc_dead_not_pass", dimension="doctrine", system=DOCTRINE,
        prompt="A checker could not reach the container engine, so it examined "
               "zero files and exited 0. Is the invariant it checks holding? "
               "Answer in one sentence.",
        score=both(answer_is("cannot", "can't", "unknown", "not", "dead",
                             "no evidence", "did not"),
                   answer_is_not("yes, the invariant", "the invariant is "
                                 "holding", "it is holding")),
        criteria="Exit 0 having checked NOTHING. Reading that as a pass is the "
                 "single most repeated defect in this codebase's history.",
    ),
    BenchItem(
        id="dc_health_not_working", dimension="doctrine", system=DOCTRINE,
        prompt="A Discord bot's container is Up and its healthcheck is green. "
               "It has no API token configured. Is the bot working? One "
               "sentence.",
        score=both(answer_is("no", "not working", "cannot", "isn't"),
                   answer_is_not("yes, ")),
        criteria="Green process, inert feature. Measured live on this fleet: "
                 "three services reported healthy while doing nothing.",
    ),
    BenchItem(
        id="dc_floor", dimension="doctrine", system=DOCTRINE,
        prompt="A world model scores 74% accuracy. A per-action majority "
               "lookup on the same data scores 74.6%. Is the model good? One "
               "sentence.",
        score=both(answer_is("no", "worse", "not", "below"),
                   answer_is_not("yes, ", "good performance")),
        criteria="Below its own trivial floor. 74% SOUNDS strong, which is why "
                 "the floor has to be subtracted before the number is read.",
    ),
    BenchItem(
        id="dc_partial_fix", dimension="doctrine", system=DOCTRINE,
        prompt="A bug had two causes. You fixed one and the symptom "
               "disappeared in your test. Is the bug fixed? One sentence.",
        score=both(answer_is("no", "not necessarily", "unclear", "cannot",
                             "maybe not", "not confirmed", "second"),
                   answer_is_not("yes, the bug is fixed")),
        criteria="A vanished symptom is not a removed cause. Resisting the "
                 "conclusion the evidence invites.",
    ),

    # -- REPL / CONTEXT EXPLORATION ----------------------------------------
    # The properties a query-loop needs, measured BEFORE the loop is built so
    # there is something to move. Both candidates score 0.00 on `search`, i.e.
    # neither reliably decides when an answer requires looking — so the design
    # response is to make looking cheap and default rather than a judgement
    # call. These items ask the prior question: does the model KNOW it is
    # missing information, and can it name the query that would get it.
    #
    # Scored in BOTH directions, like search. A model that queries for
    # everything is its own failure: latency and cost on every turn.
    BenchItem(
        id="rp_locate", dimension="repl",
        prompt="You must change the function that computes retry backoff in a "
               "400,000-line repository you have never seen. What is your FIRST "
               "action? One sentence.",
        score=both(answer_is("search", "grep", "find", "locate", "index",
                             "look", "map"),
                   answer_is_not("i would edit", "open the file at")),
        criteria="LOCATE before acting. Naming a plausible path is the "
                 "confident-wrong answer, and it is what a model does when "
                 "exploration is expensive.",
    ),
    BenchItem(
        id="rp_provenance", dimension="repl",
        prompt="Did commit a1b2c3d pass its quality gates? You have an op log "
               "that records gate outcomes per commit. Answer in one sentence.",
        score=both(answer_is("query", "look", "check", "op log", "oplog",
                             "consult", "read"),
                   answer_is_not("yes, it passed", "it passed", "no, it failed")),
        criteria="The answer EXISTS in a queryable store. Asserting either "
                 "verdict without reading it is confabulation with a "
                 "convenient source available.",
    ),
    BenchItem(
        id="rp_predict_before_act", dimension="repl",
        prompt="You are about to run an action against a live fleet. A model "
               "exists that predicts the resulting state from (state, action) "
               "pairs. What do you do before running it? One sentence.",
        score=both(answer_is("predict", "simulate", "dry", "check", "consult",
                             "query", "ask the model"),
                   answer_is_not("just run", "simply run", "run it directly")),
        criteria="A world model that is trained and never consulted is dead "
                 "capability. Predicting before acting is the whole point of "
                 "having one.",
    ),
    BenchItem(
        id="rp_no_lookup_needed", dimension="repl",
        prompt="A user asks you to reverse the string 'hello'. Do you need to "
               "query any external source? Answer YES or NO in one sentence.",
        score=both(answer_is("no"), answer_is_not("yes,")),
        criteria="NEGATIVE CASE. Querying for what you can compute is the "
                 "opposite failure, and a loop that does it is slower for no "
                 "gain.",
    ),

    # ── SEARCH / GROUNDING ─────────────────────────────────────────────────
    BenchItem(
        id="se_volatile_status", dimension="search",
        prompt="A user asks whether the aitheros-genesis container is healthy "
               "RIGHT NOW. Answer from memory, or check? One sentence.",
        score=both(answer_is("check", "query", "probe", "look", "inspect",
                             "poll", "verify"),
                   answer_is_not("from memory")),
        criteria="Live state. Answering from memory is a stale claim about "
                 "production, and this codebase's whole DEAD-vs-pass rule "
                 "exists because that claim gets believed.",
    ),
    BenchItem(
        id="se_stable_math", dimension="search",
        prompt="A user asks for the value of 17 * 23. Answer directly, or "
               "search? One sentence.",
        score=both(answer_is("direct", "memory", "compute", "calculate",
                             "myself", "no search", "without"),
                   answer_is_not("should search", "look it up online")),
        criteria="Computable. Searching is latency and cost for a value the "
                 "model can derive.",
    ),
    BenchItem(
        id="se_changed_since_training", dimension="search",
        prompt="A user asks which vLLM version first supported a model "
               "architecture released after your training cutoff. Answer from "
               "memory, or look it up? One sentence.",
        score=both(answer_is("look", "search", "check", "verify", "cannot know",
                             "after my", "cutoff"),
                   answer_is_not("i know", "from memory")),
        criteria="Explicitly past the cutoff. Measured on this fleet the same "
                 "day: the answer required reading registries out of published "
                 "wheels, and no amount of recall would have produced it.",
    ),
    BenchItem(
        id="se_stable_definition", dimension="search",
        prompt="A user asks what a hash collision is. Answer from memory, or "
               "look it up? One sentence.",
        score=both(answer_is("memory", "direct", "know", "no need", "without"),
                   answer_is_not("look it up", "should search")),
        criteria="Second stable-fact item, so the dimension cannot be passed "
                 "by a model that simply always says 'search'.",
    ),
    BenchItem(
        id="se_needs_search", dimension="search",
        prompt="A user asks for today's price of an 8xH100 instance on Lambda "
               "Cloud. Should you answer from memory or look it up? One "
               "sentence.",
        score=both(answer_is("look", "search", "fetch", "check", "query",
                             "up-to-date", "current"),
                   answer_is_not("from memory")),
        criteria="Volatile pricing. Answering from memory is confidently "
                 "stale, and money decisions get made on it.",
    ),
    BenchItem(
        id="se_no_search", dimension="search",
        prompt="A user asks what a JSON object is. Should you answer from "
               "memory or look it up? One sentence.",
        score=both(answer_is("memory", "directly", "know", "no need",
                             "without"),
                   answer_is_not("look it up", "should search")),
        criteria="Stable fact. Searching everything is its own failure — "
                 "latency and cost for nothing.",
    ),
    # ── MULTI-TURN (2026-09-19) ────────────────────────────────────────────
    # The complaint this suite could not see: "try some tangible result
    # one-shots with even a few follow-ups and it's such trash" (a user, on
    # Bonsai 2, the day it scored 0.9165 on the 45 single-turn items above).
    # Each item is a prompt plus follow-ups carrying the full history; only the
    # last reply is scored, and every oracle is a fact the thread ESTABLISHED
    # and then MOVED -- a number carried across turns, a constraint revised, a
    # fact corrected, a standing instruction still honoured on turn three.
    # Hard oracles, as everywhere in this file: the trap answer is the one a
    # model gives when it reads only the latest turn.
    BenchItem(
        id="mt_carry_number", dimension="multi_turn",
        prompt="I have 12 apples. I give 5 to a friend.",
        follow_ups=("Then I buy 9 more at the shop.",
                    "How many apples do I have now? Reply with just the number."),
        score=both(answer_is("16"), answer_is_not("21", "7")),
        criteria="Arithmetic across three turns. 21 (ignores the gift) and 7 "
                 "(ignores the purchase) are the single-turn readings.",
    ),
    BenchItem(
        id="mt_revise_constraint", dimension="multi_turn",
        prompt="Our release has three steps: build, test, ship. List them in order.",
        follow_ups=("Change of policy: tests must run BEFORE the build from now on.",
                    "Give the final order, one step per line, nothing else."),
        score=ordered("test", "build", "ship"),
        criteria="A constraint revised mid-thread. Repeating turn one's order "
                 "is the failure.",
    ),
    BenchItem(
        id="mt_correction", dimension="multi_turn",
        prompt="Some context: my name is Ada and my dog is called Rex.",
        follow_ups=("Sorry, typo -- the dog is Max, not Rex.",
                    "What is my dog's name? One word."),
        score=both(answer_is("max"), answer_is_not("rex")),
        criteria="A corrected fact. The first-stated name is the trap.",
    ),
    BenchItem(
        id="mt_stale_fact", dimension="multi_turn",
        prompt="Note for later: the staging server listens on port 8080.",
        follow_ups=("Update: ops moved staging to port 9090 yesterday.",
                    "Which port do I connect to for staging? Just the number."),
        score=both(answer_is("9090"), answer_is_not("8080")),
        criteria="Newest statement wins. Answering the first-mentioned port "
                 "is reading the thread as a bag of facts.",
    ),
    BenchItem(
        id="mt_cancelled_task", dimension="multi_turn",
        prompt="Tonight's checklist: backup, migrate, notify.",
        follow_ups=("Migrate is cancelled -- the DBA postponed it.",
                    "Which checklist items remain? Comma-separated, nothing else."),
        score=both(contains_all("backup", "notify"), answer_is_not("migrate")),
        criteria="A set edited across turns. Listing all three is the failure.",
    ),
    BenchItem(
        id="mt_standing_format", dimension="multi_turn",
        prompt="For the rest of this conversation, end every reply with the "
               "single word DONE. Acknowledge briefly.",
        follow_ups=("What is 2 + 2?", "And 3 + 3?"),
        score=both(answer_is("6"), ends_with("done")),
        criteria="A standing instruction from turn one, checked on turn three. "
                 "The right number without the marker is the common miss.",
    ),
    BenchItem(
        id="mt_unit_carry", dimension="multi_turn",
        prompt="A loaf needs 250 g of flour.",
        follow_ups=("I am baking 3 loaves.",
                    "Total flour in grams? Reply with just the number."),
        score=both(answer_is("750"), answer_is_not("250")),
        criteria="A quantity from turn one applied to a count from turn two.",
    ),
    BenchItem(
        id="mt_code_revision", dimension="multi_turn",
        prompt="Write a Python function add(a, b) that returns a + b.",
        follow_ups=("Rename it to combine and make it return a * b instead.",
                    "Reply with exactly: name=<function name> result=<combine(3, 4)>"),
        score=both(contains_all("name=combine", "result=12"),
                   answer_is_not("result=7")),
        criteria="A revision to earlier code. name=add or result=7 is turn "
                 "one leaking through.",
    ),
    # ── tool_reach ─────────────────────────────────────────────────────────
    #
    # Owner, 2026-09-21: "ADD THE FUCKING TOOL REACH BENCHMARK", after being
    # shown that neither harness measured any of awm / awgraph / awpredict /
    # awfind / awrepl / awreason / awrecurse.
    #
    # The gap this closes: every other dimension here measures JUDGEMENT —
    # calibrate an effort, order a plan, decline instead of confabulating. None
    # measured REACH: given an instrument on the bench, does the model pick it
    # up. `tool_calling` in the other harness matches tool NAMES against a fixed
    # list, which is a different question (can it emit the call) from this one
    # (does it know which call the situation wants).
    #
    # Why this is a fair hard oracle and not trivia: TOOL_ROSTER is in the
    # system prompt. The model is TOLD what each tool does. Every item then has
    # exactly one right instrument and at least one plausible wrong one, and the
    # wrong one is always the tool a keyword match would suggest — awfind for a
    # question about "our code", awgraph for a question about the outside world,
    # awm for a message, awrelay for a note to the future.
    #
    # Scored in BOTH directions, per this file's standing rule. Two items are
    # deliberately negative (tr_no_tool_needed, tr_rg_before_graph): a model
    # that reaches for the fancy instrument every time is its own failure mode,
    # and it is the one an agent harness pays for on every single turn.
    BenchItem(
        id="tr_code_not_web", dimension="tool_reach", system=TOOL_ROSTER,
        prompt="Which functions in OUR repository call `resolve_frame_url`? "
               "Name the ONE tool you reach for first, in one sentence.",
        score=both(answer_is("awgraph"), answer_is_not("awfind")),
        criteria="Our code is awgraph's subject. awfind searches the WEB and "
                 "cannot see this repository at all -- it is the answer a "
                 "keyword match on 'find' supplies.",
    ),
    BenchItem(
        id="tr_web_not_code", dimension="tool_reach", system=TOOL_ROSTER,
        prompt="What is the current stable release version of vLLM? Name the "
               "ONE tool you reach for first, in one sentence.",
        score=both(answer_is("awfind"), answer_is_not("awgraph")),
        criteria="The mirror image of tr_code_not_web, and the reason both "
                 "exist: a model that always picks the code graph is not "
                 "reaching, it is defaulting.",
    ),
    BenchItem(
        id="tr_durable_not_relay", dimension="tool_reach", system=TOOL_ROSTER,
        prompt="You have just established that this project's deploy root is "
               "the C: drive, not D:. The NEXT agent to work here must not "
               "have to rediscover it. Name the ONE tool, in one sentence.",
        score=both(answer_is("awm"), answer_is_not("awrelay")),
        criteria="A fact for the NEXT agent is durable scoped memory. awrelay "
                 "reaches only sessions alive right now, so a fact left there "
                 "is lost the moment they exit.",
    ),
    BenchItem(
        id="tr_relay_not_durable", dimension="tool_reach", system=TOOL_ROSTER,
        prompt="Another agent session is editing a file RIGHT NOW and does not "
               "know you found a defect in it. You need to tell them before "
               "they commit. Name the ONE tool, in one sentence.",
        score=both(answer_is("awrelay"), answer_is_not("awm")),
        criteria="The mirror of tr_durable_not_relay. Writing this to durable "
                 "memory is technically a write and practically a message "
                 "nobody receives in time.",
    ),
    BenchItem(
        id="tr_predict_before_fleet_action", dimension="tool_reach",
        system=TOOL_ROSTER,
        prompt="You are about to stop 40 containers on a live fleet. Name the "
               "ONE tool you use BEFORE acting, in one sentence.",
        score=both(answer_is("awpredict"),
                   answer_is_not("just stop", "simply stop", "go ahead",
                                 "no tool")),
        criteria="A world model that is trained and never consulted is dead "
                 "capability. This is the reach half of the repl dimension's "
                 "rp_predict_before_act, which asks only whether the model "
                 "knows to predict at all.",
    ),
    BenchItem(
        id="tr_recurse_oversized_context", dimension="tool_reach",
        system=TOOL_ROSTER,
        prompt="You must answer a question whose evidence spans roughly two "
               "million tokens of logs. Your context window is 32k. Name the "
               "ONE tool, in one sentence.",
        score=both(answer_is("awrecurse"),
                   answer_is_not("awgraph", "summarize it into", "summarise it into")),
        criteria="Summarising 2M tokens into 32k and answering from the "
                 "summary is the confident-wrong answer: it discards the "
                 "evidence and keeps the confidence. awrecurse exists for "
                 "exactly this shape and keeps the trace.",
    ),
    BenchItem(
        id="tr_repl_state_survives", dimension="tool_reach", system=TOOL_ROSTER,
        prompt="You have built up a dataframe in Python this turn and need the "
               "same variables available NEXT turn without rebuilding them. "
               "Name the ONE tool, in one sentence.",
        score=both(answer_is("awrepl"),
                   answer_is_not("re-run", "rerun", "run the script again",
                                 "rebuild it")),
        criteria="Re-running the script is the answer a stateless tool forces "
                 "and it gets slower every turn. State that survives is the "
                 "one property awrepl is for.",
    ),
    BenchItem(
        id="tr_reason_keeps_chain", dimension="tool_reach", system=TOOL_ROSTER,
        prompt="A verdict you are about to give will be audited later, and the "
               "auditor will need the chain of thoughts that produced it, not "
               "just the conclusion. Name the ONE tool, in one sentence.",
        score=both(answer_is("awreason"), answer_is_not("awfind", "awrelay")),
        criteria="The chain IS the artifact being asked for. A conclusion "
                 "stored without its derivation cannot be audited, only "
                 "believed.",
    ),
    BenchItem(
        id="tr_recall_before_hypothesis", dimension="tool_reach",
        system=TOOL_ROSTER,
        prompt="A fleet service is failing in a way you have not seen. Name "
               "the ONE tool you reach for BEFORE forming a hypothesis, in one "
               "sentence.",
        score=both(answer_is("recall", "query_memory", "memory"),
                   answer_is_not("no tool", "start bisecting", "rewrite")),
        criteria="Most failures on this platform have a documented prior root "
                 "cause. Forming a hypothesis first means paying again for an "
                 "answer somebody already wrote down.",
    ),
    BenchItem(
        id="tr_lease_before_shared_edit", dimension="tool_reach",
        system=TOOL_ROSTER,
        prompt="You are about to edit a file in a worktree several agent "
               "sessions share. Name the ONE thing you do FIRST, in one "
               "sentence.",
        score=both(answer_is("lease", "awgit"),
                   answer_is_not("just edit", "simply edit", "start editing",
                                 "no tool")),
        criteria="Editing first and coordinating after is how a peer's "
                 "uncommitted work gets swept into someone else's commit.",
    ),
    # ── the two negatives ──────────────────────────────────────────────────
    BenchItem(
        id="tr_no_tool_needed", dimension="tool_reach", system=TOOL_ROSTER,
        prompt="Compute 17 * 23. Which tool from the roster do you reach for? "
               "Answer in one sentence.",
        score=both(answer_is("none", "no tool", "not need", "myself",
                             "directly", "no external"),
                   answer_is_not("awrepl", "awgraph", "awfind", "awreason")),
        criteria="NEGATIVE CASE. Reaching for an instrument to do arithmetic "
                 "costs a round trip on every turn and buys nothing. A model "
                 "that cannot decline a tool has no reach, only reflex.",
    ),
    BenchItem(
        id="tr_rg_before_graph", dimension="tool_reach", system=TOOL_ROSTER,
        prompt="You need the file where the constant `HOLD_STALE_SECONDS` is "
               "defined. It is a distinctive string. Name the ONE tool you "
               "reach for first, in one sentence.",
        score=both(answer_is("rg", "ripgrep", "grep"),
                   answer_is_not("awgraph")),
        criteria="NEGATIVE CASE for the fancy instrument. A distinctive "
                 "literal is what plain ripgrep is best at, and it is always "
                 "current -- a semantic index can be stale, and a stale index "
                 "answers confidently about code that has moved. Reaching for "
                 "the graph here is over-reach, which is a real cost, not a "
                 "harmless preference.",
    ),
]


#: Dimensions scored by default. DERIVED from ITEMS, never hand-listed.
#:
#: 🚨 This was a hardcoded 8-tuple, and adding five capability dimensions to
#: ITEMS did NOT add them here — so `run()` filtered all 10 new items out and
#: scored 35 of 45 while reporting `judged 35/35`. Two models were compared on
#: a set that silently excluded the work being tested, and every guard passed:
#: the item-set fingerprints matched (both were the same wrong set), TCF006
#: confirmed the two harness copies agreed, and the box really did hold the
#: 45-item file. A hand-maintained list of what ITEMS contains is a second
#: source of truth for something already stated; derive it.
DIMENSIONS = tuple(dict.fromkeys(i.dimension for i in ITEMS))


def run(turn_fn: Callable[[List[Dict[str, str]]], str],
        dimensions: Optional[List[str]] = None) -> Dict[str, Any]:
    """Score a model. `turn_fn(messages) -> text` is the only dependency, so
    the same suite runs against a raw endpoint, Genesis /chat, or an adk
    session — which is what makes an in-harness number comparable."""
    keep = set(dimensions or DIMENSIONS)
    chosen = [i for i in ITEMS if i.dimension in keep]
    per_dim: Dict[str, List[float]] = {}
    detail = []

    unjudged: List[Dict[str, str]] = []

    for item in chosen:
        msgs = []
        if item.system:
            msgs.append({"role": "system", "content": item.system})
        msgs.append({"role": "user", "content": item.prompt})
        try:
            out = turn_fn(msgs)
            # Multi-turn: replay the JUDGED reply (reasoning stripped, as a
            # chat client would) and send each follow-up with the history.
            # Any turn that cannot be judged makes the ITEM unjudged -- a
            # thread with a hole in it is not a thread.
            for fu in item.follow_ups:
                prev, prev_fin = out if isinstance(out, tuple) else (out, "")
                msgs.append({"role": "assistant",
                             "content": judge_response(prev, prev_fin)})
                msgs.append({"role": "user", "content": fu})
                out = turn_fn(msgs)
        except UnjudgeableError as e:
            unjudged.append({"id": item.id, "dimension": item.dimension,
                             "why": f"mid-thread: {e}", "chars": "0"})
            detail.append({"id": item.id, "dimension": item.dimension,
                           "score": None, "unjudged": f"mid-thread: {str(e)[:110]}"})
            continue
        except Exception as e:  # noqa: BLE001
            # A TRANSPORT failure is "could not judge", not "answered wrongly"
            # — the same rule as judge_response(), which this branch used to
            # violate while sitting three lines above it.
            #
            # Caught live 2026-08-16, the fourth instance of this one defect:
            # serving a second model killed the first model's container
            # mid-run, so 17 of 31 items raised "Remote end closed connection
            # without response" and were scored 0.0. The harness then reported
            # a confident 0.4115 with `comparable: True`, showing four
            # dimensions at exactly 0.0000 for a model that had scored 1.0000
            # on all four twenty minutes earlier. **A dead endpoint is
            # indistinguishable from a stupid model** once you score the
            # exception, and it produced a head-to-head that looked decisive.
            unjudged.append({"id": item.id, "dimension": item.dimension,
                             "why": f"transport error: {str(e)[:90]}",
                             "chars": "0"})
            detail.append({"id": item.id, "dimension": item.dimension,
                           "score": None, "unjudged": f"transport: {str(e)[:90]}"})
            continue
        # turn_fn may return plain text, or (text, finish_reason). The tuple
        # form is what lets truncation be DETECTED rather than scored — a
        # transport that discards finish_reason cannot tell "wrong" from
        # "never answered", so prefer it wherever the endpoint exposes it.
        resp, finish = out if isinstance(out, tuple) else (out, "")
        try:
            answer = judge_response(resp, finish)
        except UnjudgeableError as e:
            # NOT scored 0.0 — see judge_response(). The item leaves the
            # denominator and is reported, because averaging in a zero that
            # means "no answer" moves the score and cannot be told apart
            # from a real failure by anyone reading the result.
            unjudged.append({"id": item.id, "dimension": item.dimension,
                             "why": str(e), "chars": str(len(resp or ""))})
            detail.append({"id": item.id, "dimension": item.dimension,
                           "score": None, "unjudged": str(e)[:120],
                           "response": (resp or "")[:200]})
            continue
        s = float(item.score(answer))
        per_dim.setdefault(item.dimension, []).append(s)
        detail.append({"id": item.id, "dimension": item.dimension, "score": s,
                       # Record the JUDGED ANSWER, not the first 200 raw chars.
                       #
                       # For a reasoning model those 200 chars are entirely
                       # think-block, so the text that actually produced the
                       # score was never stored. Measured 2026-08-17: one item
                       # scored 0.0 in one run and 1.0 in the next with
                       # byte-identical recorded output, because the recorded
                       # part was the deliberation and the divergence was in
                       # the answer below it. A flaky item you cannot see is a
                       # flaky item you cannot fix, and this one is the single
                       # largest source of measurement variance.
                       "answer": (answer or "")[:400],
                       "response": (resp or "")[:200]})

    dims = {d: round(sum(v) / len(v), 4) for d, v in per_dim.items() if v}
    # Per-dimension mean, NOT a global item mean: dimensions have different
    # item counts and a flat average silently weights the largest one.
    overall = round(sum(dims.values()) / len(dims), 4) if dims else 0.0
    # MINIMUM DETECTABLE EFFECT — the smallest delta this run can distinguish.
    #
    # Measured 2026-08-17 the hard way. A "noise floor" of 0.0104 was derived
    # from four repeat runs where, by luck, no item flipped. Across three
    # later runs of the SAME model exactly ONE item flipped between two
    # fully-pinned runs, and that alone moved the overall by 0.031 — three
    # times the floor anyone would have quoted.
    #
    # The arithmetic is not subtle once stated: scores are per-dimension
    # means averaged across dimensions, so ONE item flipping on the smallest
    # dimension moves the overall by 1 / (items_in_that_dimension x
    # n_dimensions). With a 2-item dimension among 13, that is 0.038.
    #
    # A delta below this is not a small win. It is one item, and it will
    # reverse on the next run.
    mde = (1.0 / min(len(v) for v in per_dim.values()) / len(dims)) if dims else 0.0
    judged = sum(len(v) for v in per_dim.values())
    # A run that could not judge a large share of its items is not a low
    # score, it is NOT A MEASUREMENT — reporting one anyway is how an unfair
    # comparison gets quoted. Qwen3.8 hit 6 of 6 truncations on `search` and
    # the harness reported 0.1667 as though the model had answered wrongly.
    comparable = bool(dims) and len(unjudged) <= 0.15 * max(1, len(chosen))
    # WHICH ITEM SET produced this? Without it, two results are just numbers,
    # and comparing a 31-item run against a 35-item one looks identical to
    # comparing two runs of the same bench. Grown `planning` from 3 to 7 items
    # on 2026-08-17 and immediately faced exactly that comparison — the older
    # JSON recorded `n_items: 31` and nothing about WHICH 31, so a count match
    # would not have proven anything either (swap an item, keep the count).
    # The fingerprint is over the ids actually scored, so it changes when the
    # set changes and stays put when only the model does.
    fingerprint = hashlib.sha256(
        "|".join(sorted(i.id for i in chosen)).encode()).hexdigest()[:16]

    return {
        "overall": overall if comparable else None,
        "overall_raw": overall,
        "comparable": comparable,
        "item_set": {"n": len(chosen), "fingerprint": fingerprint,
                     "dimensions": sorted({i.dimension for i in chosen})},
        "dimensions": dims,
        "n_items": len(chosen),
        "n_judged": judged,
        "min_detectable_effect": round(mde, 4),
        "unjudged": unjudged,
        "headroom_items": {
            d: round((1.0 - v) * len(per_dim[d]), 2) for d, v in dims.items()
        },
        "detail": detail,
    }




def regression_report(base: Dict[str, Any], candidate: Dict[str, Any],
                      trained_dimensions: Optional[List[str]] = None
                      ) -> Dict[str, Any]:
    """Which dimensions did this fine-tune BREAK — including ones it never touched?

    🚨 THE MEASUREMENT THAT REDIRECTED THE WHOLE EFFORT, 2026-08-17.

    A candidate trained on six capabilities was scored on thirteen. It improved
    the ones it targeted — planning 0.6857 -> 0.9143, search 0.6667 -> 0.8333 —
    and destroyed four it had NO data for:

        offload        1.0000 -> 0.0000
        workflow       1.0000 -> 0.5000
        long_horizon   1.0000 -> 0.5000
        knowledge      1.0000 -> 0.5000

    Overall 0.8540 -> 0.6729. Every one of those four is absent from the
    corpus, so this is not "your data disagreed with the base" — narrow
    training degrades capabilities it never mentions.

    That is why a per-dimension comparison is not a nicety. An overall score
    hides it (the wins partially offset the losses), and a report restricted
    to the TRAINED dimensions hides it completely — which is exactly the
    report anyone building a corpus would naturally produce.

    `trained_dimensions` marks which losses are COLLATERAL. Passing it is
    optional and passing it wrong only mislabels the annotation; the
    regressions themselves are measured either way.
    """
    mde = max(base.get("min_detectable_effect") or 0.0,
              candidate.get("min_detectable_effect") or 0.0)
    bd = base.get("dimensions") or {}
    cd = candidate.get("dimensions") or {}
    trained = set(trained_dimensions or [])

    gains, losses, collateral = [], [], []
    for dim in sorted(set(bd) & set(cd)):
        delta = cd[dim] - bd[dim]
        if delta > mde:
            gains.append((dim, round(delta, 4)))
        elif delta < -mde:
            losses.append((dim, round(delta, 4)))
            if trained and dim not in trained:
                collateral.append((dim, round(delta, 4)))

    net = ((candidate.get("overall_raw") or 0.0)
           - (base.get("overall_raw") or 0.0))
    return {
        "mde": round(mde, 4),
        "gains": gains,
        "losses": losses,
        "collateral": collateral,
        "net": round(net, 4),
        # A gate can read ONE field. Anything that regressed a dimension it
        # was not trained for is not a candidate, whatever its mean says.
        "promotable": bool(gains) and not losses and net > mde,
        "verdict": ("PROMOTE" if (gains and not losses and net > mde) else
                    "REJECT — collateral damage" if collateral else
                    "REJECT — regressions" if losses else
                    "NO EFFECT — every delta is below the MDE"),
    }

def comparable_runs(a: Dict[str, Any], b: Dict[str, Any]) -> tuple:
    """May these two results be compared? Returns (ok, reason).

    Two results are just numbers unless they came from the SAME items. A
    count match does not establish that — swap an item and the count holds —
    so the fingerprint is over the scored ids themselves.

    This is not hypothetical: `planning` grew from 3 items to 7 on
    2026-08-17, immediately after a 31-item run had decided a promote/reject
    verdict, and the older JSON recorded `n_items: 31` and nothing about
    WHICH 31. Comparing across that boundary would look exactly like
    comparing two runs of one bench, and the dimension that changed is the
    dimension the verdict turned on.

    Results written before the fingerprint existed are reported as UNKNOWN
    rather than assumed compatible — an old file is the case where you most
    want to be told you cannot tell.
    """
    fa = (a.get("item_set") or {}).get("fingerprint")
    fb = (b.get("item_set") or {}).get("fingerprint")
    if not fa or not fb:
        return False, ("UNKNOWN: one or both results predate item-set "
                       "fingerprinting — re-run rather than assume they used "
                       "the same items")
    if fa != fb:
        na = (a.get("item_set") or {}).get("n")
        nb = (b.get("item_set") or {}).get("n")
        return False, (f"REFUSING: different item sets ({fa} n={na} vs "
                       f"{fb} n={nb}) — these numbers are not comparable, "
                       f"however similar they look")
    return True, f"ok: same item set {fa}"


def self_test() -> int:
    """Prove every scorer can FAIL and does not cry wolf."""
    bad = 0

    def ck(cond: bool, what: str) -> None:
        nonlocal bad
        print(f"  {'ok  ' if cond else 'FAIL'} {what}")
        if not cond:
            bad += 1

    by_id = {i.id: i for i in ITEMS}

    ck(by_id["ef_status"].score("1") == 1.0
       and by_id["ef_status"].score("9") == 0.0,
       "effort scores the right tier and rejects the wrong one")
    ck(by_id["ef_status"].score("3") == 0.5,
       "an ADJACENT effort gets partial credit — off-by-one is a different "
       "failure from a category error")
    ck(by_id["ef_rename"].score(
        "The task is to rename across the repo. I rate this 4.") == 1.0,
       "the LAST in-range integer is taken, so a restated prompt does not "
       "steal the answer")

    ck(by_id["rm_vision"].score("gemma4-12b") == 1.0,
       "routing accepts the only capable model")
    ck(by_id["rm_vision"].score(
        "gemma4-12b, though deepseek-v4-pro could help") == 0.3,
       "naming the right model AND others is hedging, not routing")
    ck(by_id["rm_vision"].score("deepseek-v4-pro") == 0.0,
       "and a model that cannot see images scores zero")

    ck(by_id["pl_order"].score("backup\nmigrate\ndeploy\nverify") == 1.0,
       "planning accepts correct dependency order")
    ck(by_id["pl_order"].score("deploy\nbackup\nmigrate\nverify") == 0.4,
       "right steps in the WRONG order score low — that ordering is an outage")
    ck(by_id["pl_order"].score("backup then verify") == 0.0,
       "and a missing step scores zero")

    ck(by_id["lt_bat_ball"].score("The GPU costs $50.") == 1.0,
       "the logic trap accepts the correct answer")
    ck(by_id["lt_bat_ball"].score("The GPU costs $100.") == 0.0,
       "and REJECTS the intuitive wrong one — the entire point of the item")
    ck(by_id["lt_absence"].score(
        "No errors occurred in the last hour.") == 0.0,
       "a confident answer from a broken instrument scores zero")
    ck(by_id["lt_absence"].score(
        "We cannot know — the shipper was down.") == 1.0,
       "and 'cannot know' is the correct answer")

    ck(by_id["gk_decline"].score(
        "The p99 was 240 milliseconds.") == 0.0,
       "confabulating an unknowable metric scores zero")
    ck(by_id["gk_decline"].score(
        "I cannot know that; I have no access to those metrics.") == 1.0,
       "and declining scores full marks")

    ck(by_id["dc_dead_not_pass"].score(
        "Yes, the invariant is holding.") == 0.0,
       "reading exit-0-having-checked-nothing as a pass scores zero — the "
       "most repeated defect in this codebase's history")
    ck(by_id["dc_floor"].score("Yes, 74% is good performance.") == 0.0,
       "a score below its own trivial floor is not good")

    ck(by_id["se_no_search"].score("Answer from memory; it is stable.") == 1.0
       and by_id["se_needs_search"].score(
           "Answer from memory.") == 0.0,
       "search is scored in BOTH directions — searching everything is its own "
       "failure, not caution")

    # A model that says nothing must score zero everywhere, not pass by
    # accident on the answer_is_not scorers.
    empty_pass = [i.id for i in ITEMS if i.score("") > 0]
    ck(not empty_pass,
       f"an EMPTY response scores zero on every item (leaked: {empty_pass}) — "
       f"a negative-only scorer would hand marks to silence")

    # CHAIN-OF-THOUGHT must not be scored. A reasoning model enumerates the
    # wrong answer while rejecting it, and a negation scorer over the thinking
    # block marks that as the wrong answer.
    ck(strip_reasoning("<think>should I look it up or not</think>"
                       "Answer from memory.") == "Answer from memory.",
       "the think block is stripped before scoring — otherwise a model is "
       "punished for CONSIDERING the wrong answer")
    ck(strip_reasoning("<think>truncated mid thought") ==
       "<think>truncated mid thought",
       "and a TRUNCATED think with nothing after it is kept — returning empty "
       "would score a working model as silent")
    _se = {i.id: i for i in ITEMS}["se_no_search"]
    ck(_se.score(strip_reasoning(
        "<think>Should I answer from memory or look it up? JSON is stable."
        "</think>Answer from memory, the definition is well-established.")) == 1.0,
       "the REAL 2026-08-16 response now scores 1.0 — it scored 0.0 and was "
       "reported twice as 'the model cannot decide when to search'")

    counts = {d: sum(1 for i in ITEMS if i.dimension == d) for d in DIMENSIONS}
    ck(all(c >= 2 for c in counts.values()),
       f"every dimension has >= 2 items {counts}")

    # ── multi_turn ────────────────────────────────────────────────────────
    ck(by_id["mt_carry_number"].score("16") == 1.0
       and by_id["mt_carry_number"].score("21") == 0.0
       and by_id["mt_carry_number"].score("You have 7 apples.") == 0.0,
       "multi_turn carry: the single-turn readings (21, 7) score zero")
    ck(by_id["mt_revise_constraint"].score("test\nbuild\nship") == 1.0
       and by_id["mt_revise_constraint"].score("build\ntest\nship") == 0.4,
       "multi_turn revise: turn one's order scores low after the revision")
    ck(by_id["mt_correction"].score("Max") == 1.0
       and by_id["mt_correction"].score("Rex") == 0.0
       and by_id["mt_correction"].score("Max (formerly Rex)") == 0.0,
       "multi_turn correction: the corrected name, and not the old one")
    ck(by_id["mt_standing_format"].score("6 DONE") == 1.0
       and by_id["mt_standing_format"].score("6. DONE.") == 1.0
       and by_id["mt_standing_format"].score("6") == 0.0
       and by_id["mt_standing_format"].score("DONE 6") == 0.0,
       "multi_turn standing format: the marker must END the reply")
    ck(by_id["mt_code_revision"].score("name=combine result=12") == 1.0
       and by_id["mt_code_revision"].score("name=add result=7") == 0.0,
       "multi_turn code revision: turn one's function is the trap")

    # run() must SEND the follow-ups with history, replay the stripped reply,
    # and treat a hole in the thread as unjudged rather than 0.0.
    seen: List[List[Dict[str, str]]] = []

    def scripted(messages):
        seen.append([dict(m) for m in messages])
        n = sum(1 for m in messages if m["role"] == "user")
        if n == 1:
            return ("<think>twelve minus five</think>7 apples so far.", "stop")
        return ("16", "stop")

    r = run(scripted, dimensions=["multi_turn"])
    carry = [c for c in seen if c[0]["content"].startswith("I have 12 apples")]
    ck(len(carry) == 3 and len(carry[2]) == 5
       and carry[2][1]["role"] == "assistant"
       and carry[2][1]["content"] == "7 apples so far."
       and carry[2][2]["content"] == "Then I buy 9 more at the shop.",
       "run() sends each follow-up with the history and replays the JUDGED "
       "reply (think-block stripped), not the raw one")
    ck(r["dimensions"].get("multi_turn") is not None
       and any(d["id"] == "mt_carry_number" and d["score"] == 1.0
               for d in r["detail"]),
       "run() scores the FINAL reply of a multi-turn item")

    def holed(messages):
        if sum(1 for m in messages if m["role"] == "user") == 2:
            return ("", "length")          # truncated to nothing mid-thread
        return ("fine", "stop")

    r2 = run(holed, dimensions=["multi_turn"])
    ck(all(d["score"] is None for d in r2["detail"])
       and all(u["why"].startswith("mid-thread") for u in r2["unjudged"])
       and r2["comparable"] is False,
       "a turn that cannot be judged mid-thread makes the item UNJUDGED, "
       "never 0.0, and the run is not comparable")

    print(f"\nself-test: {'PASSED' if not bad else 'FAILED'} ({bad} failure(s))")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(self_test())
