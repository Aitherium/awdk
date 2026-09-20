"""adk.compact + /compact: a long tool output shrinks through the decision door,
an unreachable door never drops a line, and a wrong verdict can be taught.

These are CLIENT tests: the compaction rules themselves are asserted by the
service tree's `compact.py --self-test`. What must hold here is the seam --
which mode was chosen, what reaches /decide/batch, and that every failure path
keeps more rather than less.
"""

from __future__ import annotations

import json

import pytest
from adk import compact as api
from adk.shell.plugins.builtins import compact as plugin

PYTEST_OUT = "\n".join(
    ["============================= test session starts =============================",
     "platform win32 -- Python 3.12.6, pytest-8.3.2",
     "collected 12 items",
     ""]
    + [f"tests/test_a.py::test_{i} PASSED                        [{i * 8:3d}%]" for i in range(40)]
    + ["tests/test_a.py::test_boom FAILED                         [100%]",
       "E       assert 1 == 2",
       "tests/test_a.py:12: AssertionError",
       "FAILED tests/test_a.py::test_boom - assert 1 == 2",
       "========================= 1 failed, 40 passed in 3.21s ========================="]
) + "\n"

MUST_SURVIVE = ("FAILED tests/test_a.py::test_boom - assert 1 == 2",
                "1 failed, 40 passed",
                "E       assert 1 == 2")


class _Resp:
    def __init__(self, body):
        self._b = json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _impl_or_skip():
    impl = api._impl()
    if impl is None:
        pytest.skip("the world-model service tree is not on this box "
                    "(set AITHER_WM_SVC); the remote path is the public one")
    return impl


# ------------------------------------------------------------------ fallback
def test_no_service_tree_still_compacts_and_keeps_the_failures(monkeypatch):
    """A stranger's machine has no service tree: crude rules, never a refusal.

    The drop FLOOR is the point of this test, not `kept_lines < total_lines`.
    Until 2026-09-20 `passed` was a bare keep-word in the fallback, so every
    per-test `PASSED` line was kept: on a real 314-line passing pytest run it
    kept 308 lines where the door's own rules keep 28. The loose assertion
    passed the whole time, because dropping six lines out of 314 is still
    "fewer".
    """
    monkeypatch.setattr(api, "_impl", lambda: None)
    r = api.compact(PYTEST_OUT, "pytest")
    assert r.mode.startswith("rules-only")
    assert r.kept_lines < r.total_lines
    assert r.dropped >= r.total_lines // 2, (
        f"the fallback kept {r.kept_lines} of {r.total_lines} lines — it is "
        "keeping the filler it exists to remove"
    )
    for line in MUST_SURVIVE:
        assert line in r.kept


def test_the_fallback_does_not_keep_a_line_just_for_saying_passed(monkeypatch):
    """A COUNT of passes is a summary; a per-test PASSED line is filler."""
    monkeypatch.setattr(api, "_impl", lambda: None)
    r = api.compact(PYTEST_OUT, "pytest")
    assert "1 failed, 40 passed" in r.kept
    per_test = [ln for ln in r.kept.split("\n")
                if "PASSED" in ln and "::test_" in ln]
    # Each distinct skeleton keeps its first two and its last, and this fixture
    # has two of them (the `[  0%]` and `[100%]` bracket widths), so 6 is the
    # floor a correct fallback reaches. The regression being pinned is 40.
    assert len(per_test) <= 8, f"{len(per_test)} per-test PASSED lines survived"


def test_the_fallback_keeps_every_record_header_in_a_git_log(monkeypatch):
    """Repetition is not filler when the repeated line is a record BOUNDARY.
    Forty `commit <sha>` lines share one skeleton; collapsing them by
    repetition lost 31 of the bench's labelled commit hashes."""
    monkeypatch.setattr(api, "_impl", lambda: None)
    shas = [f"commit {i:040x}" for i in range(40)]
    log = []
    for i, sha in enumerate(shas):
        log += [sha, f"Author: Someone <a{i}@b.com>",
                "Date:   Sat Sep 20 08:00:00 2026 +0000", "",
                f"    fix(thing): subject number {i}", ""]
        log += [f" src/mod_{j}.py | {j + 1} ++--" for j in range(6)] + [""]
    r = api.compact("\n".join(log) + "\n", "git")
    for sha in shas:
        assert sha in r.kept, f"{sha} was collapsed away"
    for i in range(40):
        assert f"    fix(thing): subject number {i}" in r.kept
    assert r.dropped > 0, "the diffstat rows should still collapse"


def test_the_fallback_collapses_a_repeating_shape_whatever_the_wording(monkeypatch):
    """The skeleton must survive differing word counts inside a line: two
    pytest ids with different numbers of underscores are the same SHAPE."""
    monkeypatch.setattr(api, "_impl", lambda: None)
    names = ["a", "b_c", "d_e_f", "g_h_i_j"]
    body = [f"tests/test_{names[i % 4]}.py::test_{names[i % 4]}_{i} PASSED [{i:3d}%]"
            for i in range(80)]
    r = api.compact("\n".join(["header one", "header two"] + body
                              + ["=== 80 passed in 1.00s ==="]) + "\n", "pytest")
    assert r.dropped >= 60, f"only {r.dropped} of {r.total_lines} dropped"
    assert "80 passed" in r.kept


def test_strict_mode_says_so_instead_of_pretending(monkeypatch):
    monkeypatch.setattr(api, "_impl", lambda: None)
    with pytest.raises(api.CompactUnavailableError):
        api.compact(PYTEST_OUT, "pytest", strict=True)


# ------------------------------------------------------------------- remote
def test_remote_mode_posts_one_batch_of_shapes_and_never_the_text(monkeypatch):
    """One /decide/batch for the whole output, questions carry SHAPES only."""
    from adk import choose as door

    calls = []

    def fake_urlopen(req, timeout=0, context=None):
        body = json.loads(req.data)
        calls.append((req.full_url, body))
        return _Resp({"answers": [
            {"answer": "no", "source": "engine", "confidence": 0.9,
             "decision_id": f"d{i}", "learned_from": 7}
            for i, _ in enumerate(body["items"])]})

    monkeypatch.setenv("AITHER_DECIDE_URL", "http://door.test")
    monkeypatch.setattr(door.urllib.request, "urlopen", fake_urlopen)
    _impl_or_skip()
    r = api.compact(PYTEST_OUT, "pytest", mode="remote")

    assert r.mode == "remote"
    assert len(calls) == 1, "one round trip for the whole output, not one per line"
    url, body = calls[0]
    assert url == "http://door.test/decide/batch"
    assert all(it["domain"] == "decide.compact.pytest" for it in body["items"])
    assert all(it["kind"] == "yesno" for it in body["items"])
    blob = json.dumps(body)
    assert "test_boom" not in blob and "assert 1 == 2" not in blob, (
        "the door is asked about shapes; no line text may leave the process")
    assert r.learned and r.dropped > 30
    for line in MUST_SURVIVE:
        assert line in r.kept


def test_an_unreachable_door_keeps_what_the_rules_kept(monkeypatch):
    from adk import choose as door

    def down(req, timeout=0, context=None):
        raise door.urllib.error.URLError("refused")

    monkeypatch.setenv("AITHER_DECIDE_URL", "http://door.test")
    monkeypatch.setattr(door.urllib.request, "urlopen", down)
    _impl_or_skip()
    r = api.compact(PYTEST_OUT, "pytest", mode="remote")
    assert "door unreachable" in r.mode
    for line in MUST_SURVIVE:
        assert line in r.kept
    # and it is not silently a pass: strict callers get the error
    with pytest.raises(api.CompactUnavailableError):
        api.compact(PYTEST_OUT, "pytest", mode="remote", strict=True)


def test_source_none_keeps_the_line(monkeypatch):
    """'nothing to go on' must never read as 'drop it'."""
    from adk import choose as door

    def fake_urlopen(req, timeout=0, context=None):
        body = json.loads(req.data)
        return _Resp({"answers": [
            {"answer": "no", "source": "none", "confidence": 0.0, "decision_id": f"n{i}"}
            for i, _ in enumerate(body["items"])]})

    monkeypatch.setenv("AITHER_DECIDE_URL", "http://door.test")
    monkeypatch.setattr(door.urllib.request, "urlopen", fake_urlopen)
    _impl_or_skip()
    r = api.compact(PYTEST_OUT, "pytest", mode="remote")
    assert r.raw["door_dropped"] == 0
    assert all(d["verdict"] == "unknown" for d in r.decisions)


# ------------------------------------------------------------------ teaching
def test_teach_posts_a_reward_per_decision_and_none_without_a_verdict(monkeypatch):
    from adk import choose as door

    posted = []

    def fake_urlopen(req, timeout=0, context=None):
        body = json.loads(req.data)
        if req.full_url.endswith("/decide/batch"):
            return _Resp({"answers": [
                {"answer": "no", "source": "engine", "confidence": 0.9, "decision_id": f"d{i}"}
                for i, _ in enumerate(body["items"])]})
        posted.append(body)
        return _Resp({"ok": True, "answer": body.get("answer"), "reward": body.get("reward")})

    monkeypatch.setenv("AITHER_DECIDE_URL", "http://door.test")
    monkeypatch.setattr(door.urllib.request, "urlopen", fake_urlopen)
    _impl_or_skip()
    r = api.compact(PYTEST_OUT, "pytest", mode="remote")

    assert api.teach(r, wrong_ids=None) == [], "silence is not a reward"
    ids = r.decision_ids()
    api.teach(r, wrong_ids=[ids[0]])
    rewards = {p.get("decision_id"): p.get("reward") for p in posted if p.get("decision_id")}
    assert rewards[ids[0]] == -1.0
    assert all(v == 1.0 for k, v in rewards.items() if k != ids[0])
    # a wrong 'drop' also teaches the OTHER answer, or the engine keeps picking it
    counterfactuals = [p for p in posted if p.get("state") and p.get("answer") == "yes"]
    assert counterfactuals, "a wrong verdict must teach the alternative too"


# -------------------------------------------------------------------- plugin
def test_plugin_parses_and_guesses_the_fork(tmp_path):
    opts = plugin._parse(["run.log", "--tool", "pytest", "--budget", "10"])
    assert opts["tool"] == "pytest" and opts["budget"] == 10
    assert plugin._parse(["build-podman.log"])["tool"] == "podman"
    assert plugin._parse(["whatever.txt"])["tool"] == "tool"
    with pytest.raises(ValueError):
        plugin._parse([])


def test_plugin_renders_counts_and_the_teach_hint():
    r = api.CompactResult(
        kept="two lines\nkept", kept_lines=2, total_lines=90, dropped=88,
        mode="in-process", tool="pytest", source_counts={"engine": 6},
        decisions=[{"decision_id": "abc123", "shape": "kind:pytest-pass", "count": 80,
                    "verdict": "drop", "source": "engine", "confidence": 0.9}],
        latency_ms=12.0,
    )
    text = plugin.render(r)
    assert "kept 2 of 90 lines" in text and "/compact teach abc123" in text
    assert "engine" in plugin.render_decisions(r)
    assert r.header().startswith("[compacted by the decision door: kept 2 of 90 lines")


def test_plugin_reads_the_last_output_from_ctx():
    assert plugin._read("-", {"last_output": "hello"}) == "hello"
    with pytest.raises(ValueError):
        plugin._read("-", {})


def test_a_single_letter_pytest_verdict_survives_the_fallback():
    """The adversarial pass, 2026-09-20. A progress line whose verdict is the
    single letter F has a skeleton BYTE-IDENTICAL to the PASSED lines around it
    (`PASSED` and `F` both collapse to one token run), so the only line naming the
    failing test was swallowed into "... (N similar lines dropped)" -- the model is
    told something failed and cannot be told what. Watched failing before the fix:
    13 of 318 kept, the test name gone.
    """
    lines = [
        f"tests/test_zeta_router.py::test_ok_{i:03d} PASSED  [ {i * 100 // 318:2d}%]"
        for i in range(310)
    ]
    lines.insert(
        154, "tests/test_zeta_router.py::test_capability_denies_foreign_tenant F  [ 48%]"
    )
    lines.append("=========== 1 failed, 310 passed in 4.02s ===========")
    text = "\n".join(lines)

    kept = api._rules_only_fallback(text, "pytest").kept
    assert "test_capability_denies_foreign_tenant" in kept, kept[-400:]
    assert "1 failed, 310 passed" in kept
    # and it must still be a COMPACTION, not a passthrough
    assert kept.count("PASSED") < 60, kept.count("PASSED")


def test_a_dotted_progress_line_carrying_an_F_survives_the_fallback():
    """The other shape of the same defect: `tests/x.py ....F...  [ 48%]`."""
    text = "\n".join(
        ["tests/test_a.py ........................            [ 20%]"] * 40
        + ["tests/test_b.py ....F...                            [ 48%]"]
        + ["tests/test_c.py ........................            [ 90%]"] * 40
        + ["=========== 1 failed, 640 passed in 9.1s ==========="]
    )
    kept = api._rules_only_fallback(text, "pytest").kept
    assert "test_b.py ....F" in kept, kept[-300:]
