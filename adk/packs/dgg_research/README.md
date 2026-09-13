# dgg-research

A research agent for a record that has to survive a hostile reader.

It works an evidence base the way the people who build one actually work: resolve a person to an actor before asserting anything about them, quote verbatim before interpreting, cite the artifact every claim came from, and record what you could *not* find as carefully as what you could.

```bash
pip install "awdk[memory]"
python -m adk.toolpacks.dgg_research.onboard   # reads this machine, picks the brain, says which
adk chat                                       # the five tools are already registered
```

There is no `adk dgg-research` subcommand, on purpose. The tool-pack loader
already scans `adk/toolpacks/`, so the tools are on the agent without one — and
a verb the runtime cannot answer reads as the pack being broken rather than as
the command being fictional.

## It runs on your hardware, not ours

There is no hosted-model assumption anywhere in this pack.

| tier | when | what |
|---|---|---|
| your own endpoint | you set `AITHER_INFERENCE_URL` or `OPENAI_BASE_URL` | wins over detection — you're paying for it and you know why |
| GPU | ≥ 13 GB usable VRAM | llama.cpp + Bonsai-27B |
| CPU | anything else | llama.cpp + Bonsai-4B-Q1 |

`onboard` reads the host and prints the tier with the reason it chose it. A GPU it cannot query is reported as **unknown**, never as absent — those two lead to opposite correct actions, and telling you that you have no GPU when the query merely failed sends you to fix the wrong thing.

It never falls back to a hosted API. If nothing local loads and you declared no endpoint, it stops and says so. A private corpus quietly leaving the building because the local model would not load is the one failure here that cannot be walked back.

## The five tools

| | |
|---|---|
| `actor_resolve` | Is this person already in the record? **Never writes.** Returns every candidate with a score and the margin over the runner-up. |
| `actor_references` | Every frame an actor appears in — as actor, push actor, or target — plus the evidence linked to them. |
| `evidence_push` | An action, against the artifact it came from. Refuses without one. |
| `mention_verify` | Carries a **person's** confirmation of a proposed match. Never makes one. |
| `corpus_null` | A null, with the corpus and window that bound it, and the control query that proves the instrument worked. |

## Why `park` is a good answer

`actor_resolve` returns candidates, plural, with the margin over the runner-up — because a high score with a close runner-up is the **worst** case, not the best. Two plausible actors scoring alike is exactly when nothing should be decided automatically.

When the agent gets `park` or `propose` back, that is a correct outcome and not a retry signal. Re-sending with a different spelling until something links converts a careful refusal into a fuzzy match by brute force, which is the failure the whole design exists to prevent.

**The bar is yours to set.** Which role may auto-link a fuzzy match, at what score, with what margin, lives in the server's `ResolutionPolicy` table — one active row per role, editable in the admin, no migration and no deploy. The agent asks; it does not decide. A maintainer who knows the actor list and a first-week volunteer pasting a name off a screenshot are not doing the same thing, and one global switch has to be wrong for one of them.

## What it will not do

- Claim an AI byline anywhere — commit, PR, issue, or file. This overrides tool defaults that add one automatically.
- Commit, push, open a PR, or create a repo without being asked for that specific action.
- Delete archived material. Corrections go in as corrections.
- Invent an actor to make a write succeed.
- Present a search result as a census.

## Configuration

| variable | |
|---|---|
| `DGG_API_BASE` | the record's API (default `http://localhost:8000`) |
| `DGG_ACTOR_ROLE` | selects a **policy**, never a permission — it grants nothing |
| `DGG_ACTOR_USER` | who is acting, for the audit trail |
| `AITHER_INFERENCE_URL` | your own model endpoint, if you have one |
