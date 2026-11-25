"""Scrolls: a five-item shelf over the catalogue that restocks after a quiet spell."""
from __future__ import annotations

import random
from pathlib import Path

from adk import scrolls as s


def _items(n, **over):
    rows = []
    for i in range(n):
        row = {"id": f"pack-{i:02d}", "kind": "agent_pack", "name": f"Pack {i}",
               "what_it_does": f"does thing {i}", "status": "available",
               "pricing": {"tier": "free"}, "source": "first_party"}
        row.update(over)
        rows.append(row)
    return rows


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _shelf(tmp_path, items, installed=(), install=None, clock=None, **kw):
    clock = clock or Clock()
    shelf = s.ScrollShelf(tmp_path / "shelf.json", catalog_fn=lambda: items,
                          installed_fn=lambda: set(installed),
                          install_fn=install or (lambda i: {"ok": True, "plan": ["x"],
                                                            "digest": "d"}),
                          now=clock, rng=random.Random(7), **kw)
    return shelf, clock


def test_five_scrolls_from_free_available_uninstalled_entries(tmp_path: Path):
    items = (_items(8) + _items(1, id="paid", pricing={"tier": "paid"}) +
             _items(1, id="blocked", status="blocked"))
    shelf, _ = _shelf(tmp_path, items, installed={"pack-00"})
    cards = shelf.scrolls()
    ids = {c["id"] for c in cards}
    assert len(cards) == 5 and not ids & {"paid", "blocked", "pack-00"}
    assert shelf.scrolls() == cards                      # stable until something resolves


def test_collect_shows_the_card_and_unknown_scroll_is_none(tmp_path: Path):
    shelf, _ = _shelf(tmp_path, _items(6))
    first = shelf.scrolls()[0]
    card = shelf.collect(first["id"])
    assert card["description"].startswith("does thing") and card["name"].startswith("Pack")
    assert shelf.collect("nope") is None


def test_restock_waits_ten_to_twenty_minutes_after_the_last_one_resolves(tmp_path: Path):
    shelf, clock = _shelf(tmp_path, _items(12))
    for card in shelf.scrolls():
        assert shelf.install(card["id"])["ok"]
    assert shelf.scrolls() == []                         # shelf empty, quiet spell on
    wait = shelf.seconds_until_restock()
    assert 600.0 <= wait <= 1200.0
    clock.t += 599
    assert shelf.scrolls() == []
    clock.t += 1200
    fresh = shelf.scrolls()
    assert len(fresh) == 5 and fresh                     # new batch of five
    first_batch_done = set(shelf.state["dismissed"])
    assert not {c["id"] for c in fresh} & first_batch_done


def test_dismissed_scrolls_never_come_back_and_install_failure_keeps_the_scroll(tmp_path: Path):
    calls = []

    def flaky(item_id):
        calls.append(item_id)
        return {"ok": False, "detail": "Network error while fetching"}

    shelf, _ = _shelf(tmp_path, _items(7), install=flaky)
    a, b = [c["id"] for c in shelf.scrolls()[:2]]
    assert shelf.dismiss(a) is True and shelf.dismiss(a) is False
    bad = shelf.install(b)
    assert bad == {"ok": False, "id": b,
                   "error": "Network error: could not reach the catalogue"}
    assert b in shelf.state["outstanding"]               # still collectable, retry works
    assert shelf.install("zzz")["error"] == "That scroll is not on the shelf"
    assert a in shelf.state["dismissed"]


def test_state_survives_a_restart_and_corrupt_state_resets(tmp_path: Path):
    shelf, clock = _shelf(tmp_path, _items(9))
    ids = [c["id"] for c in shelf.scrolls()]
    shelf.dismiss(ids[0])
    again, _ = _shelf(tmp_path, _items(9), clock=clock)
    assert [c["id"] for c in again.scrolls()] == ids[1:]
    (tmp_path / "shelf.json").write_text("{ nope", encoding="utf-8")
    fresh, _ = _shelf(tmp_path, _items(9))
    assert len(fresh.scrolls()) == 5


def test_exception_in_install_is_explained_and_empty_catalogue_backs_off(tmp_path: Path):
    def boom(_):
        raise TimeoutError("operation timed out")

    shelf, clock = _shelf(tmp_path, _items(6), install=boom)
    sid = shelf.scrolls()[0]["id"]
    assert "timed out" in shelf.install(sid)["error"]
    empty, clock2 = _shelf(tmp_path / "e", [])
    assert empty.scrolls() == []
    assert empty.seconds_until_restock() >= 600.0


def test_installed_elsewhere_drops_off_the_shelf(tmp_path: Path):
    items = _items(7)
    inst: set = set()
    shelf = s.ScrollShelf(tmp_path / "x.json", catalog_fn=lambda: items,
                          installed_fn=lambda: inst, install_fn=lambda i: {"ok": True},
                          now=Clock(), rng=random.Random(1))
    ids = [c["id"] for c in shelf.scrolls()]
    inst.add(ids[0])
    assert ids[0] not in [c["id"] for c in shelf.scrolls()]


def test_explain_failure_vocabulary():
    assert s.explain_failure("Permission denied: /x").startswith("Permission denied")
    assert s.explain_failure("no catalog entry 'zz'") == "Not found in the catalogue"
    assert s.explain_failure("weird") == "weird" and s.explain_failure("") == \
        "Installation failed"
