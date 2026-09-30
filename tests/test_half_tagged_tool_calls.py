"""Text tool calls that lost their opening ``<tool_call>`` tag are still calls.

Measured 2026-09-30: Bonsai-4B-Q1_0 behind llama.cpp ``--jinja`` wrote every call
as bare JSON followed by ``</tool_call>``; before this the ADK dropped all of them.
"""

from __future__ import annotations

from adk.llm.base import extract_tool_calls_from_text, has_text_tool_call


def test_bare_json_before_a_closing_tag_is_a_call():
    text = ('{"name": "remind_me", "arguments": {"when": "in 20 minutes", '
            '"text": "pizza"}}\n</tool_call>')
    calls, cleaned = extract_tool_calls_from_text(text)
    assert [(c.name, c.arguments) for c in calls] == [
        ("remind_me", {"when": "in 20 minutes", "text": "pizza"})]
    assert cleaned == ""


def test_nested_arguments_and_surrounding_prose():
    text = ('Sure.\n{"name": "mail_send", "arguments": {"to": "a@example.com", '
            '"meta": {"k": [1, 2]}}}</tool_call>\nAsking you first.')
    calls, cleaned = extract_tool_calls_from_text(text)
    assert calls[0].name == "mail_send"
    assert calls[0].arguments["meta"] == {"k": [1, 2]}
    assert cleaned == "Sure.\n\nAsking you first."


def test_two_half_tagged_calls():
    text = ('{"name": "list_followups", "arguments": {}}</tool_call>'
            '{"name": "receipts", "arguments": {"n": 3}}</tool_call>')
    calls, _ = extract_tool_calls_from_text(text)
    assert [c.name for c in calls] == ["list_followups", "receipts"]


def test_closing_tag_without_a_call_is_left_alone():
    for text in ("done</tool_call>", '{"text": "hello"}</tool_call>', "{broken</tool_call>"):
        calls, cleaned = extract_tool_calls_from_text(text)
        assert calls == [] and cleaned == text


def test_bare_json_without_any_tag_still_needs_the_hint():
    text = '{"name": "todo_add", "arguments": {"text": "milk"}}'
    assert extract_tool_calls_from_text(text)[0] == []
    assert [c.name for c in extract_tool_calls_from_text(text, "tool_calls")[0]] == ["todo_add"]


def test_full_hermes_tags_unchanged():
    text = '<tool_call>{"name": "receipts", "arguments": {"n": 5}}</tool_call>'
    calls, cleaned = extract_tool_calls_from_text(text)
    assert [c.name for c in calls] == ["receipts"] and cleaned == ""


def test_has_text_tool_call():
    assert has_text_tool_call("<tool_call>{}")
    assert has_text_tool_call("{}</tool_call>")
    assert not has_text_tool_call("plain reply")
    assert not has_text_tool_call("")
