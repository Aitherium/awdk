"""`adk catalog stats` names what each number counts, and a stale index says so.

Measured 2026-11-12: it printed "tools 0, packs 0" from the newest file in the
discovery directory (one directory's part, 15 days old) while ``adk catalog list``
held hundreds of entries, the loader 52 packs and the gateway 23; and
``adk discover query`` printed "0 cards" from an index that did not exist for the
current bearer.
"""
from __future__ import annotations

import json
import time

import pytest

from adk import catalog
from adk.discovery import cli as discover_cli
from adk.discovery import index


def _state(cards, age_s, was="fresh", refreshed=False, error=""):
    return {"merged": {"cards": cards, "parts": {}}, "age_s": age_s, "was": was,
            "refreshed": refreshed, "error": error}


def _gateway(url, bearer=""):
    assert url.endswith("/health/packs")
    return {"packs": {"civic": 18, "rea": 5, "headroom": 0, "untether": 0}, "count": 4}


def test_stats_reports_catalogue_installed_and_gateway_as_three_different_counts():
    cards = [{"domain": "tools", "name": "a"}, {"domain": "tools", "name": "b"},
             {"domain": "skills", "name": "c"}]
    stats = catalog.catalog_stats(gateway_fetch=_gateway, index_state=_state(cards, 12.0))

    listed = catalog.load_catalog()
    assert stats["catalogue"]["entries"] == len(listed) > 0
    # One catalogue source: the totals are the catalogue's, never the index's.
    assert stats["packs_total"] == stats["catalogue"]["packs"] == sum(
        1 for i in listed if i["kind"] in ("agent_pack", "brain_pack", "awpack", "bundle"))
    assert stats["source"] == "catalog"

    assert stats["gateway"]["packs"] == 2 and stats["gateway"]["tools"] == 23
    assert stats["gateway"]["registered_no_tools"] == ["headroom", "untether"]
    assert stats["index"]["cards"] == 3
    assert stats["index"]["by_domain"] == {"tools": 2, "skills": 1}
    assert isinstance(stats["installed"]["packs"], int)

    text = "\n".join(catalog.format_stats(stats))
    for label in ("available in the catalogue:", "installed on this machine:",
                  "loaded in the gateway:", "searchable by an agent here:",
                  "registered but serving no tools (2): headroom, untether"):
        assert label in text, label


def test_a_gateway_that_is_down_is_printed_as_unknown_not_as_zero():
    def down(url, bearer=""):
        raise OSError("connection refused")

    stats = catalog.catalog_stats(gateway_fetch=down, index_state=_state([], 1.0))
    assert stats["gateway"]["packs"] is None
    assert "loaded in the gateway:      unknown (OSError" in "\n".join(catalog.format_stats(stats))


def test_a_stale_index_shows_its_age_and_the_refresh_command():
    stale = _state([], 15 * 86400.0, was="stale")
    assert "stale" in index.describe_age(stale)
    assert "15.0d old" in index.describe_age(stale)
    assert "adk discover build" in index.describe_age(stale)
    assert "no index for this caller" in index.describe_age(_state([], None, was="missing"))
    failed = _state([], 15 * 86400.0, was="stale", error="OSError: gateway down")
    assert "rebuild failed" in index.describe_age(failed)
    text = "\n".join(catalog.format_stats(
        catalog.catalog_stats(gateway_fetch=_gateway, index_state=stale)))
    assert "15.0d old" in text


@pytest.fixture()
def index_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DISCOVERY_DIR", str(tmp_path / "discovery"))
    monkeypatch.setattr(index, "index_dir", lambda: tmp_path / "discovery")
    (tmp_path / "discovery").mkdir()
    return tmp_path


def _write_parts(cwd, built_at, cards):
    for part in ("cwd", "user"):
        path = index.part_path(part, cwd)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": index.SCHEMA, "built_at": built_at,
                                    "cards": cards if part == "user" else []}),
                        encoding="utf-8")


def test_ensure_fresh_rebuilds_a_missing_or_stale_index_and_leaves_a_fresh_one(index_home):
    cwd = str(index_home)
    builds = []

    def builder(c):
        builds.append(c)
        _write_parts(cwd, time.time(), [{"domain": "tools", "name": "t1"}])

    state = index.ensure_fresh(cwd, builder=builder)
    assert state["was"] == "missing" and state["refreshed"] and len(builds) == 1
    assert [c["name"] for c in state["merged"]["cards"]] == ["t1"]

    state = index.ensure_fresh(cwd, builder=builder)
    assert state["was"] == "fresh" and not state["refreshed"] and len(builds) == 1

    _write_parts(cwd, time.time() - 15 * 86400, [])
    assert index.ensure_fresh(cwd, refresh=False, builder=builder)["was"] == "stale"
    assert len(builds) == 1, "refresh=False never builds"
    state = index.ensure_fresh(cwd, builder=builder)
    assert state["was"] == "stale" and state["refreshed"] and len(builds) == 2

    def broken(c):
        raise OSError("gateway down")

    _write_parts(cwd, time.time() - 15 * 86400, [])
    state = index.ensure_fresh(cwd, builder=broken)
    assert not state["refreshed"] and "gateway down" in state["error"]


def test_discover_query_rebuilds_first_and_prints_the_index_age(monkeypatch, capsys):
    seen = {}

    def fake_fresh(cwd=None, refresh=True, **k):
        seen["refresh"] = refresh
        return _state([{"domain": "tools", "name": "git_log", "one_line": "commit history",
                        "how_to_invoke": "x", "hosting": "fleet", "source": "s"}],
                      3.0, was="missing", refreshed=True)

    monkeypatch.setattr(index, "ensure_fresh", fake_fresh)
    rc = discover_cli.main(["query", "commit history", "--no-live"])
    out = capsys.readouterr().out
    assert seen["refresh"] is True
    assert "discovery index: rebuilt just now (it was missing)" in out
    assert rc in (0, 1)
    discover_cli.main(["query", "commit history", "--no-live", "--no-refresh"])
    assert seen["refresh"] is False


def test_a_customer_pack_is_never_labelled_for_everyone():
    """packs_catalog.yaml marks garg ``allowed_tenants`` and jgames ``unlisted``."""
    by_id = {i["id"]: i for i in catalog.load_catalog()}
    for pack_id in ("garg", "jgames"):
        if pack_id in by_id:  # absent from the bundled (public) catalogue by design
            assert by_id[pack_id]["audience"] == ["owner"], pack_id
    restricted = catalog._normalize_pack({"id": "x", "name": "X", "allowed_tenants": ["t"]})
    assert restricted["audience"] == ["owner"]
    assert catalog._normalize_pack({"id": "y", "name": "Y"})["audience"] == ["everyone"]
