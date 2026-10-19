"""kvslot: a shared prompt prefix is prefilled once, saved under its kvpool key, and a
FRESH server restores it and evaluates only the tail.

The fake llama-server below models the one property that matters for DeepSeek-V4: its KV
cache cannot be truncated to an earlier position, so a slot whose cache is not a prefix of
the new prompt reprocesses everything.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from adk import kvpool
from adk import kvslot as ks

BLOCK = 16


class _Fake:
    """One-slot llama-server: byte tokenizer, slot save/restore, no truncation (V4-like)."""

    def __init__(self, slot_dir: Path, model: str = "fake-model"):
        self.slot_dir, self.model = slot_dir, model
        self.cache: list = []
        self.evaluated: list = []

    def complete(self, body: dict) -> dict:
        prompt = list(body["prompt"])
        lcp = 0
        if body.get("cache_prompt", True):
            while lcp < min(len(prompt), len(self.cache)) and prompt[lcp] == self.cache[lcp]:
                lcp += 1
            if lcp < len(self.cache):  # cache diverges/longer: V4 cannot cut back -> full
                lcp = 0
        if lcp == len(prompt):
            lcp -= 1
        n = len(prompt) - lcp
        self.evaluated.append(n)
        self.cache = prompt[:]
        content = "" if body.get("n_predict", 0) == 0 else f"out-{sum(prompt) % 9973}"
        out = {"content": content, "timings": {"prompt_n": n, "prompt_ms": n * 10.0}}
        if body.get("n_probs"):
            lp = -float(sum(prompt) % 7) / 10
            out["completion_probabilities"] = [{"top_logprobs": [{"id": 1, "logprob": lp}]}]
        return out

    def slot(self, action: str, body: dict) -> tuple[int, dict]:
        if action == "erase":
            self.cache = []
            return 200, {"id_slot": 0, "n_erased": 0}
        path = self.slot_dir / body["filename"]
        if action == "save":
            path.write_text(json.dumps({"model": self.model, "tokens": self.cache}))
            return 200, {
                "n_saved": len(self.cache),
                "n_written": path.stat().st_size,
                "timings": {"save_ms": 1.0},
            }
        if action == "restore":
            if not path.is_file():
                return 400, {"error": "missing"}
            d = json.loads(path.read_text())
            if d["model"] != self.model:
                return 400, {"error": "foreign"}
            self.cache = list(d["tokens"])
            return 200, {"n_restored": len(self.cache), "n_read": 1, "timings": {"restore_ms": 2.0}}
        return 400, {"error": "action"}


def _serve(fake: _Fake) -> tuple[ThreadingHTTPServer, str]:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, obj: dict) -> None:
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            self._send(200, {"model_path": fake.model, "build_info": "b1"})

        def do_POST(self):
            u = urlsplit(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if u.path == "/tokenize":
                self._send(200, {"tokens": list(body["content"].encode())})
            elif u.path == "/completion":
                self._send(200, fake.complete(body))
            elif u.path.startswith("/slots/"):
                self._send(*fake.slot(parse_qs(u.query)["action"][0], body))
            else:
                self._send(404, {})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture
def pair(tmp_path):
    out = []
    for name in ("a", "b"):
        d = tmp_path / f"slots-{name}"
        d.mkdir()
        fake = _Fake(d)
        srv, url = _serve(fake)
        out.append((fake, url, d, srv))
    yield [o[:3] for o in out], ks.SlotStore(tmp_path / "pool")
    for o in out:
        o[3].shutdown()


PREFIX = "system: you are an agent. tools: " + "x" * 70  # 103 bytes -> 6 full blocks


def _cache(url, d, store, fp=None, scope="tenant-a/ws-1"):
    srv = ks.LlamaServer(url, timeout=10)
    fp = fp or ks.fingerprint_from_props(srv.props(), "ctk=f16")
    return ks.PrefixCache(srv, store, d, fp, block=BLOCK, scope=scope)


def test_other_scope_never_hits_and_never_shares_a_directory(pair):
    """Same model, same tokens, another tenant: no restore, its own key and directory."""
    (_, ua, da), (fb, ub, db) = pair[0]
    store = pair[1]
    oa = _cache(ua, da, store, scope="tenant-a/ws-1").complete(
        PREFIX + " q1", prefix_tokens=len(PREFIX)
    )
    ob = _cache(ub, db, store, scope="tenant-b/ws-9").complete(
        PREFIX + " q1", prefix_tokens=len(PREFIX)
    )
    assert oa.published and ob.published and not ob.restored
    assert ob.key != oa.key
    assert fb.evaluated[0] == 96  # B prefilled its own prefix, never A's snapshot
    assert len({p.parent for p in store.root.glob("*/*.slot")}) == 2


def test_scope_is_required():
    srv = ks.LlamaServer("http://127.0.0.1:9", timeout=1)
    with pytest.raises(TypeError):
        ks.PrefixCache(srv, ks.SlotStore("x"), "x", b"\0" * 32)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        ks.PrefixCache(srv, ks.SlotStore("x"), "x", b"\0" * 32, scope="")


def test_save_failure_still_answers_and_publishes_nothing(pair, monkeypatch):
    (_, ua, da), _ = pair[0]
    store = pair[1]
    orig = _Fake.slot

    def no_save(self, action, body):
        if action == "save":
            return 501, {"error": "this cache type cannot be saved"}
        return orig(self, action, body)

    monkeypatch.setattr(_Fake, "slot", no_save)
    o = _cache(ua, da, store).complete(PREFIX + " q1", prefix_tokens=len(PREFIX))
    assert not o.published and not o.restored and o.cut == 0 and o.content
    assert store.entries() == []


def test_fresh_server_restores_and_skips_prefix(pair):
    (fa, ua, da), (fb, ub, db) = pair[0]
    store = pair[1]
    oa = _cache(ua, da, store).complete(PREFIX + " q1?", prefix_tokens=len(PREFIX))
    assert oa.published and not oa.restored and oa.cut == 96
    assert oa.prompt_n == oa.prompt_tokens - 96  # A itself reuses the warm slot
    assert len(store.entries()) == 1

    ob = _cache(ub, db, store).complete(PREFIX + " q2?", prefix_tokens=len(PREFIX))
    assert ob.restored and not ob.published and ob.cut == 96 and ob.key == oa.key
    assert ob.prompt_n == ob.prompt_tokens - 96
    assert fb.evaluated == [ob.prompt_n]  # B never prefilled the prefix

    # restored output equals B prefilling the same prompt itself
    full = ks.LlamaServer(ub).completion(list((PREFIX + " q2?").encode()), 4, 0, cache_prompt=False)
    assert full["content"] == ob.content


def test_key_is_the_kvpool_chain_hash(pair):
    (_, ua, da), _ = pair[0]
    c = _cache(ua, da, pair[1])
    o = c.complete(PREFIX + " q", prefix_tokens=len(PREFIX))
    toks = list((PREFIX + " q").encode())
    assert o.key == kvpool.chain_hashes(toks, c.fp, BLOCK)[5].hex()


def test_corrupt_snapshot_is_a_miss_not_a_restore(pair):
    (_, ua, da), (_, ub, db) = pair[0]
    store = pair[1]
    _cache(ua, da, store).complete(PREFIX + " q1", prefix_tokens=len(PREFIX))
    blob = next(store.root.glob("*/*.slot"))
    blob.write_text(blob.read_text().replace("fake-model", "fake-modeX"))
    ob = _cache(ub, db, store).complete(PREFIX + " q2", prefix_tokens=len(PREFIX))
    assert ob.published and not ob.restored


def test_other_model_never_hits(pair):
    (_, ua, da), (_, ub, db) = pair[0]
    store = pair[1]
    _cache(ua, da, store).complete(PREFIX + " q1", prefix_tokens=len(PREFIX))
    other = kvpool.model_fingerprint("another-model")
    ob = _cache(ub, db, store, fp=other).complete(PREFIX + " q2", prefix_tokens=len(PREFIX))
    assert not ob.restored and ob.published
    assert len(store.entries()) == 2


def test_short_prefix_shares_nothing(pair):
    (fa, ua, da), _ = pair[0]
    o = _cache(ua, da, pair[1]).complete("tiny" + "y" * 40, prefix_tokens=4)
    assert o.cut == 0 and not o.published and o.prompt_n == 44
    assert pair[1].entries() == []


def test_cut_never_swallows_the_whole_prompt(pair):
    (_, ua, da), _ = pair[0]
    o = _cache(ua, da, pair[1]).complete("z" * 64)  # exactly 4 blocks, share all
    assert o.cut == 48 and o.prompt_n == 16


def test_longest_stored_prefix_wins(pair):
    (_, ua, da), (_, ub, db) = pair[0]
    store = pair[1]
    _cache(ua, da, store).complete(PREFIX + " q", prefix_tokens=48)  # publishes 3 blocks
    _cache(ua, da, store).complete(
        PREFIX + " q", prefix_tokens=len(PREFIX)
    )  # restores 3, no publish
    ob = _cache(ub, db, store).complete(PREFIX + " q", prefix_tokens=len(PREFIX))
    assert ob.restored and ob.cut == 48


def test_demo_passes_against_two_fresh_servers(pair, tmp_path, capsys):
    (_, ua, da), (_, ub, db) = pair[0]
    pf = tmp_path / "prefix.txt"
    pf.write_text(PREFIX * 3)
    rc = ks._demo(
        [
            "--a",
            ua,
            "--a-slots",
            str(da),
            "--b",
            ub,
            "--b-slots",
            str(db),
            "--pool",
            str(tmp_path / "pool2"),
            "--prefix-file",
            str(pf),
            "--block",
            str(BLOCK),
            "--n-predict",
            "4",
        ]
    )
    rep = json.loads(capsys.readouterr().out)
    assert rc == 0 and rep["verdict"] == "PASS" and rep["same_greedy_output_as_full_prefill"]
    assert rep["first_token_top5_max_abs_logprob_delta"] == 0.0


def test_logprob_helpers():
    r = {"completion_probabilities": [{"top_logprobs": [{"id": 3, "logprob": -0.5}]}]}
    assert ks.first_logprobs(r) == {3: -0.5}
    assert ks.first_logprobs(
        {"completion_probabilities": [{"probs": [{"id": 2, "prob": 1.0}]}]}
    ) == {2: 0.0}
    assert ks.logprob_delta({1: -0.1}, {1: -0.3}) == pytest.approx(0.2)
    assert ks.logprob_delta({1: 0.0}, {2: 0.0}) == float("inf")


def test_saved_prefill_seconds():
    assert ks.saved_prefill_seconds(4096, 64.0, 0.5) == pytest.approx(63.5)
    with pytest.raises(ValueError):
        ks.saved_prefill_seconds(1, 0, 0)
