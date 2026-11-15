"""`adk catalog` -- the awdk door of the unified catalog.

Failing-first coverage for the Set C contract:
  * the reader works with no network and no gateway (>=200 items, person fields),
  * a curated bundle's members all resolve to real entries,
  * a free first-party install plans with no network and no signature,
  * the discovery source feeds `adk discover query` with a receipt of its own,
  * parity with the platform's `lib.catalog` reader is asserted by
    the platform's `test_catalog_parity.py`.

The catalog is read from the checkout that contains this test file. The arms that
need the monorepo's pack shelves skip, by name, when the shelves are absent (a pip
install, a sparse checkout); the rest run anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adk import catalog
from adk.discovery import index, scope, sources

REPO = Path(__file__).resolve().parents[2]

#: The arms below count awpacks, resolve bundle members and plan a brain-pack
#: install, so they need the monorepo's pack shelves. A pip install (or a sparse
#: checkout of awdk/ alone) has neither shelf; the arms that read only the
#: shipped data still run there.
_PACK_SHELVES = (REPO / "awpack" / "packs", REPO / "AitherOS" / "Library" / "packs")
needs_pack_shelves = pytest.mark.skipif(
    not all(shelf.is_dir() for shelf in _PACK_SHELVES),
    reason="needs the monorepo pack shelves (awpack/packs, AitherOS/Library/packs)",
)


def _list_json(capsys, *argv: str):
    code = catalog.cli_main(["list", "--json", *argv])
    out = capsys.readouterr().out
    return code, json.loads(out)


@needs_pack_shelves
def test_catalog_list_offline(capsys):
    """No network, no gateway: every person field is present on every entry."""
    code, doc = _list_json(capsys)
    assert code == 0, doc
    assert doc["ok"] is True
    assert doc["count"] >= 200, f"only {doc['count']} entries"
    for item in doc["items"]:
        for field in catalog.PERSON_FIELDS:
            assert field in item, f"{item['id']} missing {field}"
        assert item["what_it_does"], item["id"]
        assert len(item["what_it_does"]) <= 140, item["id"]
        assert item["status"] in ("available", "preview", "blocked", "internal")
    kinds = {i["kind"] for i in doc["items"]}
    for expected in ("agent_pack", "tool_pack", "skill_pack", "application",
                     "brain_pack", "awpack", "bundle"):
        assert expected in kinds, f"no {expected} in the catalog: {sorted(kinds)}"


@needs_pack_shelves
def test_catalog_get_bundle():
    """A bundle's members resolve; a bundle is not a list of dead names."""
    entry = catalog.get_entry("jason-business-desk")
    assert entry is not None, "curated bundle missing from the catalog"
    assert entry["kind"] == "bundle"
    assert entry["includes"], "bundle lists no members"
    for member in entry["includes"]:
        got = catalog.get_entry(member["id"])
        assert got is not None, f"bundle member {member['id']!r} does not resolve"
    assert catalog.get_entry("patty-everyday") is not None


@needs_pack_shelves
def test_install_dry_run_free_pack(monkeypatch):
    """A free first-party pack plans by DIGEST; no signature, no network."""
    monkeypatch.setattr(catalog, "_is_paid", lambda item: False)
    result = catalog.install_entry("brain:aitherium/aither", "workspace", dry_run=True)
    assert result["ok"] is True, result
    assert result["exit_code"] == 0
    assert result["verified_by"] == "digest"
    assert result["plan"], "a dry run must say what it would do"


def test_install_paid_without_signature_refused():
    result = catalog.install_entry("media-forge", "workspace", dry_run=True)
    assert result["ok"] is False, result
    assert result["exit_code"] == 1
    assert "signature" in result["detail"]


def test_discovery_catalog_source(tmp_path, monkeypatch):
    """`adk discover query "film budgeting"` sees the catalog, with a receipt."""
    monkeypatch.setenv("AITHER_DISCOVERY_DIR", str(tmp_path))
    cards, receipt = sources.catalog_source(REPO)
    assert receipt["ok"] is True, receipt
    assert receipt["count"] >= 200, receipt
    hit = [c for c in cards if "budget" in (c["name"] + c["one_line"]).lower()]
    assert hit, "no budget card in the catalog cards"

    caller = scope.Caller(tier="free", fingerprint="test-fp")
    doc = index.build_part("user", REPO, force=True, caller=caller,
                           table=[("catalog_source", sources.catalog_source)])
    assert doc["sources"]["catalog_source"]["ok"] is True
    assert doc["sources"]["catalog_source"]["count"] >= 200
    names = " ".join(c["name"] + " " + c["one_line"] for c in doc["cards"]).lower()
    assert "budget" in names


def test_catalog_json_is_ascii_safe(capsys):
    """`--json` output parses on a cp1252 console: every non-ASCII char escaped."""
    code = catalog.cli_main(["get", "patty-everyday", "--json"])
    out = capsys.readouterr().out
    assert code == 0
    json.loads(out)  # ensure_ascii=True keeps this decode-able in any locale
    assert out.isascii()
