"""Engine tournament: parity gate before speed, measured ranking feeds resolve_recipe.

Every endpoint here is an in-process fake OpenAI-compatible server
(``tournament.FakeEngine``), so these tests need no GPU and no network.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from adk.toolpacks.node_bootstrap import recipes
from adk.toolpacks.node_bootstrap import tournament as t
from adk.toolpacks.node_bootstrap.recipes import resolve_recipe

MODEL = "test-model"
PROMPTS = t.GOLDEN_PROMPTS[:8]
CONN = t.Conn(timeout=10.0)


@pytest.fixture(autouse=True)
def _isolated_tournament_file(tmp_path, monkeypatch):
    """No test may read or write the real node verdict file."""
    monkeypatch.setenv(t.ENV_TOURNAMENT_FILE, str(tmp_path / "tournament.json"))


@pytest.fixture
def engines():
    started = []

    def make(mode="identical", **kw):
        e = t.FakeEngine(mode, **kw).start()
        started.append(e)
        return e

    yield make
    for e in started:
        e.close()


def _dead_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}"


# --------------------------------------------------------------------------- prompts

def test_golden_prompts_are_32_diverse_and_unique():
    assert len(t.GOLDEN_PROMPTS) == 32
    assert len({p["id"] for p in t.GOLDEN_PROMPTS}) == 32
    cats = {p["category"] for p in t.GOLDEN_PROMPTS}
    assert {"prose", "code", "math", "extraction", "multilingual", "instruction"} <= cats


def test_load_prompts_jsonl(tmp_path):
    f = tmp_path / "p.jsonl"
    f.write_text('{"id": "a", "prompt": "hi"}\n\n{"prompt": "there", "category": "x"}\n',
                 encoding="utf-8")
    got = t.load_prompts(f)
    assert [p["prompt"] for p in got] == ["hi", "there"]
    assert got[0]["id"] == "a" and got[1]["category"] == "x"
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"id": "a"}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        t.load_prompts(bad)


# --------------------------------------------------------------------------- complete

def test_complete_parses_logprobs(engines):
    e = engines()
    c = t.complete(e.url, MODEL, "hello", max_tokens=6, top_logprobs=3, conn=CONN)
    assert c.tokens and len(c.tokens) == 6
    assert c.text == "".join(c.tokens)
    assert c.top is not None and all(1 <= len(d) <= 3 for d in c.top)
    assert c.chosen_logprobs is not None and len(c.chosen_logprobs) == 6


def test_complete_bearer_comes_from_env_var(engines, monkeypatch):
    e = engines(require_token="tok-123")
    with pytest.raises(t.EndpointError):
        t.complete(e.url, MODEL, "hi", 4, 0, t.Conn(timeout=5.0))
    monkeypatch.setenv("TEST_ENGINE_TOKEN", "tok-123")
    c = t.complete(e.url, MODEL, "hi", 4, 0, t.Conn(token_env="TEST_ENGINE_TOKEN", timeout=5))
    assert c.text


def test_tls_verification_is_on_by_default():
    ctx = t.Conn().ssl_context()
    assert ctx.verify_mode.name == "CERT_REQUIRED" and ctx.check_hostname


def test_insecure_warns(capsys):
    ctx = t.Conn(insecure=True).ssl_context()
    assert ctx.verify_mode.name == "CERT_NONE"
    assert "DISABLED" in capsys.readouterr().err


# --------------------------------------------------------------------------- parity

def test_identical_engine_passes(engines):
    r = t.parity(engines().url, engines().url, MODEL, PROMPTS, max_tokens=16, conn=CONN)
    assert r.verdict == t.PASS, r.reasons
    assert r.exit_code == 0
    assert r.floor["exact_match_rate"] == 1.0
    assert r.candidate["top1_agreement"] == 1.0


def test_perturbed_engine_fails(engines):
    r = t.parity(engines().url, engines("perturbed").url, MODEL, PROMPTS, max_tokens=16,
                 conn=CONN)
    assert r.verdict == t.FAIL, r.reasons
    assert r.exit_code == 1
    failed = {c["metric"] for c in r.checks if c["ok"] is False}
    assert "mean_abs_dlogprob" in failed and "exact_match_rate" in failed


def test_perturbed_engine_would_pass_with_absurd_tolerances(engines):
    """The FAIL above comes from the tolerance gate, not from a hardwired verdict."""
    loose = {k: 10.0 for k in t.LOGPROB_METRICS + t.TEXT_METRICS}
    r = t.parity(engines().url, engines("perturbed").url, MODEL, PROMPTS, max_tokens=16,
                 tolerances=loose, conn=CONN)
    assert r.verdict == t.PASS, r.reasons


def test_no_logprobs_with_gate_required_is_unjudged(engines):
    r = t.parity(engines().url, engines("nologprobs").url, MODEL, PROMPTS, max_tokens=16,
                 conn=CONN)
    assert r.verdict == t.UNJUDGED, r.reasons
    assert r.exit_code == 2
    assert any("not measurable" in x for x in r.reasons)


def test_no_logprobs_text_only_when_not_required(engines):
    r = t.parity(engines().url, engines("nologprobs").url, MODEL, PROMPTS, max_tokens=16,
                 require_logprobs=False, conn=CONN)
    assert r.verdict == t.PASS, r.reasons
    lp_checks = [c for c in r.checks if c["metric"] in t.LOGPROB_METRICS]
    assert all(c["ok"] is None and not c["required"] for c in lp_checks)


def test_nondeterministic_reference_is_unjudged(engines):
    r = t.parity(engines("nondeterministic").url, engines().url, MODEL, PROMPTS,
                 max_tokens=16, conn=CONN)
    assert r.verdict == t.UNJUDGED
    assert "non-deterministic" in " ".join(r.reasons)


def test_batching_server_noise_is_judged_on_argmax_not_text(engines):
    """A batching server drifts in text but not in argmax; parity must still judge it.

    Measured on a production vLLM lane: 42% exact match against itself, 98.6% top-1
    agreement. Gating on text refused every such server.
    """
    ref = engines("batchnoise", length=48).url
    r = t.parity(ref, engines(length=48).url, MODEL, PROMPTS, max_tokens=48, conn=CONN)
    assert r.floor["exact_match_rate"] < 0.5 <= r.floor["top1_agreement"]
    assert r.verdict == t.PASS, r.reasons
    text = [c for c in r.checks if c["metric"] in t.TEXT_METRICS]
    assert text and all(not c["required"] for c in text)
    bad = t.parity(ref, engines("perturbed", length=48).url, MODEL, PROMPTS,
                   max_tokens=48, conn=CONN)
    assert bad.verdict == t.FAIL


def test_model_mismatch_is_unjudged(engines):
    r = t.parity(engines().url, engines(model="other").url, MODEL, PROMPTS, max_tokens=16,
                 conn=CONN)
    assert r.verdict == t.UNJUDGED and "not served" in r.reasons[0]


def test_unreachable_is_unjudged(engines):
    r = t.parity(engines().url, _dead_url(), MODEL, PROMPTS, max_tokens=16, conn=CONN)
    assert r.verdict == t.UNJUDGED and "unreachable" in r.reasons[0]
    r = t.parity(_dead_url(), engines().url, MODEL, PROMPTS, max_tokens=16, conn=CONN)
    assert r.verdict == t.UNJUDGED


def test_absent_metric_never_counts_as_pass():
    """Unit-level: a required metric that is None makes the verdict UNJUDGED."""
    floor = {"exact_match_rate": 1.0, "divergence_median": 1.0, "top1_agreement": 1.0,
             "topk_overlap": 1.0, "mean_abs_dlogprob": 0.0}
    cand = dict(floor, top1_agreement=None)
    verdict, _reasons, _checks = t._judge(floor, cand, dict(t.DEFAULT_TOLERANCES), True)
    assert verdict == t.UNJUDGED


def test_near_tie_flip_is_forgiven():
    a = t.Completion("x y", ["x", " y"], [-0.1, -0.2],
                     [[("x", -0.1), ("z", -0.15)], [(" y", -0.2)]])
    b = t.Completion("z q", ["z", " q"], [-0.1, -0.2],
                     [[("z", -0.1), ("x", -0.12)], [(" q", -0.2)]])
    m = t.pair_metrics([a], [b], tie_margin=0.1)
    assert m["top1_agreement"] == 1.0 and m["tie_forgiven"] == 1
    m = t.pair_metrics([a], [b], tie_margin=0.01)
    assert m["top1_agreement"] == 0.0


# --------------------------------------------------------------------------- bench

def test_bench_prefers_usage_and_falls_back_to_chunks(engines):
    b = t.bench(engines(token_delay=0.005).url, MODEL, runs=2, max_tokens=10, conn=CONN)
    assert b["ok"] and b["token_source"] == "usage"
    assert b["decode_tps_median"] > 0 and b["ttft_s_median"] >= 0
    b = t.bench(engines(token_delay=0.005, stream_usage=False).url, MODEL, runs=2,
                max_tokens=10, conn=CONN)
    assert b["ok"] and b["token_source"] == "chunks"


def test_bench_unreachable_not_ok():
    b = t.bench(_dead_url(), MODEL, runs=1, warmup=0, conn=t.Conn(timeout=2.0))
    assert not b["ok"] and b["errors"]


# --------------------------------------------------------------------------- cold start

def test_cold_start_waits_for_a_coherent_completion_not_health(engines):
    e = engines(ready=False)

    def launch():
        threading.Timer(0.4, lambda: setattr(e, "ready", True)).start()

    r = t.cold_start(launch, e.url, MODEL, timeout=10, poll_s=0.05, conn=CONN)
    assert r["ok"], r
    assert r["seconds"] >= 0.4


def test_cold_start_times_out(engines):
    e = engines(ready=False)
    r = t.cold_start(lambda: None, e.url, MODEL, timeout=0.3, poll_s=0.05, conn=CONN)
    assert not r["ok"] and "no coherent completion" in r["error"]


# --------------------------------------------------------------------------- tournament

def test_tournament_ranks_pass_entrants_by_speed(engines, tmp_path):
    slow, fast, cheat = (engines(token_delay=0.04), engines(token_delay=0.005),
                         engines("perturbed"))
    out = tmp_path / "t.json"
    rep = t.run_tournament(
        [{"recipe_id": "slow-ref", "url": slow.url},
         {"recipe_id": "fast", "url": fast.url},
         {"recipe_id": "cheat", "url": cheat.url}],
        reference="slow-ref", model=MODEL, prompts=PROMPTS, runs=2, max_tokens=16,
        bench_max_tokens=12, conn=CONN, out_path=out)
    assert rep["ranking"] == ["fast", "slow-ref"]  # cheat is fastest but FAILed parity
    assert rep["exit_code"] == 1
    doc = json.loads(out.read_text(encoding="utf-8"))
    run = doc["models"][MODEL]
    assert doc["latest_model"] == MODEL
    assert run["node"]["host_sha256"] and "tournament" in run["versions"]
    assert t.load_ranking(path=out) == ["fast", "slow-ref"]
    assert t.load_ranking(model="nope", path=out) == []


def test_tournament_all_pass_exits_zero_and_honours_env_file(engines, tmp_path):
    rep = t.run_tournament(
        [{"recipe_id": "a", "url": engines().url}, {"recipe_id": "b", "url": engines().url}],
        reference="a", model=MODEL, prompts=PROMPTS[:3], runs=1, max_tokens=8,
        bench_max_tokens=8, conn=CONN)
    assert rep["exit_code"] == 0
    assert sorted(rep["ranking"]) == ["a", "b"]
    assert rep["written_to"] == str(tmp_path / "tournament.json")
    assert sorted(t.load_ranking()) == ["a", "b"]


def test_rank_ties_break_on_ttft_then_cold_start():
    rows = [
        {"recipe_id": "r", "parity": {"verdict": t.PASS},
         "bench": {"ok": True, "decode_tps_median": 10, "ttft_s_median": 0.5}},
        {"recipe_id": "x", "parity": {"verdict": t.PASS},
         "bench": {"ok": True, "decode_tps_median": 50, "ttft_s_median": 0.2},
         "cold_start": {"seconds": 30}},
        {"recipe_id": "y", "parity": {"verdict": t.PASS},
         "bench": {"ok": True, "decode_tps_median": 50, "ttft_s_median": 0.2},
         "cold_start": {"seconds": 10}},
        {"recipe_id": "z", "parity": {"verdict": t.PASS},
         "bench": {"ok": True, "decode_tps_median": 50, "ttft_s_median": 0.1}},
        {"recipe_id": "f", "parity": {"verdict": t.FAIL},
         "bench": {"ok": True, "decode_tps_median": 999, "ttft_s_median": 0.01}},
        {"recipe_id": "u", "parity": {"verdict": t.UNJUDGED},
         "bench": {"ok": True, "decode_tps_median": 999, "ttft_s_median": 0.01}},
    ]
    assert t.rank_entrants(rows, reference="r") == ["z", "y", "x", "r"]
    # a reference whose own floor run was not judged is NOT eligible
    rows[0]["parity"]["verdict"] = t.UNJUDGED
    assert t.rank_entrants(rows, reference="r") == ["z", "y", "x"]


def test_load_ranking_rejects_stale_foreign_and_broken(tmp_path):
    f = tmp_path / "t.json"
    run = {"finished_at": time.time(), "node": {"host_sha256": t._host_hash()},
           "ranking": ["a"]}
    f.write_text(json.dumps({"models": {"m": run}, "latest_model": "m"}), encoding="utf-8")
    assert t.load_ranking(path=f) == ["a"]
    assert t.load_ranking(path=f, max_age_s=-1) == []
    run["node"]["host_sha256"] = "someone-else"
    f.write_text(json.dumps({"models": {"m": run}, "latest_model": "m"}), encoding="utf-8")
    assert t.load_ranking(path=f) == []
    f.write_text("{not json", encoding="utf-8")
    assert t.load_ranking(path=f) == []
    assert t.load_ranking(path=tmp_path / "missing.json") == []


# --------------------------------------------------------------------------- resolve_recipe

BIG_CPU_BOX = {"ram_gb": 64.0, "cpu_cores": 16, "gpu_vendor": "none", "gpu_vram_mb": 0,
               "unified_memory": False, "gpu_name": ""}


def test_resolve_recipe_unchanged_without_ranking():
    assert resolve_recipe(BIG_CPU_BOX)["recipe"]["id"] == "cpu-ollama"
    assert resolve_recipe(BIG_CPU_BOX, ranking=[])["recipe"]["id"] == "cpu-ollama"


def test_resolve_recipe_measured_winner_beats_tier_order():
    got = resolve_recipe(BIG_CPU_BOX, ranking=["cpu-1bit-llamacpp", "cpu-ollama"])
    assert got["recipe"]["id"] == "cpu-1bit-llamacpp"
    assert "engine-tournament" in got["rationale"]


def test_resolve_recipe_never_lets_auto_select_false_or_unfit_win():
    ranking = ["strata-moe-offload", "bonsai-selfhost", "cuda-vllm-40gb", "metal-ollama"]
    got = resolve_recipe(BIG_CPU_BOX, ranking=ranking)["recipe"]["id"]
    assert got == "cpu-ollama"
    # the ranked ids are real recipes, so the exclusion is the rule, not a missing file
    for rid in ranking:
        assert recipes._load_recipe(rid), rid


def test_resolve_recipe_reads_the_tournament_file(tmp_path, monkeypatch):
    f = tmp_path / "tournament.json"
    run = {"finished_at": time.time(), "node": {"host_sha256": t._host_hash()},
           "ranking": ["bonsai-selfhost", "cpu-1bit-llamacpp", "cpu-ollama"]}
    f.write_text(json.dumps({"models": {"m": run}, "latest_model": "m"}), encoding="utf-8")
    monkeypatch.setenv(t.ENV_TOURNAMENT_FILE, str(f))
    assert resolve_recipe(BIG_CPU_BOX)["recipe"]["id"] == "cpu-1bit-llamacpp"


def test_resolve_recipe_ignores_a_ranking_that_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("broken")

    monkeypatch.setattr(t, "load_ranking", boom)
    assert resolve_recipe(BIG_CPU_BOX)["recipe"]["id"] == "cpu-ollama"


def test_explicit_recipe_id_still_wins_over_ranking():
    got = resolve_recipe(BIG_CPU_BOX, recipe_id="cloud-api", ranking=["cpu-ollama"])
    assert got["recipe"]["id"] == "cloud-api"


# --------------------------------------------------------------------------- self-test + CLI

def test_self_test_passes():
    assert t.self_test(verbose=False) == 0


def test_self_test_can_fail(monkeypatch):
    real = t.parity

    def always_pass(*a, **k):
        r = real(*a, **k)
        r.verdict = t.PASS
        return r

    monkeypatch.setattr(t, "parity", always_pass)
    assert t.self_test(verbose=False) == 1


def test_cli_parity_exit_codes(engines, monkeypatch, capsys):
    from adk.toolpacks.node_bootstrap import __main__ as cli

    ref, pert = engines(), engines("perturbed")
    prompts = ["--max-tokens", "8"]
    monkeypatch.setattr("sys.argv", ["x", "parity", "--reference", ref.url, "--candidate",
                                     ref.url, "--model", MODEL, *prompts])
    assert cli.main() == 0
    monkeypatch.setattr("sys.argv", ["x", "parity", "--reference", ref.url, "--candidate",
                                     pert.url, "--model", MODEL, *prompts])
    assert cli.main() == 1
    monkeypatch.setattr("sys.argv", ["x", "parity", "--reference", ref.url, "--candidate",
                                     _dead_url(), "--model", MODEL, *prompts])
    assert cli.main() == 2
    out = capsys.readouterr().out
    assert "_exit_code" not in out


def test_cli_tournament_requires_args(monkeypatch):
    from adk.toolpacks.node_bootstrap import __main__ as cli

    monkeypatch.setattr("sys.argv", ["x", "tournament"])
    assert cli.main() == 2


# --------------------------------------------------------------------------- reviewer regressions

def test_near_tie_flip_engine_passes_despite_zero_exact_match(engines):
    """Same model, one forgiven near-tie flip mid-answer: raw exact match is 0, PASS."""
    r = t.parity(engines(tie_at=8).url, engines("tieflip", tie_at=8).url, MODEL, PROMPTS,
                 max_tokens=16, conn=CONN)
    assert r.verdict == t.PASS, r.reasons
    assert r.candidate["exact_match_rate"] == 0.0  # raw value kept, advisory
    assert r.candidate["exact_match_rate_adj"] == 1.0
    assert r.candidate["tie_forgiven"] == len(PROMPTS)
    text = [c for c in r.checks if c["metric"] in t.TEXT_METRICS]
    assert text and all(c["basis"] == "tie-adjusted" for c in text)


def test_tie_adjustment_does_not_forgive_a_real_divergence(engines):
    """A non-tie divergence still fails the text gate even with logprobs present."""
    r = t.parity(engines().url, engines("perturbed").url, MODEL, PROMPTS, max_tokens=16,
                 conn=CONN)
    assert r.verdict == t.FAIL
    assert r.candidate["exact_match_rate_adj"] == 0.0


def test_cold_start_waits_out_a_slow_completion(engines):
    """A 2.5 s completion must not be cut off by a short probe timeout."""
    e = engines(completion_delay=2.5)
    r = t.cold_start(lambda: None, e.url, MODEL, timeout=20, poll_s=0.05,
                     conn=t.Conn(timeout=10.0))
    assert r["ok"], r
    assert r["seconds"] >= 2.5


def test_bench_buffered_stream_falls_back_to_end_to_end(engines):
    """All chunks in one write after 0.6 s of generation must not read as ~1e5 tok/s."""
    e = engines(token_delay=0.05, buffered=True)
    b = t.bench(e.url, MODEL, runs=2, max_tokens=12, warmup=0, conn=CONN)
    assert b["ok"], b
    assert b["tps_method"] == "end_to_end"
    assert b["decode_tps_median"] < 100, b
    honest = t.bench(engines(token_delay=0.01).url, MODEL, runs=2, max_tokens=12,
                     warmup=0, conn=CONN)
    assert honest["tps_method"] == "chunk_window"


def test_bench_needs_a_majority_of_runs(engines):
    flaky = engines(token_delay=0.002, fail_mod=5)  # 1 request in 5 succeeds
    b = t.bench(flaky.url, MODEL, runs=5, max_tokens=8, warmup=0, conn=CONN)
    assert b["runs"] == 1 and b["run_errors"] == 4
    assert not b["ok"]
    assert "only 1/5" in b["errors"][0]


def test_unjudged_reference_writes_no_ranking(engines, tmp_path):
    out = tmp_path / "t.json"
    rep = t.run_tournament(
        [{"recipe_id": "refr", "url": engines(model="wrong-model").url},
         {"recipe_id": "b", "url": engines().url}],
        reference="refr", model=MODEL, prompts=PROMPTS[:2], runs=1, max_tokens=8,
        bench_max_tokens=8, conn=CONN, out_path=out)
    assert rep["exit_code"] == 2
    assert rep["entrants"][0]["parity"]["verdict"] == t.UNJUDGED
    assert rep["ranking"] == []
    assert t.load_ranking(path=out) == []


def test_nondeterministic_reference_tournament_writes_no_ranking(engines, tmp_path):
    out = tmp_path / "t.json"
    rep = t.run_tournament(
        [{"recipe_id": "ref", "url": engines("nondeterministic").url},
         {"recipe_id": "b", "url": engines("nologprobs").url}],
        reference="ref", model=MODEL, prompts=PROMPTS, runs=1, max_tokens=16,
        bench_max_tokens=8, conn=CONN, out_path=out)
    assert rep["exit_code"] == 2 and rep["ranking"] == []
    assert t.load_ranking(path=out) == []


def test_launched_entrants_are_measured_one_at_a_time(engines, tmp_path):
    """launch -> parity -> bench -> stop per entrant: never two engines up together."""
    es = {rid: engines(ready=False, token_delay=0.002) for rid in ("a", "b", "c")}
    peak = {"now": 0, "max": 0}

    def launcher(rid):
        def go():
            es[rid].ready = True
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
        return go

    def stopper(rid):
        def go():
            es[rid].ready = False
            peak["now"] -= 1
        return go

    rows = [{"recipe_id": rid, "url": e.url, "launch": launcher(rid), "stop": stopper(rid)}
            for rid, e in es.items()]
    rep = t.run_tournament(rows, reference="b", model=MODEL, prompts=PROMPTS[:2], runs=1,
                           max_tokens=8, bench_max_tokens=8, conn=CONN,
                           out_path=tmp_path / "t.json", cold_start_timeout=10)
    assert peak["max"] == 1, peak
    assert rep["exit_code"] == 0, rep["entrants"]
    assert sorted(rep["ranking"]) == ["a", "b", "c"]
    assert all(r["cold_start"]["ok"] for r in rep["entrants"])


def test_cli_rejects_a_duplicate_entrant_id(monkeypatch, capsys):
    from adk.toolpacks.node_bootstrap import __main__ as cli

    monkeypatch.setattr("sys.argv", ["x", "tournament", "a=http://127.0.0.1:1",
                                     "a=http://127.0.0.1:2", "--reference", "a",
                                     "--model", MODEL, "--no-write"])
    assert cli.main() == 2
    assert "more than once" in capsys.readouterr().out


def test_single_measured_recipe_does_not_leapfrog_unmeasured_ones():
    """A tournament between A and B says nothing about C: a lone entry changes nothing."""
    got = resolve_recipe(BIG_CPU_BOX, ranking=["cpu-1bit-llamacpp"])["recipe"]["id"]
    assert got == "cpu-ollama"
