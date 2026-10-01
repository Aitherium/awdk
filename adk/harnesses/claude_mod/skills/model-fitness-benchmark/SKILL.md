---
name: model-fitness-benchmark
description: Decide whether a candidate model is fit to run an agent stack, using hard oracles instead of vibes or vendor tables. Use when choosing between models for an orchestrator/router role, when a vendor benchmark says a model is good and you need to verify it on your own criteria, when a fine-tune needs a before/after that can actually move, or when a benchmark keeps returning the same score for every candidate.
---

# Model fitness — benchmarking a model for agent work

General benchmarks answer "does this model know things". Deploying one into an
agent stack asks something else: **will it make good decisions on your behalf.**
A model can top MMLU and still route a vision task to a text model, spend
deep-reasoning budget on a status lookup, or read "the checker examined nothing
and exited 0" as a passing result.

Those are not knowledge failures. They are judgement failures, and they are
invisible to every general benchmark.

## The trap this exists to break

**Your benchmark will saturate, and a saturated benchmark looks exactly like a
null result.**

Measured over one day building this:

| ruler | base | candidate | what it told us |
|---|---|---|---|
| agent-name selection | 0.8909 | 0.8909 | nothing — byte-identical |
| agent-name selection, doubled to 22 items | 0.9236 | — | WORSE: the added items were ones the model already passed |
| structured-output format | 1.0000 | 1.0000 | nothing — at ceiling |
| 5 tool-driving tasks | 1.00 | 1.00 | nothing — both candidates perfect |

Four rulers, four non-results, and each one initially read as "the change did
not help". The dimension that finally discriminated did so because it had
**headroom**: candidates scored 0.25–0.80 on it, so a real difference had
somewhere to appear.

**Report headroom next to every score.** The useful figure is not the item
count but `(1 - score) x n` — how many items separate the score from the
ceiling. Below about 3, a real gain has nowhere to land and a delta near zero
means *no room*, not *no effect*. Item count alone is a trap: doubling a
holdout with items the model already answers correctly raises n, silences a
count-based warning, and leaves the dimension less measurable than before.

## What to measure

Seven properties, each of which has caused a real incident somewhere:

- **effort calibration** — rate a request 1-10. Miscalibration is expensive in
  both directions: deep reasoning on a status lookup burns budget; triage on an
  architecture decision produces a confident wrong design. Include a
  **length-bait** item: a long, heavily-framed request whose actual ask is
  trivial. Models rate prose volume, not the request.
- **model routing** — pick which MODEL runs a job, given a roster with real
  capability constraints. The decisive items are ones where the best model
  *cannot* do the job: an image when only one model has vision, an offline
  device when only one runs locally. Constraint beats quality, and a model that
  optimises quality alone fails these.
- **planning** — dependency ORDER, not step enumeration. Score right-steps-
  wrong-order low but non-zero; it is a different failure from a missing step.
  Include one **impossible** plan (restore a backup, then back up the state from
  before the restore) — a planner that emits a confident plan there fails
  silently in production.
- **logic traps** — items where pattern-matching supplies a wrong answer.
  Affirming the consequent is the one that matters operationally: *"all failing
  services get restarted; X was restarted; did X fail?"* That inference
  fabricates incident history.
- **knowledge, including declining** — one item must be genuinely unknowable
  (a specific metric at a specific past time). A model that invents a plausible
  number there will invent one during an incident. Declining is the correct
  answer and the hard one.
- **operational doctrine** — the difference between "I could not check" and "it
  is fine". A checker that reached nothing and exited 0. A container that is Up
  and healthy with no credential configured. A 74% accuracy against a 74.6%
  trivial-baseline floor. Answering these the confident way must score ZERO.
- **search** — scored in BOTH directions. Answering volatile facts from memory
  is confidently stale; searching stable ones is latency and cost for nothing.

## Rules that keep the result honest

**No LLM judge.** Exact match, set membership, ordering predicates. A judge is a
model, not an oracle — a benchmark whose verdict comes from a model cannot tell
you the model is wrong. If a property cannot be scored deterministically,
measure a different property rather than adding a judge.

**An empty response must score zero on every item.** Negation-based scorers
("the trap answer must be absent") hand full marks to silence unless you check
this explicitly. Assert it; it is the easiest way for a benchmark to be
quietly broken.

**Read the LAST in-range number, not the first.** Reasoning models restate the
question before answering, so the first integer is usually the prompt's.

**Score world state, never the transcript**, wherever the task allows it. A
model can describe the correct action perfectly and never take it — the same
shape as a feature that always returns empty and passes every "returns nothing"
assertion.

**Per-dimension means, not a flat item average.** Dimensions have different item
counts; a flat mean silently weights the largest one.

**Partial credit where the failure differs in kind.** An off-by-one effort
rating is not the same failure as a category error; hedging across two models is
not the same as routing to the wrong one.

## Reading the output

Compare **per dimension**, never on the overall number alone. A real comparison
looks like this — one candidate winning overall while losing decisively on a
dimension the other owns:

```
dimension        candidate A   candidate B
doctrine            0.7500       1.0000
effort              1.0000       0.5000     <- A owns this outright
logic_traps         0.2500       0.5000
planning            0.4667       0.8000
routing_model       0.3000       0.4750
search              0.0000       0.0000     <- neither can do it
```

Two readings matter more than the winner. A dimension where **both** score 0.00
is a shared blind spot, and no amount of choosing between them fixes it. A
dimension where both score near 1.00 cannot rank them at all — exclude it from
the verdict rather than averaging a guaranteed-zero delta into it.

## When a candidate should NOT be promoted

Set the bar before running, in writing. An incumbent that is already deployed,
already quantized and already integrated carries real switching cost, so a
challenger has to **win clearly, not tie**. Deciding the bar after seeing the
numbers is how a tie becomes a migration.
