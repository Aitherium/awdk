---
name: adk-deepseek-coder
description: >-
  Fill-in-the-middle, repo packing, and a reward that can fail. DeepSeek-Coder is two useful things at once, and most people only take the first.
---

# adk-deepseek-coder — fill-in-the-middle, repo packing, and a reward that can fail

DeepSeek-Coder is two useful things at once, and most people only take the first.

The obvious half is the **model**: strong at code, and uniquely good at
*fill-in-the-middle* — writing the code BETWEEN two fragments rather than continuing
after one. The half nearly everyone leaves behind is the **evaluation harness**, which
is the only part that can tell you whether any of it is working.

Both ship MIT (the code; the weights carry a separate model licence that does permit
commercial use).

## Fill-in-the-middle

```python
from adk.packs.deepseek_coder import dsc_infill, dsc_repo_context, dsc_traps, dsc_models

await dsc_infill(prefix="def quicksort(arr):\n    ", suffix="\n    return arr")
```

FIM is a different operation from chat completion, not a prompt style. Ask a chat
model to "fill the gap" and it rewrites your surrounding lines — which is exactly why
inline editor completion never worked well with one. FIM writes only the hole.

**Call `dsc_traps()` before you drive the model directly.** Every way to misformat a
FIM prompt produces a fluent, confident, wrong answer with nothing logged:

- the sentinels are **U+FF5C and U+2581**, not the ASCII `|` and `_` they look like;
- the suffix goes *after* the hole marker, not before it;
- an instruct model needs the raw-completion stop token or it halts at the first turn
  boundary — and then reads as a weak model rather than a misconfigured one.

Every one of those failures looks like "this model isn't very good".

## Repo packing

```python
dsc_repo_context(root="./src")     # dependency-first, with #path markers
```

This implements the paper's repo-level packing: partition the dependency graph into
disconnected subgraphs, then order by `argmin(in_degree)`. The `argmin` is what makes
the ordering total on a **cyclic** import graph instead of stalling — real codebases
have cycles, and a naive topological sort simply stops. Cycles are reported, never
silently broken.

Why bother: a model reading a file whose dependencies it has already seen behaves
very differently from one reading the same file cold. Order is signal.

## The half people skip: a reward that can fail

DeepSeek-Coder's repo is ~94% evaluation harness. It runs a candidate completion
against its unit tests in a separate process, in a temp directory, under resource
limits, and reports `passed` / `failed: <exc>` / `timed out`. With it you can compute
**pass@k** — the unbiased estimator, `1 - C(n-c,k)/C(n,k)`.

That distinction matters more than it looks. Averaging `c/n` across problems
systematically understates a model that is right *sometimes*, which is precisely the
signal a training loop is trying to see. pass@1 is the only k where the two agree.

**If your eval does not execute the code, you do not have pass@k — you have a string
match wearing its name.** A benchmark that reports `metric="pass@1"` while scoring
with a regex will look stable and mean nothing, and a self-improvement loop pointed at
it will optimise the regex with great enthusiasm.

### Running it safely

The harness executes **model-generated code**. Its own guard function disables a list
of destructive callables inside the child process, and upstream is explicit that this
is *not* a security sandbox — it does not contain a determined escape and does not
block the network.

So:

- Run it inside a container with **no host mount and no network**, or in the ADK's
  `sandbox` harness (see `adk-harnesses`). Not on your working tree.
- Make the unsafe path **refuse by default** and require an explicit containment
  claim from the caller. A refusal must *raise*: if it returns "nothing passed", that
  is indistinguishable from a model that got everything wrong, and it will silently
  zero your reward signal for as long as nobody looks.
- It is **fork-only**. The worker is a nested function, so under the `spawn` start
  method (Windows default, macOS since 3.8) it cannot be pickled and dies inside a
  child with `EOFError: Ran out of input` — a traceback that names `multiprocessing`
  and never names the harness. On Linux it is fine. Preflight the start method and
  say so, rather than letting that error reach a user.

### Contamination

Held-out means *the model has not seen it*. A benchmark that lives in the repository
you train on is not held out, however carefully you split it. Prefer problems dated
after the model's cutoff, and treat a suspiciously good score as evidence of leakage
before evidence of skill.

## Traps

- **A green test suite proves your port, not your model.** If you reimplement the
  scorer rather than using it, differential-test your version against the original
  across a matrix that hits every branch — an estimator that looks obviously right is
  the classic place to be subtly wrong, and unit tests written against your own
  understanding will agree with you.
- **Multi-language means multi-language-if-you-have-the-toolchains.** The Java, Go,
  Rust and TypeScript arms shell out to compilers. On a host without them, those arms
  report `failed`, which reads as a bad model rather than a missing `javac`.
- **`pass@100` on 10 samples is not 0.** Report only the k values your sample count
  can actually measure; a k you cannot measure should be omitted, not zeroed.

## See also

- `adk-harnesses` — the `sandbox` harness this belongs inside
- `adk-unsloth` — training the model whose output you are now able to score
