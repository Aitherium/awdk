"""Loop-stall repairs measured on 2026-10-27 (adk-daemon transcripts).

The failure shape: the reasoner returned the correct call, the fast model
re-issued its stale call verbatim; repeats were reported as successes so
nothing escalated; refusals named no allowed roots; and a bare call_tool({})
raised a TypeError the loop could only retry. These pin the repairs.
"""

from adk.agent import parse_reasoner_call


def test_parses_a_named_call_with_nested_arguments():
    advice = 'REASONER: try find_entity({"owner": {"name": "Jerry Jones"}}) first.'
    got = parse_reasoner_call(advice, {"find_entity", "route_query"})
    assert got == ("find_entity", {"owner": {"name": "Jerry Jones"}})


def test_ignores_a_tool_that_is_not_registered():
    advice = 'call delete_everything({"confirm": true})'
    assert parse_reasoner_call(advice, {"find_entity"}) is None


def test_ignores_prose_without_a_call():
    assert parse_reasoner_call("retry the same lookup once", {"self_memory_search"}) is None


def test_ignores_malformed_json_arguments():
    advice = "self_memory_search({'query': 'x',})"
    assert parse_reasoner_call(advice, {"self_memory_search"}) is None
