"""Tests for adk.code_locator — the amortized-index localization ensemble.

The claims that matter: default OFF (no tool registered, no HTTP), every lane fails
soft to an empty list, an honest miss renders the fall-back-to-grep block, and the
ranking prefers cross-lane agreement. No test touches the network.
"""

import json

import pytest

from adk.code_locator import CodeLocator, locator_enabled, register_locator_tool


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("AITHER_CODE_LOCATOR", "AITHER_CODEGRAPH_URL",
                "AITHER_REPOWISE_URL", "AITHER_REPOWISE_REPO", "AITHER_PROJECT_MAP"):
        monkeypatch.delenv(var, raising=False)


class TestGate:
    def test_disabled_by_default(self):
        assert locator_enabled() is False

    def test_enabled_by_flag(self, monkeypatch):
        monkeypatch.setenv("AITHER_CODE_LOCATOR", "1")
        assert locator_enabled() is True

    def test_enabled_by_configured_url(self, monkeypatch):
        monkeypatch.setenv("AITHER_CODEGRAPH_URL", "https://127.0.0.1:8153/codegraph/search")
        assert locator_enabled() is True

    def test_register_noop_when_disabled(self):
        class FakeAgent:
            pass  # no _tools attr on purpose -- must not even be touched
        assert register_locator_tool(FakeAgent()) == 0

    def test_register_registers_when_enabled(self, monkeypatch):
        monkeypatch.setenv("AITHER_CODE_LOCATOR", "1")
        registered = {}

        class FakeRegistry:
            def register(self, fn, name=None, description=None, **kw):
                registered[name] = fn

        class FakeAgent:
            _tools = FakeRegistry()

        assert register_locator_tool(FakeAgent()) == 1
        assert "locate_code" in registered
        # and the tool itself never raises, even with all lanes unconfigured
        out = registered["locate_code"]("where is anything?")
        assert isinstance(out, str)


class TestLanesFailSoft:
    def test_unconfigured_lanes_are_empty(self):
        loc = CodeLocator()
        assert loc._lane_codegraph("query") == []
        assert loc._lane_repowise("query") == []
        assert loc._lane_map("query") == []

    def test_dead_urls_are_empty_not_raising(self, monkeypatch):
        loc = CodeLocator(
            codegraph_url="http://127.0.0.1:1/nope",
            repowise_url="http://127.0.0.1:1", repowise_repo="x")
        assert loc._lane_codegraph("query") == []
        assert loc._lane_repowise("query") == []

    def test_corrupt_map_is_empty(self, tmp_path):
        p = tmp_path / "map.json"
        p.write_text("{ this is not json", encoding="utf-8")
        loc = CodeLocator(map_path=str(p))
        assert loc._lane_map("query") == []

    def test_honest_miss_block(self):
        r = CodeLocator().localize("anything at all")
        assert r["candidates"] == []
        assert "fall back to grep" in r["block"]


class TestRanking:
    def _loc_with_fakes(self, monkeypatch, cg, dirs):
        loc = CodeLocator(k=5)
        monkeypatch.setattr(loc, "_lane_codegraph", lambda q: cg)
        monkeypatch.setattr(loc, "_lane_repowise", lambda q: [])
        monkeypatch.setattr(loc, "_lane_map", lambda q: dirs)
        return loc

    def test_primary_rank_order_preserved(self, monkeypatch):
        loc = self._loc_with_fakes(
            monkeypatch,
            cg=[("/app/lib/a.py", "A"), ("/app/lib/b.py", "B")],
            dirs=[])
        r = loc.localize("q")
        assert r["candidates"][:2] == ["/app/lib/a.py", "/app/lib/b.py"]

    def test_cross_lane_agreement_boosts(self, monkeypatch):
        # b.py ranks below a.py in CodeGraph, but the map flags b's directory --
        # agreement must lift it above a.
        loc = self._loc_with_fakes(
            monkeypatch,
            cg=[("/app/lib/x/a.py", "A"), ("/app/lib/target/b.py", "B")],
            dirs=[("/app/lib/target", "the relevant subsystem")])
        r = loc.localize("q")
        assert r["candidates"][0] == "/app/lib/target/b.py"

    def test_map_only_hit_still_surfaces_dir(self, monkeypatch):
        loc = self._loc_with_fakes(
            monkeypatch, cg=[], dirs=[("/app/lib/somewhere", "purposeful dir")])
        r = loc.localize("q")
        assert r["candidates"] == ["/app/lib/somewhere"]
        assert "purposeful dir" in r["block"]

    def test_block_is_compact(self, monkeypatch):
        # the whole point: a hit must cost ~hundreds of tokens, not thousands
        cg = [(f"/app/lib/mod{i}.py", "n" * 60) for i in range(10)]
        loc = self._loc_with_fakes(monkeypatch, cg=cg, dirs=[])
        r = loc.localize("q")
        assert len(r["block"]) < 1200  # ~300 tokens ceiling


class TestMapLane:
    def test_keyword_scoring(self, tmp_path):
        p = tmp_path / "map.json"
        p.write_text(json.dumps({"dirs": {
            "/app/lib/audio": {"purpose": "audio mixing and playback"},
            "/app/lib/net": {"purpose": "network transport"},
        }}), encoding="utf-8")
        loc = CodeLocator(map_path=str(p))
        hits = loc._lane_map("where is audio playback mixed?")
        assert hits and hits[0][0] == "/app/lib/audio"
