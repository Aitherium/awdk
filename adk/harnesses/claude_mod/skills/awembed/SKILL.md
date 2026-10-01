---
name: awembed
description: "Distil an embedding model on your own corpus and prove it beats the teacher on a held-out split; score any served embedders on your data. Use before picking an embedder."
---

# awembed — an embedder that knows your corpus

```bash
awembed corpus --help        # build the training corpus from a repository
awembed probe                # can the teacher load here, one forward pass
awembed run --help           # capture -> distill -> quantize -> eval, end to end
awembed eval --help          # teacher / baseline / student / int8 on held-out split
awembed compare --corpus docs.jsonl --queries queries.jsonl \
    --endpoint a=http://host:port/v1 --endpoint b=... [--out report.json]
```

Every stage has its own flags: `awembed <stage> --help`.

**Exit codes:** 0 done · non-zero on a failed stage (quantize is fidelity-gated and
refuses a lossy export).

**Trap:** `compare` is black-box and cheap — run it FIRST; distillation needs a GPU
and the `teacher` extra (`pip install "awembed[teacher]"`). Prefixes matter: pass
`--qprefix/--dprefix` per endpoint or an instruction-tuned embedder scores low unfairly.
