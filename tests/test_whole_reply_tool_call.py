"""A reply that IS one bare JSON tool call, for a tool that was offered, is a call.

Measured 2026-10-01 on a clean first-timer install: Ternary-Bonsai-1.7B (CPU) behind
llama-server, 22 Hearth tools offered, answered "Say hello in five words." with the
content ``{"name": "ask_human", "arguments": {...}}`` -- no ``<tool_call>`` tags, no
``finish_reason`` hint -- and ``adk home serve`` showed the raw JSON to the user.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from adk.llm.base import Message, offered_tool_names, whole_reply_tool_call
from adk.llm.openai_compat import OpenAIProvider

OFFERED = {"ask_human", "remind_me"}


def test_a_whole_reply_naming_an_offered_tool_is_a_call():
    tc = whole_reply_tool_call(
        ' {"name": "remind_me", "arguments": {"text": "stretch", "when": "in 1h"}} ',
        OFFERED)
    assert tc is not None and tc.name == "remind_me"
    assert tc.arguments == {"text": "stretch", "when": "in 1h"}


def test_fenced_and_string_arguments_are_accepted():
    tc = whole_reply_tool_call('```json\n{"name": "ask_human", "arguments": "{\\"title\\": '
                               '\\"x\\"}"}\n```', OFFERED)
    assert tc is not None and tc.arguments == {"title": "x"}


@pytest.mark.parametrize("text", [
    'Here you go: {"name": "remind_me", "arguments": {}}',          # prose around it
    '{"name": "send_money", "arguments": {}}',                       # not offered
    '{"name": "remind_me", "arguments": {}, "note": "hi"}',          # extra keys
    '{"name": "remind_me", "arguments": [1, 2]}',                    # args not an object
    '{"text": "hello"}',                                              # not a call
    '{broken',
    "hello",
    "",
])
def test_anything_else_is_left_as_text(text):
    assert whole_reply_tool_call(text, OFFERED) is None


def test_nothing_offered_means_nothing_salvaged():
    assert whole_reply_tool_call('{"name": "remind_me", "arguments": {}}', set()) is None


def test_offered_tool_names_reads_openai_and_flat_shapes():
    tools = [{"type": "function", "function": {"name": "a"}}, {"name": "b"}, "junk", {}]
    assert offered_tool_names(tools) == {"a", "b"}
    assert offered_tool_names(None) == set()


def _provider(content: str) -> OpenAIProvider:
    p = OpenAIProvider(base_url="http://127.0.0.1:9/v1", api_key="", default_model="m")

    class _Resp:
        def json(self):
            return {"model": "m", "choices": [{"message": {"content": content},
                                               "finish_reason": "stop"}], "usage": {}}

    async def _post(client, url, payload):
        return _Resp()

    p._post_with_retry = _post  # type: ignore[method-assign]
    return p


TOOLS = [{"type": "function", "function": {"name": "ask_human", "parameters": {}}}]


def test_the_provider_turns_the_whole_reply_into_a_call():
    raw = json.dumps({"name": "ask_human", "arguments": {"title": "Say hello?"}})
    out = asyncio.run(_provider(raw).chat([Message(role="user", content="hi")], tools=TOOLS))
    assert [(c.name, c.arguments) for c in out.tool_calls] == [
        ("ask_human", {"title": "Say hello?"})]
    assert out.content == ""


def test_the_provider_leaves_it_alone_when_no_tools_were_offered():
    raw = json.dumps({"name": "ask_human", "arguments": {}})
    out = asyncio.run(_provider(raw).chat([Message(role="user", content="hi")]))
    assert out.tool_calls == [] and out.content == raw
