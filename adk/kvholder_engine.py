"""adk kvholder chat — run a real model whose old context lives on the holders.

The host keeps the most recent ``window`` tokens of every layer's keys and values. Everything
older is appended, in blocks, to the holders behind a relay (phones, browsers, runners). Each
attention op is holders(far) merged with local(near) by log-sum-exp, which is exact: the
output is attention over every key the model has seen, and only the window stays on the host.

Any Hugging Face causal LM whose attention goes through ``AttentionInterface`` works (Qwen3,
Qwen2, Llama, Mistral, ...). torch, transformers and numpy are imported only by this module;
``adk`` stays importable without them.

    adk kvholder phone                      # the relay; open the page on a phone
    adk kvholder chat                       # Qwen3-0.6B, far context on the phone
    adk kvholder chat --prompt-file doc.txt --question "Summarize it." --once
    adk kvholder chat --prompt-file doc.txt --verify 32   # token-identical to a local run
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

DEFAULT_MODEL = "Qwen/Qwen3-0.6B"
ATTN_NAME = "adk_kvholder"
DEFAULT_QUESTION = "Summarize the document above in three sentences."
_ACTIVE: list = []  # the Engine whose forward is running; the attention hook reads it
_QBLOCK = 512  # queries per local score matrix
_APPEND_MAX = 2048  # keys per APPEND message


def _deps():
    try:
        import numpy as np
        import torch
        import transformers
    except ImportError as e:
        raise RuntimeError(
            f"kvholder chat needs torch, transformers and numpy ({e}): "
            "pip install torch transformers numpy"
        ) from e
    return np, torch, transformers


class HolderLink:
    """One model's attention shape on a PATN connection to a holder or a relay.

    ``wire="v4"`` sends the model's own shapes (the adk holders and the phone page read them).
    ``wire="v3"`` zero-pads heads to 256 and rows to 48 for a holder that speaks only PATN v3
    (a Backburner iPhone); zeros change no dot product, but the holder does ~6x the work.
    """

    def __init__(
        self,
        client,
        n_layer: int,
        n_kv: int,
        n_heads: int,
        head_dim: int,
        kv_type: str = "f16",
        wire: str = "v4",
    ):
        from adk import kvholder as kv

        self.kv = kv
        self.client = client
        self.n_kv, self.groups, self.head_dim = n_kv, n_heads // n_kv, head_dim
        if wire == "v3":
            if head_dim > kv.HD or 8 * self.groups > kv.NR:
                raise ValueError(
                    f"head_dim {head_dim} / {self.groups} query heads per KV head do not fit "
                    "PATN v3 (256 / 6): use --wire v4"
                )
            self.dim, self.rows = kv.HD, kv.NR
        else:
            self.dim, self.rows = head_dim, 8 * self.groups
        client.configure(
            kv.Config.for_model(
                n_layer, n_kv, kv_type, k_dim=self.dim, v_dim=self.dim, rows=self.rows
            )
        )
        self.held = [0] * n_layer
        self.calls = 0
        self.ms = 0.0
        self.holder_ms = 0.0  # what the holders report computing, the rest is the link

    def append(self, layer: int, keys, vals) -> None:
        """K/V float32 [n, n_kv, head_dim] -> the holders, after what they already hold."""
        np = self.kv.np
        pad = self.dim - self.head_dim
        if pad:
            keys = np.pad(keys, ((0, 0), (0, 0), (0, pad)))
            vals = np.pad(vals, ((0, 0), (0, 0), (0, pad)))
        self.held[layer] = self.client.append(layer, self.held[layer], keys, vals)

    def attn(self, layer: int, q, scale: float):
        """Q float32 [T, n_heads, head_dim] -> (O [T, n_heads, head_dim], lse [T, n_heads])."""
        np, kv = self.kv.np, self.kv
        n_t, n_h, hd = q.shape
        g, hkv, dim = self.groups, self.n_kv, self.dim
        outs, lses = [], []
        for s in range(0, n_t, 8 * kv.MAX_GROUPS):
            qc = q[s : s + 8 * kv.MAX_GROUPS]
            t = qc.shape[0]
            ng = -(-t // 8)
            buf = np.zeros((ng * 8, n_h, dim), np.float32)
            buf[:t, :, :hd] = qc
            # PATN row r = gi*8 + tt: token tt of the group, query head kvh*G + gi
            qs = np.zeros((ng, hkv, self.rows, dim), np.float32)
            qs[:, :, : g * 8] = (
                buf.reshape(ng, 8, hkv, g, dim)
                .transpose(0, 2, 3, 1, 4)
                .reshape(ng, hkv, g * 8, dim)
            )
            t0 = time.perf_counter()
            o, lse, meta = self.client.attn(layer, qs if ng > 1 else qs[0], scale, n_tok=min(t, 8))
            self.ms += (time.perf_counter() - t0) * 1000
            self.holder_ms += meta["ms"]
            self.calls += 1
            if ng == 1:
                o, lse = o[None], lse[None]
            o = o[:, :, : g * 8].reshape(ng, hkv, g, 8, dim).transpose(0, 3, 1, 2, 4)
            lse = lse[:, :, : g * 8].reshape(ng, hkv, g, 8).transpose(0, 3, 1, 2)
            outs.append(o.reshape(ng * 8, n_h, dim)[:t, :, :hd])
            lses.append(lse.reshape(ng * 8, n_h)[:t])
        return np.concatenate(outs), np.concatenate(lses)


class Engine:
    """Greedy decoding with the far keys on ``link`` and the last ``window`` keys here.

    ``link=None`` keeps every key on the host (the same code path, nothing evicted).
    """

    def __init__(
        self,
        model,
        link: HolderLink | None = None,
        window: int = 1024,
        block: int = 256,
        chunk: int = 32768,
    ):
        _, torch, _ = _deps()
        self.torch = torch
        self.model, self.link = model, link
        self.window, self.block, self.chunk = window, max(1, block), max(1, chunk)
        c = model.config
        self.n_layer = c.num_hidden_layers
        self.n_heads = c.num_attention_heads
        self.n_kv = c.num_key_value_heads
        self.head_dim = getattr(c, "head_dim", None) or c.hidden_size // c.num_attention_heads
        self.k: list = [None] * self.n_layer  # [n_kv, n, head_dim] per layer
        self.v: list = [None] * self.n_layer
        self.pos = 0  # tokens fed so far
        self._pos0 = 0  # first position of the chunk being fed
        self.pending: list[int] = []  # the last sampled token, fed with the next input
        _register()

    @property
    def far(self) -> int:
        return self.link.held[0] if self.link else 0

    @property
    def near(self) -> int:
        return 0 if self.k[0] is None else int(self.k[0].shape[1])

    # ------------------------------------------------------------ attention hook

    def attend(self, module, query, key, value, scaling: float):
        torch = self.torch
        li = module.layer_idx
        q = query[0].float()  # [H, T, D]
        k_new, v_new = key[0].float(), value[0].float()  # [Hkv, T, D]
        n_t = q.shape[1]
        k = k_new if self.k[li] is None else torch.cat([self.k[li], k_new], 1)
        v = v_new if self.v[li] is None else torch.cat([self.v[li], v_new], 1)
        n = k.shape[1]
        base = self._pos0 + n_t - n  # position of the first local key
        far = self.link.held[li] if self.link else 0
        if base != far:
            raise RuntimeError(f"layer {li}: local keys start at {base}, holders hold {far}")
        g = self.n_heads // self.n_kv
        kr, vr = k.repeat_interleave(g, 0), v.repeat_interleave(g, 0)  # [H, N, D]
        kpos = torch.arange(base, base + n)
        os_, lses = [], []
        for a in range(0, n_t, _QBLOCK):  # bounds the score matrix on a long prompt
            qb = q[:, a : a + _QBLOCK]
            qpos = torch.arange(self._pos0 + a, self._pos0 + a + qb.shape[1])
            last = int(qpos[-1]) - base + 1  # causal: no key after the block's last query
            s = torch.matmul(qb, kr[:, :last].transpose(1, 2)) * scaling  # [H, t, N']
            s = s.masked_fill(kpos[None, :last] > qpos[:, None], float("-inf"))
            lses.append(torch.logsumexp(s, -1))
            os_.append(torch.matmul(torch.softmax(s, -1), vr[:, :last]))
        o, lse = torch.cat(os_, 1), torch.cat(lses, 1)  # [H, T, D], [H, T]
        if far:
            fo, fl = self.link.attn(li, q.transpose(0, 1).contiguous().numpy(), float(scaling))
            fo = torch.from_numpy(fo).transpose(0, 1)
            fl = torch.from_numpy(fl).transpose(0, 1)
            m = torch.maximum(lse, fl)
            a, b = torch.exp(lse - m), torch.exp(fl - m)  # fl = -inf (no keys) -> b = 0
            o = (o * a[..., None] + fo * b[..., None]) / (a + b)[..., None]
        if self.link is not None and n - self.window >= self.block:
            cut = (n - self.window) // self.block * self.block
            for a in range(0, cut, _APPEND_MAX):  # bounded messages for a phone's socket
                b = min(cut, a + _APPEND_MAX)
                self.link.append(
                    li,
                    k[:, a:b].transpose(0, 1).contiguous().numpy(),
                    v[:, a:b].transpose(0, 1).contiguous().numpy(),
                )
            k, v = k[:, cut:].contiguous(), v[:, cut:].contiguous()
        self.k[li], self.v[li] = k, v
        return o.transpose(0, 1)[None].to(query.dtype), None

    # ------------------------------------------------------------ feeding and decoding

    def feed(self, ids: list[int]):
        """Run ``ids`` (after any pending token) through the model; logits of the last one."""
        torch = self.torch
        ids = self.pending + list(ids)
        self.pending = []
        logits = None
        cfg = self.model.config
        prev = cfg._attn_implementation
        cfg._attn_implementation = ATTN_NAME
        _ACTIVE.append(self)
        # the hook masks causally itself: skip the model's dense [T, T] mask (1.6 GB at 20k)
        base = getattr(self.model, "model", None)
        had = base is not None and hasattr(base, "_update_causal_mask")
        if had:
            base._update_causal_mask = lambda *a, **k: None
        try:
            with torch.no_grad():
                for s in range(0, len(ids), self.chunk):
                    part = ids[s : s + self.chunk]
                    self._pos0 = self.pos
                    pos = torch.arange(self.pos, self.pos + len(part))[None]
                    out = self.model(
                        input_ids=torch.tensor([part]),
                        position_ids=pos,
                        use_cache=False,
                        logits_to_keep=1,
                    )
                    self.pos += len(part)
                    logits = out.logits[0, -1].float()
        finally:
            _ACTIVE.pop()
            cfg._attn_implementation = prev
            if had:
                del base._update_causal_mask  # back to the class method
        return logits

    def generate(
        self, ids: list[int], max_new: int, stop: set[int] | None = None, on_token=None
    ) -> tuple[list[int], dict]:
        """Greedy. Returns the new tokens and this turn's timings."""
        stop = stop or set()
        c0, ms0, hms0 = (
            (self.link.calls, self.link.ms, self.link.holder_ms) if self.link else (0, 0.0, 0.0)
        )
        n_in = len(self.pending) + len(ids)
        t0 = time.perf_counter()
        logits = self.feed(ids)
        t1 = time.perf_counter()
        out: list[int] = []
        while True:
            tok = int(logits.argmax())
            out.append(tok)
            if on_token:
                on_token(tok)
            if tok in stop or len(out) >= max_new:
                self.pending = [tok]
                break
            logits = self.feed([tok])
        t2 = time.perf_counter()
        calls = self.link.calls - c0 if self.link else 0
        stats = {
            "prompt_tokens": n_in,
            "prefill_s": t1 - t0,
            "new_tokens": len(out),
            "decode_s": t2 - t1,
            "tok_s": (len(out) - 1) / (t2 - t1) if len(out) > 1 and t2 > t1 else 0.0,
            "far_keys": self.far,
            "near_keys": self.near,
            "holder_calls": calls,
            "holder_ms": (self.link.ms - ms0) if self.link else 0.0,
            "holder_compute_ms": (self.link.holder_ms - hms0) if self.link else 0.0,
        }
        return out, stats


def _attention(module, query, key, value, attention_mask, scaling=None, dropout=0.0, **kw):
    eng = _ACTIVE[-1]
    if scaling is None:
        scaling = module.scaling
    return eng.attend(module, query, key, value, float(scaling))


def _register() -> None:
    _, _, transformers = _deps()
    transformers.AttentionInterface.register(ATTN_NAME, _attention)


def reference_greedy(model, ids: list[int], n: int, chunk: int = 2048) -> list[int]:
    """The model's own attention and KV cache, every key on the host: the --verify baseline."""
    _, torch, transformers = _deps()
    cache = transformers.DynamicCache()
    out: list[int] = []
    with torch.no_grad():
        logits = None
        for s in range(0, len(ids), chunk):
            r = model(
                input_ids=torch.tensor([ids[s : s + chunk]]),
                past_key_values=cache,
                use_cache=True,
                logits_to_keep=1,
            )
            logits = r.logits[0, -1]
        for _ in range(n):
            tok = int(logits.argmax())
            out.append(tok)
            if len(out) == n:
                break
            r = model(input_ids=torch.tensor([[tok]]), past_key_values=cache, use_cache=True)
            logits = r.logits[0, -1]
    return out


# ---------------------------------------------------------------- CLI


def load_model(name: str):
    """A local directory, or a hub id downloaded into ~/.aither/models (plain files)."""
    _, torch, transformers = _deps()
    path = Path(name).expanduser()
    if not path.is_dir():
        from huggingface_hub import snapshot_download

        dest = Path.home() / ".aither" / "models" / name.replace("/", "--")
        # local_dir: Windows refuses the hub cache's symlinks
        path = Path(snapshot_download(name, local_dir=str(dest)))
    tok = transformers.AutoTokenizer.from_pretrained(str(path))
    model = transformers.AutoModelForCausalLM.from_pretrained(
        str(path), torch_dtype=torch.float32, attn_implementation="sdpa"
    ).eval()
    return model, tok


def _relay_addr(arg: str) -> tuple[str, int]:
    if arg:
        host, _, port = arg.rpartition(":")
        return host or "127.0.0.1", int(port)
    from adk import kvholder_net

    st = kvholder_net.read_state() or {}
    return "127.0.0.1", int(st.get("engine_port") or 50062)


def _render(tok, history: list[dict], think: bool) -> str:
    kw = {} if think else {"enable_thinking": False}
    try:
        return tok.apply_chat_template(history, tokenize=False, add_generation_prompt=True, **kw)
    except TypeError:
        return tok.apply_chat_template(history, tokenize=False, add_generation_prompt=True)


def _stop_ids(model, tok) -> set[int]:
    ids = set()
    for v in (tok.eos_token_id, getattr(model.generation_config, "eos_token_id", None)):
        if isinstance(v, int):
            ids.add(v)
        elif v:
            ids.update(v)
    return ids


def _line(st: dict) -> str:
    per = st["holder_ms"] / st["holder_calls"] if st["holder_calls"] else 0.0
    return (
        f"[{st['prompt_tokens']} in {st['prefill_s']:.1f}s | {st['new_tokens']} out "
        f"{st['tok_s']:.2f} tok/s | far keys on holders {st['far_keys']:,} "
        f"(host window {st['near_keys']}) | holder {st['holder_calls']} calls "
        f"{per:.1f} ms avg, {st['holder_compute_ms'] / max(st['holder_calls'], 1):.1f} ms compute]"
    )


def run(args) -> int:
    try:
        _deps()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2
    import torch

    from adk import kvholder as kv

    # torch's default (every core) thrashes on a busy host: 16x slower measured at 100% load
    torch.set_num_threads(args.threads or max(1, min(8, (os.cpu_count() or 2) // 2)))
    print(f"kvholder chat: loading {args.model}", flush=True)
    model, tok = load_model(args.model)
    c = model.config
    link = None
    if not args.local:
        host, port = _relay_addr(args.relay)
        try:
            client = kv.KVHolderClient(host, port, timeout=args.timeout)
            hello = client.hello()
            for attempt in range(3):
                try:
                    link = HolderLink(
                        client,
                        c.num_hidden_layers,
                        c.num_key_value_heads,
                        c.num_attention_heads,
                        getattr(c, "head_dim", None) or c.hidden_size // c.num_attention_heads,
                        kv_type=args.kv,
                        wire=args.wire,
                    )
                    break
                except RuntimeError as e:
                    # a stale tab that died during CONFIG is dropped by the relay: ask again
                    if "holder lost" not in str(e) or attempt == 2:
                        raise
                    print(f"kvholder chat: {e}; configuring again", flush=True)
        except (OSError, RuntimeError, ValueError) as e:
            print(
                f"kvholder chat: relay {host}:{port}: {e}\n"
                "  start one with `adk kvholder phone` and attach a holder, or pass --local",
                file=sys.stderr,
            )
            return 1
        print(
            f"kvholder chat: holders via {host}:{port}: {hello['device']} "
            f"(PATN {args.wire}, {args.kv}, host window {args.window})",
            flush=True,
        )
    eng = Engine(model, link, window=args.window, block=args.block, chunk=args.chunk)
    stop = _stop_ids(model, tok)
    history: list[dict] = []
    fed_text = ""

    def turn(user: str, max_new: int, show: bool = True) -> tuple[list[int], dict]:
        nonlocal fed_text
        history.append({"role": "user", "content": user})
        full = _render(tok, history, args.think)
        text = (
            full[len(fed_text) :]
            if full.startswith(fed_text)
            else _render(tok, history[-1:], args.think)
        )
        ids = tok(text, add_special_tokens=False)["input_ids"]
        got: list[int] = []
        shown = [0]

        def on_token(t: int) -> None:
            got.append(t)
            if show:
                s = tok.decode(got, skip_special_tokens=True)
                print(s[shown[0] :], end="", flush=True)
                shown[0] = len(s)

        out, st = eng.generate(ids, max_new, stop, on_token)
        reply = tok.decode(out, skip_special_tokens=True)
        history.append({"role": "assistant", "content": reply})
        fed_text = full + tok.decode(out, skip_special_tokens=False)
        if show:
            print()
        print(_line(st), flush=True)
        return out, st

    try:
        if args.prompt_file or args.verify:
            doc = (
                Path(args.prompt_file).read_text(encoding="utf-8", errors="replace")
                if args.prompt_file
                else _SAMPLE_DOC
            )
            user = f"{doc}\n\n{args.question}"
            n = args.verify or args.max_new
            out, st = turn(user, n if args.verify else args.max_new, show=not args.verify)
            if args.verify:
                ids = tok(_render(tok, history[:1], args.think), add_special_tokens=False)[
                    "input_ids"
                ]
                t0 = time.perf_counter()
                ref = reference_greedy(model, ids, len(out))
                same = sum(1 for _ in _prefix(out, ref))
                print(f"verify: engine {out[:12]}... | local {ref[:12]}...")
                print(
                    f"verify: {same}/{len(out)} greedy tokens identical to a fully local run "
                    f"({time.perf_counter() - t0:.1f}s), far keys {st['far_keys']:,} on holders"
                )
                print("reply:", tok.decode(out, skip_special_tokens=True))
                return 0 if out == ref else 1
            if args.once:
                return 0
        while True:
            try:
                user = input("you> ").strip()
            except EOFError:
                return 0
            if user in ("/quit", "/exit"):
                return 0
            if user:
                turn(user, args.max_new)
    except KeyboardInterrupt:
        print()
        return 0
    finally:
        if link is not None:
            link.client.close()


def _prefix(a: list[int], b: list[int]):
    for x, y in zip(a, b):
        if x != y:
            return
        yield x


_SAMPLE_DOC = "\n".join(
    f"Ledger entry {i}: the courier from station {i % 17} delivered {3 + i % 11} crates of "
    f"{('copper', 'salt', 'glass', 'linen', 'cedar')[i % 5]} to warehouse {chr(65 + i % 26)}."
    for i in range(160)
)


def register_args(p) -> None:
    """The ``chat`` verb's flags (called from adk.kvholder_cli, which stays torch-free)."""
    p.add_argument(
        "--model",
        default=os.environ.get("AITHER_KVHOLDER_MODEL", DEFAULT_MODEL),
        help="hub id or local directory (default Qwen/Qwen3-0.6B)",
    )
    p.add_argument(
        "--relay",
        default="",
        help="the relay's engine port HOST:PORT (default: the running relay's state "
        "file, else 127.0.0.1:50062)",
    )
    p.add_argument(
        "--local", action="store_true", help="no holders: every key stays on this machine"
    )
    p.add_argument(
        "--window",
        type=int,
        default=1024,
        help="recent tokens kept on this machine; older ones go to the holders",
    )
    p.add_argument("--block", type=int, default=256, help="tokens per APPEND to the holders")
    p.add_argument(
        "--chunk",
        type=int,
        default=32768,
        help="prefill tokens per forward pass; in one pass each layer ships its old keys "
        "before the next layer runs, so the holders do no prefill attention",
    )
    p.add_argument(
        "--kv",
        choices=["f16", "q8_0"],
        default="f16",
        help="row format on the wire (f16 is exact to fp16)",
    )
    p.add_argument(
        "--wire",
        choices=["v4", "v3"],
        default="v4",
        help="v4: the model's own shapes (the adk holders and the phone page); "
        "v3: pad to 256-wide heads and 48 rows for a v3-only holder (Backburner)",
    )
    p.add_argument("--prompt-file", default="", help="a long document to put in the context")
    p.add_argument("--question", default=DEFAULT_QUESTION, help="asked after --prompt-file")
    p.add_argument("--once", action="store_true", help="answer --prompt-file, then exit")
    p.add_argument("--max-new", type=int, default=256, help="tokens per reply")
    p.add_argument(
        "--verify",
        type=int,
        default=0,
        metavar="N",
        help="also run the model fully locally and require the first N greedy "
        "tokens to be identical (exit 1 otherwise)",
    )
    p.add_argument("--think", action="store_true", help="let Qwen3 think before answering")
    p.add_argument(
        "--threads",
        type=int,
        default=0,
        help="torch CPU threads (default: half the cores, at most 8)",
    )
    p.add_argument("--timeout", type=float, default=300.0, help="seconds per holder call")
