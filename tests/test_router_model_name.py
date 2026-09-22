"""The router exposes the model name the loop budgets context against."""
from __future__ import annotations

from adk.context_budget import context_limit_for
from adk.llm import LLMRouter


def test_pinned_model_is_visible_and_budgeted():
    r = LLMRouter(model="deepseek-v4-pro")
    assert r.model == "deepseek-v4-pro"
    assert context_limit_for(r.model) == 128_000


def test_switch_backend_moves_the_name(monkeypatch):
    r = LLMRouter(model="bonsai2-27b")
    assert context_limit_for(r.model) == 16_384
    r.switch_backend("deepseek", base_url="https://api.deepseek.com/v1",
                     api_key="k", model="deepseek-v4-pro")
    assert r.model == "deepseek-v4-pro"
    assert context_limit_for(r.model) == 128_000
