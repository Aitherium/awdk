"""Tests for the adk structured_ml tool domain (TabFM + TimesFM).

Covers: the domain is wired into TOOL_CATEGORIES + the analyst identity, the new
capability exists, and each tool shapes its HTTP request to the AitherStructuredML
service correctly (mocked transport — no live service required).
"""

from __future__ import annotations

import json

import httpx
import pytest

import adk.builtin_tools as bt
from adk.core.capability import Capability


# ── wiring ───────────────────────────────────────────────────────────────────


def test_structured_ml_category_holds_the_three_tools():
    fns = bt.TOOL_CATEGORIES["structured_ml"]
    names = {f.__name__ for f in fns}
    assert names == {
        "tabular_classify", "tabular_regress", "timeseries_forecast", "tabular_teach"
    }


def test_analyst_identity_enables_structured_ml():
    assert "structured_ml" in bt.IDENTITY_DEFAULTS["analyst"]


def test_non_data_identities_do_not_get_structured_ml():
    # The domain is opt-in — a generic reviewer identity must NOT get it by default.
    assert "structured_ml" not in bt.IDENTITY_DEFAULTS["hydra"]


def test_structured_inference_capability_exists():
    assert Capability.STRUCTURED_INFERENCE.value == "structured_inference"


# ── request shaping (mocked transport) ───────────────────────────────────────


def _mock_httpx(monkeypatch, handler):
    # builtin_tools does `import httpx` INSIDE each tool, resolving to this same
    # global module object — so patching httpx.Client here is what the tool sees.
    real_client = httpx.Client  # capture BEFORE patching to avoid self-recursion

    def factory(*_args, **_kwargs):
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "Client", factory)


def test_tabular_classify_posts_expected_body(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"predictions": ["a"], "classes": ["a", "b"]})

    _mock_httpx(monkeypatch, handler)
    out = json.loads(
        bt.tabular_classify(
            support_rows=[{"f": 1, "label": "a"}],
            target="label",
            query_rows=[{"f": 2}],
        )
    )
    assert out["predictions"] == ["a"]
    assert seen["url"].endswith("/tabular/classify")
    assert seen["body"]["target"] == "label"
    assert seen["body"]["support_rows"] == [{"f": 1, "label": "a"}]
    assert seen["body"]["query_rows"] == [{"f": 2}]


def test_timeseries_forecast_posts_expected_body(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"point_forecast": [[1.0, 2.0]], "horizon": 2})

    _mock_httpx(monkeypatch, handler)
    out = json.loads(bt.timeseries_forecast(series=[1.0, 2.0, 3.0], horizon=2))
    assert out["horizon"] == 2
    assert seen["url"].endswith("/timeseries/forecast")
    assert seen["body"] == {"series": [1.0, 2.0, 3.0], "horizon": 2}


def test_tabular_teach_posts_to_genesis(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"promoted": True, "accuracy": 0.9})

    _mock_httpx(monkeypatch, handler)
    out = json.loads(
        bt.tabular_teach(task="lead-scoring", labeled_rows=[{"x": 1, "y": "a"}], target="y")
    )
    assert out["promoted"] is True
    # teach is STATEFUL/tenant-scoped → goes to Genesis /ml/teach, not the service.
    assert seen["url"].endswith("/ml/teach")
    assert seen["body"]["task"] == "lead-scoring" and seen["body"]["mode"] == "classify"


def test_tabular_regress_posts_expected_body(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"predictions": [3.14], "n_query": 1})

    _mock_httpx(monkeypatch, handler)
    out = json.loads(
        bt.tabular_regress(
            support_rows=[{"x": 1, "price": 10.0}], target="price", query_rows=[{"x": 2}]
        )
    )
    assert out["predictions"] == [3.14]
    assert seen["url"].endswith("/tabular/regress")
    assert seen["body"]["target"] == "price"


def test_tool_schema_lists_are_arrays_not_strings():
    # Regression guard: bare `list` hints derive {"type":"string"} and break the
    # tool schema — the params MUST be arrays so an LLM sends rows, not a string.
    from adk.tools import _extract_parameters

    props = _extract_parameters(bt.tabular_classify)["properties"]
    assert props["support_rows"]["type"] == "array"
    assert props["query_rows"]["type"] == "array"
    assert _extract_parameters(bt.timeseries_forecast)["properties"]["series"]["type"] == "array"


def test_large_response_is_summarised(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"predictions": ["c"] * 20000})

    _mock_httpx(monkeypatch, handler)
    out = json.loads(bt.tabular_classify([{"f": 1, "label": "c"}], "label", [{"f": 2}]))
    assert out.get("truncated") is True
    assert out["n_predictions"] == 20000
    assert len(out["predictions_sample"]) == 50


def test_tool_surfaces_service_error_as_json(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "target column missing"})

    _mock_httpx(monkeypatch, handler)
    out = json.loads(bt.tabular_regress([{"a": 1}], "label", [{"a": 2}]))
    assert "error" in out
    assert "target column missing" in out["error"]


def test_url_scheme_guard_rejects_non_http(monkeypatch):
    # A mis-set env with a dangerous scheme must fall back to the default, not be used.
    monkeypatch.setenv("AITHER_STRUCTURED_ML_URL", "file:///etc/passwd")
    assert bt._structured_ml_url() == bt._STRUCTURED_ML_DEFAULT_URL
    monkeypatch.setenv("AITHER_STRUCTURED_ML_URL", "http://custom-host:9999")
    assert bt._structured_ml_url() == "http://custom-host:9999"


def test_tool_surfaces_transport_error_as_json(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    _mock_httpx(monkeypatch, handler)
    out = json.loads(bt.timeseries_forecast([1.0, 2.0], 2))
    assert "error" in out
