"""Instruments category: awrecurse/awpredict/awrepl as loop tools.

Honest-degradation tests are environment-tolerant (a box without the brick gets
the same {error, fix} JSON the agent sees); the awrepl test runs a REAL session —
it is local, subprocess-backed, and the persistence across calls is the whole
feature.
"""

import importlib.util
import json

import pytest

from adk.builtin_tools import _INSTRUMENTS_TOOLS, TOOL_CATEGORIES, _init_instruments_tools

_HAS_AWRECURSE = importlib.util.find_spec("awrecurse") is not None
_HAS_AWPREDICT = importlib.util.find_spec("awpredict") is not None
_HAS_AWREPL = importlib.util.find_spec("awrepl") is not None


def test_category_registers_the_five_tools():
    _init_instruments_tools()
    assert TOOL_CATEGORIES["instruments"] == _INSTRUMENTS_TOOLS
    names = {fn.__name__ for fn in _INSTRUMENTS_TOOLS}
    assert names == {"recurse_file", "predict_engines", "predict_conforms",
                     "repl_run", "repl_reset"}


@pytest.mark.skipif(not _HAS_AWRECURSE, reason="awrecurse not installed on this box")
async def test_recurse_refuses_an_empty_query():
    from adk.builtin_tools import recurse_file

    out = json.loads(await recurse_file(context="some text", query="  "))
    assert out["error"] == "query is empty"


@pytest.mark.skipif(not _HAS_AWRECURSE, reason="awrecurse not installed on this box")
async def test_recurse_refuses_without_context():
    from adk.builtin_tools import recurse_file

    out = json.loads(await recurse_file(query="what?"))
    assert "no context" in out["error"]


@pytest.mark.skipif(not _HAS_AWRECURSE, reason="awrecurse not installed on this box")
async def test_recurse_missing_file_is_honest():
    from adk.builtin_tools import recurse_file

    out = json.loads(await recurse_file(path="Z:/does/not/exist.txt", query="x"))
    assert "cannot read" in out["error"]


@pytest.mark.skipif(not _HAS_AWREPL, reason="awrepl not installed on this box")
async def test_repl_persists_variables_across_calls():
    from adk.builtin_tools import repl_reset, repl_run

    await repl_reset()
    first = json.loads(await repl_run("x = 41"))
    assert first.get("exception") in (None, "")
    second = json.loads(await repl_run("x + 1"))
    assert "42" in str(second.get("value")), second
    assert second.get("namespace_cleared_by_timeout") is False
    reset = json.loads(await repl_reset())
    assert reset.get("ok") is True
    third = json.loads(await repl_run("print(x)"))
    assert third.get("exception") or "NameError" in (third.get("stderr") or "")


@pytest.mark.skipif(not _HAS_AWREPL, reason="awrepl not installed on this box")
async def test_repl_refuses_empty_code():
    from adk.builtin_tools import repl_run

    out = json.loads(await repl_run("   "))
    assert out["error"] == "code is empty"


@pytest.mark.skipif(not _HAS_AWREPL, reason="awrepl not installed on this box")
async def test_repl_truncation_flag_reflects_our_cut():
    # Review finding: stdout was cut at 4000 while `truncated` reported the
    # WORKER's 64 KiB flag — false — so 1000 chars vanished invisibly.
    from adk.builtin_tools import repl_run

    out = json.loads(await repl_run("print('x' * 5000)"))
    assert len(out["stdout"]) == 4000
    assert out["truncated"] is True


@pytest.mark.skipif(not _HAS_AWREPL, reason="awrepl not installed on this box")
async def test_repl_sessions_are_scoped_per_conversation_context():
    # Review finding: one process-wide namespace shared by every conversation.
    # With TOOL_SESSION_CTX set (stream_react does it per turn), each context
    # gets its own worker.
    from adk.agent import TOOL_SESSION_CTX
    from adk.builtin_tools import repl_run

    token_a = TOOL_SESSION_CTX.set("conv-aaa")
    try:
        await repl_run("scoped_x = 7")
    finally:
        TOOL_SESSION_CTX.reset(token_a)

    token_b = TOOL_SESSION_CTX.set("conv-bbb")
    try:
        out_b = json.loads(await repl_run("scoped_x + 1"))
    finally:
        TOOL_SESSION_CTX.reset(token_b)
    # Different conversation: the variable must NOT exist there.
    assert out_b.get("exception") or "NameError" in (out_b.get("stderr") or ""), out_b

    token_a2 = TOOL_SESSION_CTX.set("conv-aaa")
    try:
        out_a = json.loads(await repl_run("scoped_x + 1"))
    finally:
        TOOL_SESSION_CTX.reset(token_a2)
    assert "8" in str(out_a.get("value")), out_a

    # Don't leak workers across the suite: drop both scoped sessions.
    from adk.builtin_tools import repl_reset

    for conv in ("conv-aaa", "conv-bbb"):
        token = TOOL_SESSION_CTX.set(conv)
        try:
            await repl_reset()
        finally:
            TOOL_SESSION_CTX.reset(token)


def test_keyword_ranker_prefers_query_matching_chunks():
    from adk.builtin_tools import _KeywordRanker

    chunks = [(0, "alpha beta gamma"), (10, "nothing relevant"), (20, "the target lives here")]
    ranked = _KeywordRanker().rank("where is the target", chunks)
    assert ranked[0][0] == 20
    assert _KeywordRanker().rank("", chunks) == chunks  # no terms -> untouched order


@pytest.mark.skipif(not _HAS_AWPREDICT, reason="awpredict not installed on this box")
async def test_predict_engines_reports_real_rows():
    from adk.builtin_tools import predict_engines

    out = json.loads(await predict_engines())
    # On a box WITH awpredict the SUCCESS shape is required — an error here is a
    # regression, not an acceptable alternative (the old or-assertion passed on
    # every failure shape, including a broken probe invocation).
    assert isinstance(out.get("engines"), list) and out["engines"], out
    assert "exit_code" in out


async def test_predict_engines_timeout_is_an_honest_error(monkeypatch):
    # The error branch gets its own deterministic test instead of hiding behind
    # the success assertion's `or`.
    import adk.builtin_tools as bt

    monkeypatch.setattr(bt, "_run_py_capture", lambda code, t: (-1, "", "timed out after 5s"))
    out = json.loads(await bt.predict_engines())
    assert "timed out" in out["error"]


@pytest.mark.skipif(not _HAS_AWPREDICT, reason="awpredict not installed on this box")
async def test_predict_conforms_missing_module_names_the_cause():
    from adk.builtin_tools import predict_conforms

    out = json.loads(await predict_conforms("definitely_not_a_module_xyz", "Nope"))
    # "cannot import" — NOT the awpredict-absent shape, which the old no-skipif
    # test accepted by accident on boxes without the brick.
    assert "cannot import" in out["error"]
