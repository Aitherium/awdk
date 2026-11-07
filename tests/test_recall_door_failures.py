"""A run that FAILED must never come back from the recall door as a hit.

Measured 2026-11-02: a forge run that hit an LLM timeout completed with the text
"[Agent Failed] backend_failure: timeout: LLM call timed out after 450s". It was
recorded as verified, and every retry of the same ask was answered from the store in
about 20 s with 0 model calls, failing forever.
"""
from adk import recall_door as R

FAILURES = [
    "not done: the agent ended without a RESULT verdict (unfinished or cut off)\n\n"
    "[Agent Failed] backend_failure: timeout: LLM call timed out after 450s",
    "I'm sorry, the language model took too long to respond. Please try again -- if "
    "this persists, the backend may be overloaded.",
]
REAL = ("Here is the full teaser script: scene one opens on a neon city at night with "
        "the logo forming from light, then cuts to the studio floor.")


def test_failure_text_is_never_verified():
    for text in FAILURES:
        ok, why = R.verify_outcome(text)
        assert not ok, text
        assert "failure" in why
    assert R.verify_outcome(REAL)[0]


def test_a_stored_failure_is_a_miss_not_a_hit(tmp_path, monkeypatch):
    import re
    ask = "Eusyra pilot demo -> teaser: change the pilot demo into a teaser"
    # a row written BEFORE the failure patterns existed: record it under the OLD regex,
    # so it lands verified exactly as the 2026-11-02 rows did
    old = re.compile(r"(the ai backend is temporarily unavailable)", re.IGNORECASE)
    with monkeypatch.context() as m:
        m.setattr(R, "_CANNED_FAILURE", old)
        R.record("forge:test", ask, FAILURES[0], scope="t", verdict="verified", root=tmp_path)
        assert R.lookup("forge:test", ask, scope="t", root=tmp_path).status == R.Status.HIT
    hit = R.lookup("forge:test", ask, scope="t", root=tmp_path)
    assert hit.status == R.Status.MISS, hit


def test_a_real_answer_is_still_a_hit(tmp_path):
    ask = "write the teaser script"
    R.record("forge:test", ask, REAL, scope="t", verdict="verified", root=tmp_path)
    assert R.lookup("forge:test", ask, scope="t", root=tmp_path).status == R.Status.HIT


def test_forge_not_done_and_no_answer_are_failures():
    for text in ("not done: No findings exist for non-copywriter free-to-use children songs, "
                 "sample video, playlists, or YouTube launch plan",
                 "I couldn't produce an answer to that. (the model returned no answer after: "
                 "crm_search_contacts, graph_query)"):
        assert not R.verify_outcome(text)[0], text


def test_failure_text_beats_side_effecting_tool_calls():
    ok, why = R.verify_outcome("the model returned no answer after several tools",
                               tool_calls=[{"name": "mail_draft"}])
    assert not ok and "failure" in why
    assert R.verify_outcome(REAL, tool_calls=[{"name": "mail_draft"}])[0]


def test_result_not_done_verdict_is_a_failure():
    text = ("No findings exist for free-to-use children songs; the searches returned nothing.\n\n"
            "**RESULT: NOT DONE - no source list was found")
    assert not R.verify_outcome(text)[0]
    assert R.verify_outcome(REAL + "\n\nRESULT: DONE")[0]
