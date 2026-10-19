"""adk kvslot — llama-server prompt prefixes computed once, restored from the KV pool.

Every agent call on a big model starts with the same few thousand tokens: the system
prompt, the tool schemas, the persona. On a model whose prefill is slow (a large MoE
streaming its experts prefills at single or tens of tokens per second, not thousands), that
shared prefix is most of the latency of a short call. llama-server can already write a slot's KV to disk
(``POST /slots/{id}?action=save``) and read it back (``?action=restore``); what it lacks is
a NAME for the file that every holder and every agent computes the same way. This module
gives it the kvpool name:

    key = kvpool chain hash of the prompt's tokens up to a block boundary

so the snapshot is content addressed: two agents whose prompts share the first N blocks
compute the same key for block N without talking to each other, and a snapshot for a
different model can never be found because the chain is seeded with the model fingerprint.

Flow for one call (``PrefixCache.complete``):

1. tokenize the FULL prompt on the server (one tokenization; never prefix and tail
   separately, BPE merges across the seam would make the tokens disagree);
2. walk the chain hashes from the longest block down and restore the first snapshot the
   store holds (the server is then at exactly ``cut`` cached tokens);
3. miss: prefill only up to the declared shared boundary (``prefix_tokens``), save the slot,
   put the file in the store under its key — the next caller, on this server or a fresh
   one, restores it;
4. send the full prompt with ``cache_prompt``; the server evaluates only ``len - cut``
   tokens. ``timings.prompt_n`` in the response is the proof, and ``Outcome`` carries it.

Why snapshot at the boundary instead of trusting ``--cache-reuse``: V4's compressed KV
cache (``llama_kv_cache_dsv4``) cannot shift (``get_can_shift() == false``) and cannot
truncate to an arbitrary earlier position (``seq_rm`` accepts only ``p0 > pos_max`` or a
rollback inside ``n_rs_seq``). A slot that cached prefix+tail-of-agent-A therefore cannot
be cut back to the prefix for agent B, and llama-server falls back to a full reprocess. A
snapshot taken AT the prefix boundary only ever grows, which the V4 cache allows.

The store is a directory (``SlotStore``): ``<root>/<model fp[:16]>/<key>.slot`` plus a JSON
sidecar with the token count, byte size and sha256. A restore copies the file into the
server's ``--slot-save-path`` and verifies the digest first; a corrupt or foreign file is a
miss, never a restore. ``NexusPrefixDirectory`` (kvpool) can be attached so a router knows
which node holds a prefix warm; it is a hint and never fatal.

Standard library only; Python 3.10.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .kvpool import BLOCK_TOKENS, chain_hashes, model_fingerprint

log = logging.getLogger("adk.kvslot")

SLOT_PREFIX = "kvp-"
_DIGEST_CHUNK = 1 << 20


class SlotError(RuntimeError):
    """The server refused a slot action or answered something this module cannot read."""


# ---------------------------------------------------------------- the server


class LlamaServer:
    """The four llama-server endpoints this module needs, over urllib.

    ``headers`` carries any credential the caller holds (``Authorization: Bearer ...`` for a
    server started with ``--api-key``); this client reads no secret itself.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 600.0,
        headers: Optional[dict] = None,
        ssl_context: Optional[ssl.SSLContext] = None,
    ):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = dict(headers or {})
        self.ctx = ssl_context

    def _req(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", **self.headers},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            detail = e.read()[:400].decode("utf-8", "replace")
            raise SlotError(f"{method} {path}: HTTP {e.code}: {detail}") from e

    def props(self) -> dict:
        return self._req("GET", "/props") or {}

    def tokenize(self, text: str, add_special: bool = True) -> list:
        out = self._req("POST", "/tokenize", {"content": text, "add_special": add_special})
        toks = (out or {}).get("tokens")
        if not isinstance(toks, list):
            raise SlotError(f"/tokenize answered no token list: {out!r}"[:300])
        return [int(t if not isinstance(t, dict) else t["id"]) for t in toks]

    def completion(self, tokens: Sequence[int], n_predict: int, id_slot: int, **extra) -> dict:
        body = {
            "prompt": list(tokens),
            "n_predict": n_predict,
            "id_slot": id_slot,
            "cache_prompt": True,
            "temperature": 0.0,
            **extra,
        }
        return self._req("POST", "/completion", body) or {}

    def slot_action(self, id_slot: int, action: str, filename: Optional[str] = None) -> dict:
        body = {} if filename is None else {"filename": filename}
        return self._req("POST", f"/slots/{id_slot}?action={action}", body) or {}


def fingerprint_from_props(props: dict, *extra: str) -> bytes:
    """A model fingerprint from what the server says it serves, plus caller-known parts.

    Pass the KV cache type and anything else that changes the KV bytes in ``extra``
    (``"ctk=q8_0"``); a weights digest is better than the path when the caller has one.
    """
    model = str(props.get("model_path") or props.get("model_alias") or "")
    build = str(props.get("build_info") or "")
    return model_fingerprint("llama-server-slot", model, build, *extra)


def scoped_fingerprint(model_fp: bytes, scope: str) -> bytes:
    """The chain seed for one sharing scope (a tenant, a workspace, a family) on one model.

    Two scopes on the same model compute DIFFERENT keys for the same tokens and land in
    different store directories, so a snapshot never crosses a scope: no cross-tenant
    restore of a planted file, and no timing probe of whether another tenant used a prompt.
    """
    if not scope or not isinstance(scope, str):
        raise ValueError("a sharing scope (tenant/workspace/family id) is required")
    return model_fingerprint(b"kvslot-scope", model_fp, scope)


# ---------------------------------------------------------------- the store


@dataclass
class SlotMeta:
    key: str
    model_fp: str
    tokens: int
    nbytes: int
    sha256: str
    created: float
    source: str = ""
    extra: dict = field(default_factory=dict)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_DIGEST_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


class SlotStore:
    """Content-addressed slot snapshots on a local disk (a GPU box, a workstation, a phone)."""

    def __init__(self, root: os.PathLike | str):
        self.root = Path(root)

    def _dir(self, model_fp: bytes) -> Path:
        return self.root / model_fp.hex()[:16]

    def paths(self, model_fp: bytes, key: bytes) -> tuple[Path, Path]:
        d = self._dir(model_fp)
        return d / f"{key.hex()}.slot", d / f"{key.hex()}.json"

    def meta(self, model_fp: bytes, key: bytes) -> Optional[SlotMeta]:
        blob, side = self.paths(model_fp, key)
        if not (blob.is_file() and side.is_file()):
            return None
        try:
            m = SlotMeta(**json.loads(side.read_text()))
        except (OSError, ValueError, TypeError) as e:
            log.warning("kvslot: unreadable sidecar %s: %s", side, e)
            return None
        if m.model_fp != model_fp.hex() or m.key != key.hex():
            return None
        return m

    def put(
        self,
        model_fp: bytes,
        key: bytes,
        src: Path,
        tokens: int,
        source: str = "",
        extra: Optional[dict] = None,
    ) -> SlotMeta:
        blob, side = self.paths(model_fp, key)
        blob.parent.mkdir(parents=True, exist_ok=True)
        tmp = blob.with_suffix(".slot.tmp")
        shutil.copyfile(src, tmp)
        m = SlotMeta(
            key=key.hex(),
            model_fp=model_fp.hex(),
            tokens=int(tokens),
            nbytes=tmp.stat().st_size,
            sha256=_sha256(tmp),
            created=time.time(),
            source=source,
            extra=dict(extra or {}),
        )
        os.replace(tmp, blob)
        side.write_text(json.dumps(asdict(m), indent=1))
        return m

    def fetch(self, model_fp: bytes, key: bytes, dst: Path) -> Optional[SlotMeta]:
        """Copy the snapshot to ``dst`` iff its digest still matches; else a miss."""
        m = self.meta(model_fp, key)
        if m is None:
            return None
        blob, _ = self.paths(model_fp, key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(blob, dst)
        if _sha256(dst) != m.sha256:
            log.warning("kvslot: digest mismatch for %s, treating as a miss", blob)
            dst.unlink(missing_ok=True)
            return None
        return m

    def entries(self) -> list:
        out = []
        for side in sorted(self.root.glob("*/*.json")):
            try:
                out.append(SlotMeta(**json.loads(side.read_text())))
            except (OSError, ValueError, TypeError):
                continue
        return out


# ---------------------------------------------------------------- the cache


@dataclass
class Outcome:
    """What one call did. ``prompt_n`` is the server's own count of evaluated tokens."""

    key: str
    prompt_tokens: int
    cut: int
    restored: bool
    published: bool
    prompt_n: int
    prompt_ms: float
    restore_ms: float = 0.0
    save_ms: float = 0.0
    warm_prompt_ms: float = 0.0
    content: str = ""
    first_logprobs: dict = field(default_factory=dict)

    @property
    def skipped(self) -> int:
        return self.prompt_tokens - self.prompt_n


class PrefixCache:
    """Restore the longest pooled prefix into a slot, publish a missing one, then complete.

    ``slot_dir`` is the server's ``--slot-save-path`` as seen from THIS process (same host,
    or a shared mount); llama-server only reads and writes plain filenames inside it.
    ``scope`` is REQUIRED: the tenant/workspace/family whose callers may share prefixes.
    It seeds the chain, so keys and store directories never cross scopes.
    """

    def __init__(
        self,
        server: LlamaServer,
        store: SlotStore,
        slot_dir: os.PathLike | str,
        model_fp: bytes,
        block: int = BLOCK_TOKENS,
        id_slot: int = 0,
        directory: Any = None,
        node_id: str = "",
        *,
        scope: str,
    ):
        if block <= 0:
            raise ValueError("block must be positive")
        self.server, self.store = server, store
        self.slot_dir = Path(slot_dir)
        self.model_fp, self.scope = model_fp, scope
        self.fp, self.block, self.id_slot = scoped_fingerprint(model_fp, scope), block, id_slot
        self.directory = directory
        self.node = node_id

    @staticmethod
    def _fname(key: bytes) -> str:
        return f"{SLOT_PREFIX}{key.hex()[:40]}.bin"

    def _restore(self, hashes: list) -> tuple[int, bytes, float]:
        """Restore the longest stored prefix; (cut tokens, key, restore ms), (0, b"", 0) on none."""
        for j in range(len(hashes) - 1, -1, -1):
            key = hashes[j]
            name = self._fname(key)
            m = self.store.fetch(self.fp, key, self.slot_dir / name)
            if m is None:
                continue
            want = (j + 1) * self.block
            if m.tokens != want:
                log.warning(
                    "kvslot: %s holds %d tokens, chain says %d; skipped", name, m.tokens, want
                )
                continue
            try:
                r = self.server.slot_action(self.id_slot, "restore", name)
            except SlotError as e:
                log.warning("kvslot: restore refused (%s); treating as a miss", e)
                continue
            if int(r.get("n_restored", -1)) != want:
                log.warning("kvslot: restore read %s tokens, wanted %d", r.get("n_restored"), want)
                try:
                    self.server.slot_action(self.id_slot, "erase")
                except SlotError as e:
                    log.warning("kvslot: erase after a short restore refused: %s", e)
                continue
            return want, key, float((r.get("timings") or {}).get("restore_ms", 0.0))
        return 0, b"", 0.0

    def _publish(
        self, tokens: list, hashes: list, prefix_tokens: int
    ) -> tuple[int, bytes, float, float]:
        """Prefill up to the shared boundary, save, store. (cut, key, prompt ms, save ms)."""
        nblk = min(prefix_tokens // self.block, len(hashes))
        if nblk == 0:
            return 0, b"", 0.0, 0.0
        cut, key = nblk * self.block, hashes[nblk - 1]
        self.server.slot_action(self.id_slot, "erase")
        warm = self.server.completion(tokens[:cut], n_predict=0, id_slot=self.id_slot)
        prompt_ms = float((warm.get("timings") or {}).get("prompt_ms", 0.0))
        name = self._fname(key)
        # a server that cannot save (no --slot-save-path, a cache type without state_write)
        # still answers the call: the warm slot serves it, nothing is published
        try:
            r = self.server.slot_action(self.id_slot, "save", name)
            if int(r.get("n_saved", -1)) != cut:
                raise SlotError(f"slot save wrote {r.get('n_saved')} tokens, wanted {cut}")
            self.store.put(
                self.fp, key, self.slot_dir / name, cut, source=self.node or self.server.base
            )
        except (SlotError, OSError) as e:
            log.warning("kvslot: publish failed, serving without a snapshot: %s", e)
            return 0, b"", prompt_ms, 0.0
        if self.directory is not None:
            try:
                self.directory.register(key.hex(), nblk, cut)
            except Exception as e:  # a routing hint is never fatal
                log.warning("kvslot: prefix directory register failed: %s", e)
        return cut, key, prompt_ms, float((r.get("timings") or {}).get("save_ms", 0.0))

    def complete(
        self,
        prompt: str,
        prefix_tokens: Optional[int] = None,
        n_predict: int = 16,
        tokens: Optional[list] = None,
        **extra,
    ) -> Outcome:
        """Complete ``prompt``; its first ``prefix_tokens`` tokens are the shareable part.

        ``prefix_tokens=None`` shares every full block of the prompt (the tail after the last
        block boundary is always private).
        """
        toks = list(tokens) if tokens is not None else self.server.tokenize(prompt)
        hashes = chain_hashes(toks, self.fp, self.block)
        share = len(toks) if prefix_tokens is None else max(0, min(prefix_tokens, len(toks)))
        hashes = hashes[: share // self.block]
        # never let the cut equal the whole prompt: the server must evaluate >= 1 token
        while hashes and len(hashes) * self.block >= len(toks):
            hashes = hashes[:-1]

        cut, key, restore_ms = self._restore(hashes)
        published, warm_ms, save_ms = False, 0.0, 0.0
        if cut == 0 and hashes:
            cut, key, warm_ms, save_ms = self._publish(toks, hashes, share)
            published = cut > 0

        r = self.server.completion(toks, n_predict=n_predict, id_slot=self.id_slot, **extra)
        t = r.get("timings") or {}
        return Outcome(
            key=key.hex(),
            prompt_tokens=len(toks),
            cut=cut,
            restored=not published and cut > 0,
            published=published,
            prompt_n=int(t.get("prompt_n", -1)),
            prompt_ms=float(t.get("prompt_ms", 0.0)),
            restore_ms=restore_ms,
            save_ms=save_ms,
            warm_prompt_ms=warm_ms,
            content=str(r.get("content", "")),
            first_logprobs=first_logprobs(r),
        )


def first_logprobs(resp: dict) -> dict:
    """``{token id: logprob}`` of the first generated position (needs ``n_probs`` > 0)."""
    cp = resp.get("completion_probabilities") or []
    if not cp:
        return {}
    out = {}
    for t in cp[0].get("top_logprobs") or cp[0].get("probs") or []:
        lp = t.get("logprob")
        if lp is None and t.get("prob"):
            lp = math.log(t["prob"])
        if lp is not None:
            out[int(t.get("id", -1))] = float(lp)
    return out


def logprob_delta(a: dict, b: dict) -> float:
    """Largest |logprob difference| over tokens both distributions list; inf if disjoint."""
    shared = set(a) & set(b)
    return max((abs(a[t] - b[t]) for t in shared), default=float("inf"))


# ---------------------------------------------------------------- estimate


def saved_prefill_seconds(prefix_tokens: int, prefill_tok_s: float, restore_s: float) -> float:
    """Seconds one call saves by restoring a prefix instead of prefilling it."""
    if prefill_tok_s <= 0:
        raise ValueError("prefill speed must be positive")
    return prefix_tokens / prefill_tok_s - restore_s


# ---------------------------------------------------------------- demo / live proof


def _demo(
    argv: Optional[Sequence[str]] = None, make_server: Callable[[str], LlamaServer] = LlamaServer
) -> int:
    """Two servers, one pool: A publishes the prefix, a FRESH B restores it and skips it."""
    import argparse

    p = argparse.ArgumentParser(prog="python -m adk.kvslot")
    p.add_argument("--a", required=True, help="server A base URL")
    p.add_argument("--a-slots", required=True, help="server A --slot-save-path")
    p.add_argument("--b", required=True, help="server B base URL (fresh process)")
    p.add_argument("--b-slots", required=True, help="server B --slot-save-path")
    p.add_argument("--pool", required=True, help="SlotStore root")
    p.add_argument("--prefix-file", required=True, help="the shared system prompt / tool schema")
    p.add_argument("--block", type=int, default=BLOCK_TOKENS)
    p.add_argument("--fp-extra", default="", help="extra fingerprint part, e.g. ctk=f16")
    p.add_argument("--scope", default="kvslot-demo", help="sharing scope (tenant/workspace)")
    p.add_argument("--n-predict", type=int, default=24)
    p.add_argument("--max-logprob-delta", type=float, default=0.05)
    a = p.parse_args(argv)

    prefix = Path(a.prefix_file).read_text(encoding="utf-8")
    store = SlotStore(a.pool)
    sa, sb = make_server(a.a), make_server(a.b)
    fp = fingerprint_from_props(sa.props(), a.fp_extra)
    if fingerprint_from_props(sb.props(), a.fp_extra) != fp:
        print(json.dumps({"verdict": "FAIL", "why": "A and B serve different models"}))
        return 1
    n_prefix = len(sa.tokenize(prefix))
    tails = [
        "\n\nThe tool list above is long. In one sentence, the capital of France is",
        "\n\nThe tool list above is long. In one sentence, the largest planet is",
    ]
    ca = PrefixCache(sa, store, a.a_slots, fp, block=a.block, node_id="A", scope=a.scope)
    cb = PrefixCache(sb, store, a.b_slots, fp, block=a.block, node_id="B", scope=a.scope)
    n = a.n_predict
    # baseline: fresh B prefills B's whole prompt itself (greedy), slot erased around it
    sb.slot_action(0, "erase")
    base = sb.completion(
        sb.tokenize(prefix + tails[1]), n_predict=n, id_slot=0, cache_prompt=False, n_probs=5
    )
    sb.slot_action(0, "erase")
    oa = ca.complete(prefix + tails[0], prefix_tokens=n_prefix, n_predict=n)
    ob = cb.complete(prefix + tails[1], prefix_tokens=n_prefix, n_predict=n, n_probs=5)
    bt = base.get("timings") or {}
    same = ob.content == str(base.get("content", ""))
    dlp = logprob_delta(ob.first_logprobs, first_logprobs(base))
    ok = (
        ob.restored
        and ob.cut > 0
        and ob.prompt_n == ob.prompt_tokens - ob.cut
        and same
        and dlp <= a.max_logprob_delta
    )
    report = {
        "verdict": "PASS" if ok else "FAIL",
        "prefix_tokens": n_prefix,
        "same_greedy_output_as_full_prefill": same,
        "first_token_top5_max_abs_logprob_delta": dlp,
        "baseline_B_full_prefill": {
            "prompt_n": bt.get("prompt_n"),
            "prompt_ms": bt.get("prompt_ms"),
            "content": base.get("content"),
        },
        "A_publish": asdict(oa),
        "B_restore": asdict(ob),
        "store": [asdict(m) for m in store.entries()],
    }
    print(json.dumps(report, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    sys.exit(_demo(sys.argv[1:]))
