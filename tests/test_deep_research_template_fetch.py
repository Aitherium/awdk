"""The deep-research template's fetch_url must not cite an error page as a source.

adk.webfetch returns a 403/404 with its body still in ``text`` (Wikimedia's 403 body
is its robot policy). The template returned that body as page text whenever
``text`` was non-empty, so the agent read and cited "Please respect our robot
policy" as research. The platform ``deep_research`` pack loads this engine.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import sys
from pathlib import Path

import adk
from adk import webfetch

_PKG = "_dr_template_engine_test"


def _tools_module():
    eng = Path(adk.__file__).parent / "templates" / "deep-research" / "engine"
    if _PKG not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            _PKG, eng / "__init__.py", submodule_search_locations=[str(eng)])
        pkg = importlib.util.module_from_spec(spec)
        sys.modules[_PKG] = pkg
        spec.loader.exec_module(pkg)  # type: ignore[union-attr]
    return importlib.import_module(f"{_PKG}.tools"), importlib.import_module(f"{_PKG}.ledger")


def _fetch_url(tmp_path):
    tools, ledger = _tools_module()
    session = tools.ResearchSession(graph=None, ledger=ledger.SavingsLedger(),
                                    artifacts_dir=tmp_path)
    fns = {f.__name__: f for f in tools.build_research_tools(session)}
    return fns["fetch_url"], session


def test_blocked_page_is_an_error_not_text(tmp_path, monkeypatch):
    async def fake(url, **_k):
        return webfetch.FetchResult(url=url, status=403, engine="httpx",
                                    text="Please respect our robot policy",
                                    error="blocked (HTTP 403)")

    monkeypatch.setattr(webfetch, "fetch", fake)
    fetch_url, session = _fetch_url(tmp_path)
    out = json.loads(asyncio.run(fetch_url("https://en.wikipedia.org/wiki/X")))
    assert "error" in out and "text" not in out, out
    assert "https://en.wikipedia.org/wiki/X" not in session._page_cache


def test_a_real_page_is_returned(tmp_path, monkeypatch):
    async def fake(url, **_k):
        return webfetch.FetchResult(url=url, status=200, engine="httpx", text="body")

    monkeypatch.setattr(webfetch, "fetch", fake)
    fetch_url, _ = _fetch_url(tmp_path)
    out = json.loads(asyncio.run(fetch_url("https://example.com/")))
    assert out.get("text") == "body", out
