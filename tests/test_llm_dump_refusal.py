"""ADK_LLM_DUMP_DIR: a provider refusal leaves its REDACTED request shape on disk."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from adk.llm import openai_compat as oc


class _Resp:
    def __init__(self, status: int, text: str = ""):
        self.status_code = status
        self.headers: dict[str, str] = {}
        self.text = text or f"status {status}"
        self.reason_phrase = "x"
        self.request = httpx.Request("POST", "http://x")


class _Client:
    def __init__(self, resps):
        self.resps = list(resps)

    async def post(self, url, json=None):
        return self.resps.pop(0)


PAYLOAD = {"model": "m", "tools": [{"a": 1}], "messages": [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "", "reasoning_content": "thinking...",
     "tool_calls": [{"id": "c1"}]},
    {"role": "tool", "tool_call_id": "c1", "content": "SECRET TOOL OUTPUT"},
    {"role": "assistant", "content": "narrating", "tool_calls": [{"id": "c2"}]},
]}


def test_shape_is_lengths_only():
    shape = oc.request_shape(PAYLOAD)
    assert [s["role"] for s in shape] == ["system", "user", "assistant", "tool", "assistant"]
    assert shape[2]["reasoning_len"] == 11 and shape[4]["reasoning_len"] == 0
    assert shape[3]["tool_call_id"] == "c1"
    assert "SECRET" not in json.dumps(shape)


def test_a_400_writes_the_dump_when_the_dir_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("ADK_LLM_DUMP_DIR", str(tmp_path))
    prov = oc.OpenAIProvider(base_url="http://x/v1", api_key="k", default_model="m")
    client = _Client([_Resp(400, '{"error":{"message":"reasoning_content must be passed back"}}')])
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(prov._post_with_retry(client, "http://x/v1/chat/completions", PAYLOAD))
    files = list(tmp_path.glob("refusal-*-400.json"))
    assert len(files) == 1
    d = json.loads(files[0].read_text(encoding="utf-8"))
    assert d["status"] == 400 and "reasoning_content" in d["body"]
    assert d["shape"][4]["reasoning_len"] == 0, "the offending message is visible by shape"
    assert "SECRET" not in files[0].read_text(encoding="utf-8")


def test_no_dir_means_no_file(tmp_path, monkeypatch):
    monkeypatch.delenv("ADK_LLM_DUMP_DIR", raising=False)
    prov = oc.OpenAIProvider(base_url="http://x/v1", api_key="k", default_model="m")
    client = _Client([_Resp(400, "nope")])
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(prov._post_with_retry(client, "http://x/v1/chat/completions", PAYLOAD))
    assert not list(tmp_path.glob("*"))
