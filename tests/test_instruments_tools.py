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


@pytest.mark.skipif(not _HAS_AWPREDICT, reason="awpredict not installed on this box")
async def test_predict_engines_reports_rows_or_an_honest_error():
    from adk.builtin_tools import predict_engines

    out = json.loads(await predict_engines())
    # Either real rows, or the honest failure shape — never a silent empty.
    assert "engines" in out or "error" in out


async def test_predict_conforms_honest_on_missing_module():
    from adk.builtin_tools import predict_conforms

    out = json.loads(await predict_conforms("definitely_not_a_module_xyz", "Nope"))
    assert "error" in out
