"""Self-improvement primitives: bootstrap a model's own training signal safely.

An agent that can fine-tune itself needs three things nothing in a training
stack usually provides, and each one exists here because its absence produced a
measurably WORSE model:

    1. a way to build training data that does not damage what already works
    2. a way to tell a COLLAPSED model from a merely weak one
    3. a way to know whether a measured difference means anything at all

These were derived by fine-tuning an 8-billion-parameter orchestrator seven
times against a 13-dimension behavioural benchmark. Every run lost to its own
base. The numbers below are from those runs.

THE LAW: A CORPUS UNTEACHES WHAT IT OMITS.

Nine narrow adapters were measured dimension by dimension — each trained on one
capability and silent on the other twelve:

    9 adapters, 77 damaged dimensions total
    76 of 77 landed INSIDE the corpus's silence  ->  98.7%

Every adapter lost overall (-0.18 to -0.58) and damaged 6 to 11 capabilities.
Silence is not neutral: an omitted capability is trained toward whatever
register the corpus does teach.

The one exception is the sharpest result: a single adapter damaged a capability
that was NOT silent — its own. Trained on effort calibration it got WORSE at
effort calibration (-0.25) while losing eight other capabilities. A corpus can
be mismatched enough to damage the thing it was written to teach.

Collapse — one register answering everything — is the RARE mode: 1 of the 9.
The other 8 were damaged without collapsing. So `detect_collapse` tells you
which of two fixes applies, and most of the time the answer is the corpus.

THE FIX: YOUR PROMPTS, ITS ANSWERS.

Anchor every capability you are NOT teaching with the model's OWN correct
outputs. An answer it already produces has near-zero loss, hence near-zero
gradient: it holds that capability in place while new rows pull. Do NOT author
the keep-set — authored answers teach your phrasing just as hard for a
capability you are preserving as for one you are adding, and authoring the
keep-set is what caused the collapse above.

Everything here is dependency-free and side-effect-free: pure functions over
data you supply, so an agent can run them inside its own loop.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set

__all__ = [
    "harvest_anchors",
    "corpus_silence",
    "detect_collapse",
    "minimum_detectable_effect",
    "CollapseReport",
    "SilenceReport",
]

_WORD = re.compile(r"[a-z0-9']+")

_STOPWORDS = {
    "the", "a", "an", "to", "of", "and", "or", "is", "it", "in", "for", "on",
    "that", "this", "with", "as", "at", "by", "be", "not", "no", "yes", "you",
    "we", "i", "if", "then", "so", "but", "its", "one",
}

#: A single leading word may lead answers in at most this share of capabilities
#: before the model is collapsed rather than weak. Measured: a collapsed adapter
#: sat at 0.85 while the worst NON-collapsed model sat at 0.23.
COLLAPSE_SHARE = 0.50

#: A capability the base already performs at or above this has everything to
#: lose and nothing to gain, so a corpus omitting it is a live risk.
AT_RISK_SCORE = 0.75


class SilenceReport(dict):
    """What a corpus will damage, predicted from the corpus alone."""

    @property
    def at_risk(self) -> List[str]:
        return list(self.get("silent_and_held") or [])

    @property
    def safe(self) -> bool:
        return not self.at_risk


class CollapseReport(dict):
    """Whether a model stopped answering, or merely answered badly."""

    @property
    def collapsed(self) -> bool:
        return bool(self.get("collapsed"))


def harvest_anchors(
    prompts: Iterable[Dict[str, str]],
    generate: Callable[[str, str], str],
    judge: Callable[[Dict[str, str], str], Optional[float]],
    keep_score: float = 1.0,
) -> Dict[str, object]:
    """Generate with the model itself and keep ONLY what judges correct.

    `prompts` are dicts with `capability`, optional `system`, and `user`. Note
    they carry no answer: the answer is the model's job, and that is the entire
    point — see the module docstring.

    `judge` returns a score, or None meaning the output could not be judged
    (empty, truncated, unparseable). An unjudgeable output leaves the
    DENOMINATOR; it is not a zero. A transport failure is counted separately
    again, because "the endpoint is down" and "the model answers unreadably"
    need opposite fixes and reporting them as one number hides both.

    Rejection sampling to correct-only makes this mildly self-improving rather
    than merely neutral: the model trains on its own successes, and its
    mistakes are absent rather than contradicted. Keeping mistakes would
    reinforce them, which is the one way an anchor set can actively cause harm.
    """
    kept: List[dict] = []
    seen = judged = rejected = unjudgeable = errors = 0
    error_samples: List[str] = []

    for row in prompts:
        seen += 1
        try:
            out = generate(row.get("system", ""), row.get("user", ""))
        except Exception as e:  # noqa: BLE001
            errors += 1
            msg = f"{type(e).__name__}: {e}"
            if msg not in error_samples and len(error_samples) < 5:
                error_samples.append(msg)
            continue
        score = judge(row, out)
        if score is None:
            unjudgeable += 1
            continue
        judged += 1
        if score < keep_score:
            rejected += 1
            continue
        msgs = []
        if row.get("system"):
            msgs.append({"role": "system", "content": row["system"]})
        msgs.append({"role": "user", "content": row["user"]})
        msgs.append({"role": "assistant", "content": out})
        kept.append({
            "messages": msgs,
            # KEY NAME IS A CONTRACT. This emitted "capabilities" while every
            # consumer of a training corpus reads "dimensions" — and a missing
            # key does not raise, it reads as a row declaring NOTHING. A
            # coverage gate would then see zero capabilities in the corpus,
            # under-report what is at risk, and pass. That is the silent-no-op
            # shape, in the module written to prevent silent damage.
            #
            # Found by an adversarial audit, not by a test: the self-test was
            # self-contained ("capability" in -> "capabilities" out) and so
            # agreed with itself perfectly while disagreeing with everything
            # downstream.
            "dimensions": [row.get("capability", "")],
            "source": "self_distilled_anchor",
            # the real judged score, never a constant: a constant is a default
            # wearing a measurement's name, and every filter reading it is inert
            "quality_score": float(score),
        })

    return {
        "rows": kept,
        "seen": seen,
        "judged": judged,
        "kept": len(kept),
        "rejected": rejected,
        "unjudgeable": unjudgeable,
        "errors": errors,
        "error_samples": error_samples,
        # None, never 0.0, when nothing could be judged
        "keep_rate": (len(kept) / judged) if judged else None,
    }


def corpus_silence(
    corpus_capabilities: Set[str],
    base_scores: Dict[str, float],
    at_risk_score: float = AT_RISK_SCORE,
) -> SilenceReport:
    """Predict what a corpus will damage, BEFORE spending anything to train it.

    Damage lands where the corpus is silent, so the capabilities at risk are
    exactly those the model already performs well and the corpus never
    mentions. This is the cheapest check in a training stack: it needs no GPU,
    no run, and no model — only the corpus and a prior measurement.
    """
    held = {c for c, s in (base_scores or {}).items() if s >= at_risk_score}
    silent = sorted(held - set(corpus_capabilities or set()))
    return SilenceReport({
        "held_capabilities": sorted(held),
        "corpus_capabilities": sorted(corpus_capabilities or set()),
        "silent_and_held": silent,
        "advice": (
            "anchor these with the model's OWN correct outputs; do not author "
            "answers for them" if silent else
            "the corpus speaks about everything the model already holds"),
    })


def _lead_word(answer: str) -> str:
    s = re.sub(r"<think>.*?</think>", " ", answer or "", flags=re.S | re.I)
    s = re.sub(r"</?think>", " ", s, flags=re.I)
    for tok in _WORD.findall(s.lower()):
        if tok not in _STOPWORDS:
            return tok
    return ""


def detect_collapse(
    answers_by_capability: Dict[str, Sequence[str]],
    share: float = COLLAPSE_SHARE,
    min_capabilities: int = 4,
) -> CollapseReport:
    """Distinguish a COLLAPSED model from a weak one. They score identically.

    A collapsed model has fallen into one register and answers everything from
    it. Its scores look like ordinary failures — a collapsed answer is
    well-formed, confident, and on-topic for some OTHER question — so a score
    table cannot tell you that the model stopped answering the question rather
    than answering it badly. The two need opposite fixes: collapse is a
    training-recipe problem (rank, learning rate, epochs, completion-only
    loss); weakness is a data problem.

    Pass the model's ANSWERS, not its raw output. Reasoning preambles are
    formulaic — a model that opens every deliberation with the same word will
    read as maximally collapsed while being perfectly healthy.
    """
    by_lead: Dict[str, Set[str]] = {}
    caps: Set[str] = set()
    answered = 0
    for cap, answers in (answers_by_capability or {}).items():
        caps.add(cap)
        for a in answers or []:
            if not (a or "").strip():
                continue
            answered += 1
            lead = _lead_word(a)
            if lead:
                by_lead.setdefault(lead, set()).add(cap)

    if not answered or len(caps) < min_capabilities:
        return CollapseReport({
            "collapsed": None,
            "reason": (
                f"cannot judge: {answered} answer(s) across {len(caps)} "
                f"capabilit(ies); spread is not meaningful below "
                f"{min_capabilities}"),
        })

    lead, spread = max(by_lead.items(), key=lambda kv: len(kv[1]),
                       default=("", set()))
    observed = len(spread) / len(caps)
    return CollapseReport({
        "collapsed": observed >= share,
        "lead_word": lead,
        "capabilities_led": len(spread),
        "capabilities_total": len(caps),
        "share": observed,
        "reason": (
            f"the single word {lead!r} leads answers in {len(spread)}/"
            f"{len(caps)} capabilities ({observed:.0%}); its low scores mean it "
            f"stopped answering the question, so fix the training recipe, not "
            f"the data for the capabilities that scored badly"
            if observed >= share else
            f"most-common lead {lead!r} spans {len(spread)}/{len(caps)} "
            f"({observed:.0%}) — answers vary by capability"),
    })


def minimum_detectable_effect(items_in_smallest: int, n_capabilities: int) -> float:
    """The smallest overall change a single flipped item can produce.

    A reported improvement below this is not an improvement, it is one item
    changing its mind. Stating it turns "we went up 0.018" into a question with
    an answer — and it retracted a real claimed win on the runs behind this
    module.
    """
    if items_in_smallest <= 0 or n_capabilities <= 0:
        return 0.0
    return 1.0 / items_in_smallest / n_capabilities


def self_test() -> int:
    bad = 0

    def ck(cond, what):
        nonlocal bad
        print(f"  {'ok  ' if cond else 'FAIL'} {what}")
        if not cond:
            bad += 1

    ps = [{"capability": c, "system": "", "user": f"q about {c}"}
          for c in ("plan", "route", "verify", "escalate")]

    good = harvest_anchors(ps, lambda s, u: "a real answer", lambda r, x: 1.0)
    ck(good["kept"] == len(ps) and good["keep_rate"] == 1.0,
       "a correct model yields one anchor per prompt")
    ck(all(r["messages"][-1]["content"] == "a real answer" for r in good["rows"]),
       "and the assistant turn is the MODEL's output, never an authored one — "
       "authoring the keep-set is what damages a preserved capability")

    wrong = harvest_anchors(ps, lambda s, u: "bad", lambda r, x: 0.0)
    ck(wrong["kept"] == 0 and wrong["rejected"] == len(ps),
       "incorrect outputs are rejected — training a model on its own mistakes "
       "reinforces them")

    none = harvest_anchors(ps, lambda s, u: "", lambda r, x: None)
    ck(none["keep_rate"] is None and none["errors"] == 0,
       "an unjudgeable output leaves the denominator (keep_rate None, not 0.0) "
       "and is not miscounted as a transport error")

    def boom(s, u):
        raise ConnectionError("refused")

    dead = harvest_anchors(ps, boom, lambda r, x: 1.0)
    ck(dead["errors"] == len(ps) and dead["unjudgeable"] == 0
       and any("refused" in m for m in dead["error_samples"]),
       "a transport failure is counted separately AND keeps its message — it "
       "is the only thing separating a dead endpoint from an unreadable model")

    sil = corpus_silence({"plan"}, {"plan": 0.9, "route": 0.95, "verify": 0.4})
    ck(sil.at_risk == ["route"],
       "corpus_silence names a held capability the corpus omits, and does NOT "
       "name the weak one the corpus legitimately exists to improve")
    ck(corpus_silence({"plan", "route"}, {"plan": 0.9, "route": 0.95}).safe,
       "and a corpus covering everything held is safe")

    collapsed = detect_collapse({
        "plan": ["Crystallise now, then plan."],
        "route": ["Crystallise now, then route."],
        "verify": ["Crystallise now, then verify."],
        "escalate": ["Crystallise now, then escalate."]})
    ck(collapsed.collapsed and collapsed["lead_word"] == "crystallise",
       "detect_collapse names a model that answers every capability from one "
       "register — the failure a score table renders as ordinary wrongness")

    healthy = detect_collapse({
        "plan": ["Decompose into stages."],
        "route": ["Send it to the reasoning tier."],
        "verify": ["Run it and read the traceback."],
        "escalate": ["Ask the owner; this is irreversible."]})
    ck(healthy.collapsed is False,
       "and a model answering each capability in its own register is not "
       "flagged — a rule firing on any repetition would flag good models too")

    ck(detect_collapse({"plan": ["x"]})["collapsed"] is None,
       "too few capabilities returns None (cannot judge), never False — "
       "'I could not look' is not 'nothing is wrong'")

    ck(all("dimensions" in r for r in good["rows"]),
       "anchor rows carry the `dimensions` key every corpus consumer reads — a "
       "differently-named key does not raise, it reads as a row declaring "
       "NOTHING, so a coverage gate sees an empty corpus and passes")
    ck(all(r["dimensions"] and r["dimensions"][0] for r in good["rows"]),
       "and the capability is actually IN it, so coverage is computable")

    ck(abs(minimum_detectable_effect(5, 13) - 1 / 5 / 13) < 1e-9,
       "minimum_detectable_effect is 1/(items in smallest)/(capabilities)")
    ck(minimum_detectable_effect(0, 13) == 0.0,
       "and degenerate input does not raise")

    print(f"\nself-test: {'PASSED' if not bad else 'FAILED'} ({bad} failure(s))")
    return 1 if bad else 0


if __name__ == "__main__":
    import sys
    raise SystemExit(self_test() if "--self-test" in sys.argv else 0)
