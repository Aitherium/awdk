# KV holders for DeepSeek-V4.1 compressed sparse attention

Reference implementation: `adk/kvholder_v41.py`. Parity test: `tests/test_kvholder_v41.py`.
Model math: DeepSeek-V4.1-Flash-1017 `inference/model.py` (Compressor, Indexer,
`select_candidate_blocks`, Attention, `sparse_attn`). Runtime it targets: the Unsloth fork
`unslothai/llama.cpp` branch `dsv41-pin-after61` @4a314d3, `src/models/deepseek41.cpp`.

## What V4.1 attention is

One softmax per head over `[window raw KV | top-k compressed rows]` plus an attention sink:

- **Window**: the last `window_size` (128) raw KV rows of this layer. Host only.
- **Compressed rows**: produced only by the kv-source layers (2, 8, 14, 20). Ratio 2 for
  layers 2-19 (one row per 2 tokens, softmax-gated pooling), ratio 1 for 20-39. Every
  compressed layer reads the latest kv source at or before it.
- **Index keys**: `k_norm(wk(latent))`, RoPE'd, one per compressed row, also produced only
  by kv sources (the fork stores them next to the row: `[512 KV | 128 index K]`).
- **Indexer** on index-source layers (2, 8, 14, 20, 24, 28, 32, 36):
  `score[t] = sum_h relu(q_idx_h . k_t) * w_h`, `top-k = min(512, n_comp)`, reused by the
  layers until the next index source.
- **Two-level candidates**: layer 20 also scores blocks of 8 rows by their max, pins the
  block holding each query's newest visible row (+inf), keeps the top 2048 blocks; layers
  24-36 score only inside those blocks.
- **Sink**: `attn_sink[h]` adds `exp(sink)` to the denominator, nothing to the numerator.

## What a holder keeps

For its raw-token page range `[lo, hi)` (both multiples of `V41Layout.align()` = 8 on the
real config: whole ratio-2 groups and whole 8-row candidate blocks):

| table | rows | per row |
|---|---|---|
| kv source 2, 8, 14 (ratio 2) | `(hi-lo)/2` each | 512-wide KV + 128-wide index key |
| kv source 20 (ratio 1) | `hi-lo` | 512-wide KV + 128-wide index key |

2.5 rows per raw token. At f16 that is 3.2 KB/token (128k tokens: 0.42 GB); at the
model's own fp4 rows (~0.36 KB/row incl. scales) about 0.9 KB/token. No weights, no
window, no MoE, no Engram. Per step it caches only its scores and selections.

## Protocol (per step, per layer; `S` queries)

| round | layers | host -> part | part -> host |
|---|---|---|---|
| SCORE (41/42) | index sources | `q_idx [S,32,128]`, `w [S,32]`, visible length `[S]`, pinned block `[S]` | local top-512 `(score f64, pos)`; on layer 20 also local top-2048 `(block max, block id)` |
| ATTEND (43/44) | every compressed layer | thresholds (index-source layers only), `q [S,64,512]`, scale | `(O [S,64,512], lse [S,64])` over its selected rows |

Index layer: 2 round trips; other compressed layers: 1. 38 compressed layers + 8 index
layers = 46 round trips per token per holder; the payload is a few hundred KB, so LAN /
mesh latency, not bandwidth, sets the cost. The holder's attention work is bounded by
the top-k (at most 512 rows per layer across ALL parts); only scoring is O(rows).

The host is just another part (its own newest slice) plus the coordinator: it merges the
local lists, computes one threshold per query, then merges partials with its window
partial and the sink.

## Why it is exact

Order every candidate by the total order `key(t) = (score desc, position asc)`.

1. **Scores are local.** `score[t]` depends only on `k_t` and the query (`q_idx`, `w`). A
   block's score is the max over its own 8 rows, and an aligned page holds whole blocks.
   The pin is a function of the query's visible length only. So each part computes exactly
   the values the single device would, for its rows.
2. **The union of local top-k contains the global top-k.** If `t` is in the global top-k,
   fewer than `k` keys anywhere outrank it, so fewer than `k` in its own part do, so `t` is
   in its part's local top-k.
3. **One threshold selects exactly the global top-k.** Let `tau` be the k-th key of the
   union. The global top-k is `{t : key(t) >= tau}`, and every part can apply that test to
   its own cached keys. If the union holds fewer than `k` finite keys, no part cut a finite
   key off its list (a cut list holds `k` finite keys), so "all finite" is the answer. If it
   holds exactly `k`, the k-th key must still be used: a part that sent `k` may hold more
   (the parity test caught this; `test_mutant_take_all_when_union_has_exactly_k_fails`).
4. **-inf handling matches the reference.** The reference takes `min(k, width)` and then
   drops unreachable picks; a reachable -inf (non-candidate) pick is impossible when
   `candidate_topk_blocks * block_size >= index_topk` (16384 >= 512, checked by
   `V41Layout.validate`). So "finite keys only" equals the reference.
5. **Partial softmax merges exactly.** For disjoint row sets `P_i` with
   `lse_i = log sum_{j in P_i} exp(s_j)` and `O_i = sum exp(s_j - lse_i) v_j`:
   `O = sum_i exp(lse_i - L) O_i` with `L = log(sum_i exp(lse_i) + exp(sink))`. That is the
   single softmax over the union plus the sink; the sink is a part with `lse = sink`, `O = 0`.
   Window and compressed rows are different sources, so they are disjoint by construction,
   as in the reference concat.
6. **Post-attention ops are linear and host-side** (inverse RoPE on the output, `wo_a`,
   `wo_b`), applied to the merged `O`.

Ties: `torch.topk`/`ggml_top_k` break exact ties arbitrarily, so on a genuine tie the
reference is itself undefined; this protocol picks the lowest position. Scores must be
computed in the same precision on every part (f32 accumulation, as the fork forces with
`GGML_PREC_F32` when its index keys are not quantized); a sub-ulp near-tie can otherwise flip, as it already can
between the fork's CPU and CUDA paths. Thresholds travel as f64 so a part compares against
the exact value it sent.

## What the test proves

`tests/test_kvholder_v41.py` builds a tiny random V4.1 (window-only layer, a ratio-2 group
with a second index source reusing the first's index keys, a ratio-1 group led by the
candidate source plus a level-two consumer) and compares every layer's output and the
selected row set against a numpy port of model.py, itself cross-checked against
model.py's torch `select_candidate_blocks`/top-k code. Five cases: decode (model.py's
`start_pos > 0` path) and multi-query chunks, 1-3 holders, holders owning most selected
rows, and queries that see fewer than k rows. Over the wire (types 40-44, f32 payloads)
too. Mutants that must fail: per-part top-k with no global merge, no pin, ignoring
candidates, strict threshold, the "exactly k" bug, plain-average merge, merge without sink.

## C++ integration plan (fork `dsv41-pin-after61`, not modified in this lane)

The fork computes all of this in one graph per ubatch (`deepseek41.cpp` ~L651-L848):
`comp_all` = `[hd | n_idx_embd]` rows per kv source in the CSA/HCA cache, `score` via
`ggml_mul_mat` + `relu` + `sum_rows`, `cand_pin` from `llm_graph_input_dsv41`, `ggml_top_k`,
and `build_attn_mha` over `concat(raw_k, comp_k)` with `kq_mask = [raw_mask | sel_mask]`.
Steps, smallest first:

1. **Remote row range in the dsv4 cache.** Extend `llama_kv_cache_dsv4` (CSA/HCA contexts)
   with a `remote_lo/remote_hi` compressed range per stream: rows below `remote_hi` are not
   resident; the compressor still writes new rows locally, and an eviction hook ships the
   oldest aligned pages (`V41_LOAD`) when the resident range exceeds a budget. Keep
   `align()` = lcm(ratio * block) so a page never splits a group or a block.
2. **Split the indexer at a graph boundary.** On index-source layers the graph must stop
   after `iq` (RoPE'd) and `iw` (scaled), because the global top-k needs the holders'
   lists. Use a `ggml_custom` op on the CPU backend (the same kind of host callback a
   `LLAMA_KV_REMOTE` PATN host uses) that (a) sends `V41_SCORE` with `iq`, `iw`, the visible lengths and
   the pin, (b) runs the local `score`/`bmax` on resident rows, (c) merges and produces the
   threshold. Output: a `[n_comp_local, nt]` local `sel_mask` / `cand_mask` exactly as
   today, plus the thresholds kept in the input object for the ATTEND call.
3. **Remote partial as an extra attention source.** Keep `build_attn_mha` for resident
   rows, but return `(O, lse)` instead of normalized `O`: that is `ggml_flash_attn_ext`
   with the sink removed and the LSE exposed (`GGML_OP_FLASH_ATTN_EXT` already computes
   `M`/`S` per row; expose them, or run the soft_max path and keep `log(sum)`). A second
   custom op sends `V41_ATTEND` (`q`, thresholds) to each holder and merges the returned
   `(O_i, lse_i)` with the local one and `exp(sink)` - the same `merge_lse`. Store the
   holder's rows in the fork's own `comp_all` cache type, so no quantization differs.
4. **Overlap.** The ATTEND round for layer `l` only needs `q_l`; issue it as soon as `q`
   exists and run the window + resident attention meanwhile. SCORE for index layer `l`
   needs `iq`/`iw` of layer `l`, so it cannot be hoisted, but holders can pre-load the
   next index layer's key table into cache.
5. **Parity gate.** Dump `sel_top_k` and `attn_comp` per layer (the fork already names
   them with `cb()`) with and without a holder (`adk kvholder serve` running
   `V41Holder.handle`), and require identical selections and `|o| < 1e-3` relative on a
   long prompt; reuse this module's `V41Coordinator` as the oracle.
6. **Prefill.** Holders only take pages older than every query of a ubatch, so prefill
   runs unchanged and pages are shipped once their tokens fall out of the resident budget.
