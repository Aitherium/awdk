"""Scrolls: a small rotating shelf of things worth adding, offered one click at a time.

AgentCraft's "Skill Scrolls" (https://www.getagentcraft.com/docs/features/skill-scrolls,
proprietary, documented behaviour reimplemented here, no code copied) drop five
collectible suggestions on the map; a hero collects one, a card shows what it is, and
Install or Dismiss resolves it. When all five are resolved a fresh batch of five arrives
after a random 10-20 minutes, drawn from things you do not already have.

Here the "skills.sh" source is our own catalogue (``adk.catalog``: packs, tool packs and
MCP packs that are free and available) and Install is ``adk.catalog.install_entry``, so a
scroll is the same install a person gets from ``adk catalog install`` -- same digest
pin, same refusal of a paid or blocked entry. Nothing here fetches anything itself.

A UI draws the scrolls wherever it likes (a map, a tray, a notification); this module owns
the shelf state, which survives restarts, and the install error vocabulary.
"""
from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

BATCH_SIZE = 5
RESPAWN_RANGE_S = (600.0, 1200.0)          # 10-20 minutes
DEFAULT_KINDS = ("agent_pack", "tool_pack", "mcp_pack", "awpack", "brain_pack")

#: Install failures in the documented words, keyed by what the failure looked like.
ERROR_WORDS = (
    ("network", "Network error: could not reach the catalogue"),
    ("not found", "Not found in the catalogue"),
    ("no catalog entry", "Not found in the catalogue"),
    ("permission", "Permission denied: check folder permissions"),
    ("timed out", "Installation timed out: check the network connection"),
)


def _default_catalog() -> list:
    from adk import catalog
    out: list = []
    for kind in DEFAULT_KINDS:
        out.extend(catalog.search(kind=kind, limit=None))
    return out


def _default_installed() -> set:
    try:
        from adk.addon_lock import installs
        return {str(r.get("id") or r.get("addon_id") or "") for r in installs()} - {""}
    except Exception:  # noqa: BLE001 - an unreadable lock means "nothing known installed"
        return set()


def _default_install(item_id: str) -> dict:
    from adk import catalog
    return catalog.install_entry(item_id)


def explain_failure(detail: str) -> str:
    """Map an install failure to a plain sentence; unknown text passes through."""
    low = (detail or "").lower()
    for needle, words in ERROR_WORDS:
        if needle in low:
            return words
    return detail or "Installation failed"


class ScrollShelf:
    """Five outstanding scrolls, resolved one at a time, restocked after a quiet spell."""

    def __init__(self, state_path: Optional[Path] = None, *,
                 catalog_fn: Optional[Callable[[], Iterable[dict]]] = None,
                 installed_fn: Optional[Callable[[], Iterable[str]]] = None,
                 install_fn: Optional[Callable[[str], dict]] = None,
                 now: Callable[[], float] = time.time,
                 rng: Optional[random.Random] = None,
                 batch_size: int = BATCH_SIZE,
                 respawn_range: tuple = RESPAWN_RANGE_S) -> None:
        self.path = Path(state_path) if state_path else None
        self._catalog = catalog_fn or _default_catalog
        self._installed = installed_fn or _default_installed
        self._install = install_fn or _default_install
        self._now = now
        self._rng = rng or random.Random()
        self.batch_size = batch_size
        self.respawn_range = respawn_range
        self.state: dict = {"outstanding": [], "dismissed": [], "respawn_at": 0.0}
        self._load()

    # ----- persistence -------------------------------------------------------------

    def _load(self) -> None:
        if self.path and self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.state["outstanding"] = [str(i) for i in raw.get("outstanding", [])]
                self.state["dismissed"] = [str(i) for i in raw.get("dismissed", [])]
                self.state["respawn_at"] = float(raw.get("respawn_at", 0.0))
            except (ValueError, OSError, AttributeError, TypeError):
                self.state = {"outstanding": [], "dismissed": [], "respawn_at": 0.0}

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.state), encoding="utf-8")
        os.replace(tmp, self.path)

    # ----- the shelf ---------------------------------------------------------------

    def _eligible(self) -> list:
        have = set(self._installed())
        skip = have | set(self.state["dismissed"]) | set(self.state["outstanding"])
        rows = []
        for item in self._catalog():
            if not isinstance(item, dict) or not item.get("id"):
                continue
            if item["id"] in skip or item.get("status", "available") != "available":
                continue
            if (item.get("pricing") or {}).get("tier", "free") != "free":
                continue
            rows.append(item)
        return sorted(rows, key=lambda i: i["id"])

    def _card(self, item: dict) -> dict:
        return {"id": item["id"], "name": item.get("name") or item["id"],
                "kind": item.get("kind", ""),
                "description": item.get("what_it_does") or item.get("summary", ""),
                "source": item.get("source", "")}

    def _index(self) -> dict:
        return {i["id"]: i for i in self._catalog() if isinstance(i, dict) and i.get("id")}

    def scrolls(self) -> list:
        """The scrolls on the shelf now, restocking first if the quiet spell is over."""
        # Drop outstanding scrolls that were installed elsewhere in the meantime.
        have = set(self._installed())
        self.state["outstanding"] = [i for i in self.state["outstanding"] if i not in have]
        if not self.state["outstanding"] and self._now() >= self.state["respawn_at"]:
            pool = self._eligible()
            self._rng.shuffle(pool)
            self.state["outstanding"] = [i["id"] for i in pool[: self.batch_size]]
            if not self.state["outstanding"]:
                # Nothing left to offer: look again later instead of every call.
                self.state["respawn_at"] = self._now() + self.respawn_range[0]
        self._save()
        index = self._index()
        return [self._card(index[i]) for i in self.state["outstanding"] if i in index]

    def _resolve(self, scroll_id: str, dismiss: bool) -> bool:
        if scroll_id not in self.state["outstanding"]:
            return False
        self.state["outstanding"].remove(scroll_id)
        if dismiss:
            self.state["dismissed"].append(scroll_id)
        if not self.state["outstanding"]:
            self.state["respawn_at"] = self._now() + self._rng.uniform(*self.respawn_range)
        self._save()
        return True

    def collect(self, scroll_id: str) -> Optional[dict]:
        """The card a hero reads on arrival, or None for a scroll that is not on the shelf."""
        if scroll_id not in self.state["outstanding"]:
            return None
        item = self._index().get(scroll_id)
        return self._card(item) if item else None

    def dismiss(self, scroll_id: str) -> bool:
        return self._resolve(scroll_id, dismiss=True)

    def install(self, scroll_id: str) -> dict:
        """Install the scroll's entry; the scroll is resolved only when the install worked."""
        if scroll_id not in self.state["outstanding"]:
            return {"ok": False, "id": scroll_id, "error": "That scroll is not on the shelf"}
        try:
            result = self._install(scroll_id)
        except Exception as exc:  # noqa: BLE001 - reported to the person in plain words
            return {"ok": False, "id": scroll_id, "error": explain_failure(str(exc))}
        if not result.get("ok"):
            return {"ok": False, "id": scroll_id,
                    "error": explain_failure(str(result.get("detail") or ""))}
        self._resolve(scroll_id, dismiss=False)
        return {"ok": True, "id": scroll_id, "plan": result.get("plan", []),
                "digest": result.get("digest", "")}

    def seconds_until_restock(self) -> float:
        if self.state["outstanding"]:
            return 0.0
        return max(0.0, self.state["respawn_at"] - self._now())
