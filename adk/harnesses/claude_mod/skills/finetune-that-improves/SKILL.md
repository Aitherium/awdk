---
name: finetune-that-improves
description: >-
  Train your own model without making it worse. Most fine-tunes lose to the model they started from, and you cannot tell from the score. This is what we learned doing it wrong seven times in a row.
---

# finetune-that-improves — train your own model without making it worse

Most fine-tunes lose to the model they started from, and you cannot tell from
the score. This is what we learned doing it wrong seven times in a row.

Every number below was measured on one 8B orchestrator against a 45-item,
13-dimension benchmark. The base scored **0.8540**.

| run | corpus | overall | vs base |
|---|---|---|---|
| base | — | **0.8540** | — |
| v12 | 100% retrieval recall | 0.7479 | −0.1061 |
| v13 | 8 dimensions, answers only | 0.7914 | −0.0626 |
| v14 | 8 dimensions, with derivations | 0.7946 | −0.0594 |
| v15 | authored, all capabilities | 0.6729 | −0.1811 |
| one-capability expert | a single dimension | 0.3936 | −0.4604 |

Not one of them was a bad training run. Every one converged, saved an adapter,
and produced a model that answered fluently.

## 1. A corpus unteaches what it omits

This is the single most useful thing we measured. Train nine experts, each on
one capability and silent on the other twelve:

```
9 experts, 77 damaged dimensions in total
76 of the 77 landed INSIDE the corpus's silence   ->  98.7%
every expert lost overall: −0.18 to −0.58
```

Silence is not neutral. A capability your corpus never mentions gets trained
toward whatever register your corpus *does* teach. You are not "adding a skill";
you are reshaping everything, and only the thing you wrote about gets a vote.

**The sharpest result is the exception.** One expert damaged a dimension it was
*not* silent on — its own, the one it was built to teach — while losing eight
others. So a disappointing run does not automatically mean "needs more data".

**Corollary that saves the most time: check what the base already scores before
designing a corpus at all.** Ours was at 1.0000 on 9 of 13 dimensions. There was
almost nothing to gain and everything to lose, and most of what we called
improvements were trades.

## 2. The fix: your prompts, its answers

Anchor every capability you are **not** teaching with the base model's **own
correct answers**.

- An answer the model already produces has near-zero loss, therefore near-zero
  gradient. It **holds** that capability in place while your new rows pull.
- **Never author the keep-set.** An answer you wrote teaches your register just
  as hard for a capability you are trying to preserve as for one you are trying
  to add. Authoring the keep-set is what caused the −0.1811 collapse.
- Rejection-sample to correct answers only. Keeping the model's mistakes
  reinforces them — the one way an anchor actively harms.
- Anchors are keyed to a specific base. One model's answers are not another
  model's anchors.

## 3. Two failure modes that score identically

**Collapse.** The adapter falls into one register and answers everything from
it. Asked a *workflow* question: "Crystallise now, rollback later." Asked a
*long-horizon* question: "Crystallise now, ship later." Eleven of thirteen
dimensions led with the same word.

The bench recorded `0.0000` on two dimensions. Those zeros read as *bad at this
capability*, and the obvious response — more data for the dimensions that scored
zero — is exactly wrong. The model was not weak. It had stopped answering the
question.

**Damage without collapse.** A healthy spread of answers, and still eight
dimensions lost. No recipe change fixes this one; it is §1, and the fix is §2.

The split is lopsided — **one collapse to eight damage-without-collapse** — so
the corpus is almost always the answer and the recipe almost never is. Diagnose
before you tune, because the collapse fix is the more satisfying-looking of the
two and it changes nothing on the other eight.

## 4. The recipe that collapses, and the one that does not

| | collapsed | use |
|---|---|---|
| rank | 32 | **16** |
| learning rate | 1e-4 | **2e-5** |
| epochs | 2 | **1** |
| loss | full text | **completion only** |

Full-text loss over ~1,200 rows with repeated prompt shapes teaches the *prompt
distribution* too. That is half of how a narrow corpus collapses a model.

**Check that your trainer is actually masking the prompt, by name.** The flag is
spelled differently everywhere — `completion_only_loss` and `assistant_only_loss`
in TRL depending on version, `train_on_responses_only` in Unsloth and in
soup-cli. Searching for the wrong spelling tells you a trainer cannot mask when
it can; assuming it masks when it does not costs you a run you cannot diagnose
afterwards.

Watch for the silent downgrade. At least one trainer falls back to full-text
loss with a single warning line when the tokenizer has no chat template. The
run then looks completely normal: loss curve fine, adapter saved, a number at
the end. Verify by running the trainer's own formatter over a few real rows and
checking the labels contain both masked (`-100`) and unmasked positions.

## 5. Measurement rules, each of which cost us a run

- **"Could not judge" must leave the denominator.** Empty responses, truncation
  at `max_tokens`, unclosed think blocks, transport errors — none of these is a
  0.0. Scoring them produced four confidently wrong findings, including one
  0.4115 with four dimensions at exactly 0.0000.
- **A transport failure is not an unreadable answer.** 440 errored requests were
  reported as "the model answered nothing readable" because the error text had
  been discarded. Two states needing opposite fixes rendered as one number.
- **Derive the dimension list from the items**, never hand-list it. A hardcoded
  8-tuple silently filtered out 10 new items while reporting `judged 35/35`.
- **Compare on the same item set.** A 35-item baseline cannot be compared to a
  45-item run.
- **Know your minimum detectable effect**: `1 / (items in smallest dimension ×
  dimensions)`. Ours was 0.0385 at 35 items, 0.0769 at 45. A "win" smaller than
  your MDE is not a win — this retracted one of our own claimed improvements.
- **A held-out score of exactly 1.0000 is a leakage tell.** Ten of eleven router
  experts scored 1.0000 because the classes were separable by vocabulary we had
  written ourselves. The only honest number came from real traffic.
- **An aggregate hides a bucket.** A router reported 0.9583 overall while one
  class routed at 0.5417. Gate per class, never on the mean.

## 6. Operational traps

- **Never wrap a training job in `timeout`.** A `timeout 599` killed a two-hour
  run at ten minutes. Exit 143 is SIGTERM, not a training failure.
- **Never pipe a long job through `tail`.** It buffers until EOF, so eleven dead
  benchmark runs printed as eleven blank lines and exited 0.
- **Never filter subprocess output on success-words.** If nothing matches, print
  the return code, stdout and stderr. A filter that only knows success turns
  every failure into silence.
- **Use absolute tool paths.** A repo-relative path under a changed working
  directory made every subprocess die instantly on "can't open file".
- **Save the adapter.** A trainer that calls `.train()` without
  `save_pretrained` fails at the merge an hour later, against a path that never
  existed.
- **Do not share the GPUs.** Two runs on one box make both results
  unattributable.

## 7. The order that makes this cheap

Damage is predictable from the corpus, so the expensive checks go last:

```
1. score the base            what is there to gain?
2. build anchors             the base's own answers, correct-only
3. gate the corpus           refuse here — it is free
4. snapshot                  before you replace anything
5. train
6. bench on the same items   against your MDE
7. seal + publish            so others can verify what you made
```

Step 3 is the one people skip and the one that pays. Predicting damage from the
corpus alone named three of the four dimensions one run went on to destroy —
before a GPU was rented.

## 8. What is still open

Whether anchoring works is **being measured, not assumed**. The falsifiable
prediction: our generators cover 11 of 13 dimensions, so two cannot be anchored.
If §1 holds, an anchored run should protect the 11 and may still damage exactly
those two.

Reinforcement learning is the fuller version of §2 — the model acts across its
whole surface and is scored, so there is no narrow imitation target to collapse
onto. This is the cheap, supervised approximation of that.
