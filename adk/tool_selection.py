"""Which tool schemas a turn ships: a small core, plus ``load_tools`` for the rest.

Why (measured 2026-09-27 on the :9001 daemon): ``_filter_tools_by_intent`` FAILS OPEN
— an unclassified turn (intent None/"" or the heuristic's neutral ``DEFAULT``, which is
what ``coarse_code_intent`` returns for most real input) shipped EVERY registered
schema. A trivial "reply ok" carried 55 built-in schemas (~5.2k tokens by the
chars/4 estimate MicroScheduler uses; ~91 schemas on the live daemon with its gateway
tools attached) to a model whose whole window is 8,192 tokens.

The fix is not to hide tools. Hiding them is its own measured defect (2026-08-22,
agent.py ``_intent_matches_categories``): a capability filtered out of the prompt is
indistinguishable from one that does not exist, and the agent told the owner it had no
web search while ``web_search`` was registered. So an unclassified turn gets:

* a CORE set — read/list/write/edit files, run a shell command, search/fetch the web,
  and the gateway's ``search_tools``/``call_tool`` pair when attached; and
* ``load_tools(category)`` — one meta-tool whose DESCRIPTION names every other
  category and how many tools it holds, so nothing is invisible. Calling it adds that
  category's schemas to the rest of the turn.

Registered tools stay callable by name either way: execution reads the registry, not
the offered list. ``ADK_TOOL_SELECTION=all`` restores the old ship-everything path.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

LOAD_TOOLS_NAME = "load_tools"

#: Offered on every unclassified turn. Chosen by measured schema cost (~730 tokens
#: for the eight built-ins) against what a general assistant needs without asking:
#: the file surface, one shell, the web. Everything else is one ``load_tools`` away.
CORE_TOOL_NAMES: frozenset[str] = frozenset({
    "file_read", "file_list", "file_write", "file_edit",
    "shell_exec",
    "web_search", "web_fetch",
    # Gateway meta-tools (registered by adk.server when the MCP gateway attaches):
    # together they reach the whole ~1,200-tool platform catalogue on demand.
    "search_tools", "call_tool",
})

#: Intents that mean "the classifier could not tell". Only these get the core set;
#: a real classification (CODE, CONVERSATION, research, ...) keeps its own filter.
UNCLASSIFIED_INTENTS: frozenset[str] = frozenset({"", "default"})

#: Name-prefix categories for tools that are not in builtin TOOL_CATEGORIES.
_PREFIX_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("self_", "self"),
)
#: Anything else registered on the agent (gateway eager tools, packs, app tools).
OTHER_CATEGORY = "platform"


def selection_mode() -> str:
    """``core`` (default) or ``all`` — from ``ADK_TOOL_SELECTION``."""
    raw = os.environ.get("ADK_TOOL_SELECTION", "core").strip().lower()
    return "all" if raw in ("all", "off", "0", "false", "legacy") else "core"


def is_unclassified(intent_type: str | None) -> bool:
    """True when the intent carries no signal (None, empty, or DEFAULT)."""
    return (intent_type or "").strip().lower() in UNCLASSIFIED_INTENTS


def _builtin_category_map() -> dict[str, str]:
    """tool name -> builtin category, read LIVE (several categories fill lazily)."""
    try:
        from adk.builtin_tools import TOOL_CATEGORIES
    except Exception:  # noqa: BLE001 — a missing builtin table only coarsens categories
        return {}
    out: dict[str, str] = {}
    for cat, fns in TOOL_CATEGORIES.items():
        for fn in fns or []:
            name = getattr(fn, "__name__", None)
            if name and name not in out:
                out[name] = cat
    return out


def categorize(tools: Iterable[Any]) -> dict[str, list[Any]]:
    """Group ToolDefs by category, in registration order. Core tools are excluded."""
    builtin = _builtin_category_map()
    groups: dict[str, list[Any]] = {}
    for td in tools:
        name = getattr(td, "name", "")
        if not name or name in CORE_TOOL_NAMES or name == LOAD_TOOLS_NAME:
            continue
        cat = builtin.get(name)
        if cat is None:
            cat = next((c for pfx, c in _PREFIX_CATEGORIES if name.startswith(pfx)),
                       OTHER_CATEGORY)
        groups.setdefault(cat, []).append(td)
    return groups


def load_tools_schema(groups: dict[str, list[Any]]) -> dict:
    """The OpenAI function schema for ``load_tools``, naming every loadable category."""
    listing = ", ".join(f"{cat} ({len(tds)})" for cat, tds in groups.items())
    return {
        "type": "function",
        "function": {
            "name": LOAD_TOOLS_NAME,
            "description": (
                "Load more tools for this task. Only a core set is loaded; call this "
                "with a category to add its tools, then call them. Categories: "
                + (listing or "(none)")
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "One category name, or 'all'.",
                    },
                },
                "required": ["category"],
            },
        },
    }


class TurnToolSelection:
    """Per-turn tool offer. Local to one ``chat()`` call — never shared state.

    Args:
        tools: every ToolDef registered on the agent.
        intent_type: the turn's classified intent (None/""/DEFAULT = unclassified).
        filter_fn: the existing intent filter, applied when the turn IS classified.
    """

    def __init__(self, tools: list[Any], intent_type: str | None, filter_fn,
                 mode: str | None = None) -> None:
        self._all = list(tools)
        self.loaded: list[str] = []
        # ``mode`` is the AGENT's own choice and beats the env default: a curated
        # agent whose real tools are not in the core (Agent Home's remind_me /
        # follow_up / receipts) passes "all", or a small local model never loads
        # them and answers "I set a reminder" with nothing on disk.
        chosen = (mode or selection_mode()).strip().lower()
        self.active = chosen == "core" and is_unclassified(intent_type)
        if not self.active:
            self._offered = list(filter_fn(self._all, intent_type))
            self._groups: dict[str, list[Any]] = {}
            return
        self._offered = [t for t in self._all if getattr(t, "name", "") in CORE_TOOL_NAMES]
        self._groups = categorize(self._all)

    @property
    def offered(self) -> list[Any]:
        return list(self._offered)

    @property
    def categories(self) -> dict[str, int]:
        """Still-loadable categories and their tool counts."""
        return {cat: len(tds) for cat, tds in self._groups.items()}

    def meta_schema(self) -> dict:
        """The ``load_tools`` schema for the categories still loadable."""
        return load_tools_schema(self._groups)

    def schemas(self, to_openai) -> list[dict] | None:
        """The ``tools=`` payload: offered schemas (+ ``load_tools`` while any remain)."""
        out = list(to_openai(self._offered)) if self._offered else []
        if self.active and self._groups:
            out.append(self.meta_schema())
        return out or None

    def load(self, arguments: dict | None) -> str:
        """Execute ``load_tools``: move a category's schemas into the offer."""
        if not self.active:
            return "All tools for this turn are already loaded."
        cat = str((arguments or {}).get("category") or "").strip().lower()
        if not cat:
            return "load_tools needs a category. Available: " + self._listing()
        if cat == "all":
            picked = [c for c in self._groups]
        elif cat in self._groups:
            picked = [cat]
        else:
            return f"Unknown category '{cat}'. Available: " + self._listing()
        added: list[str] = []
        for c in picked:
            for td in self._groups.pop(c):
                self._offered.append(td)
                added.append(td.name)
            self.loaded.append(c)
        return (f"Loaded {len(added)} tool(s) from {', '.join(picked)}: "
                + ", ".join(added) + ". Call them directly now.")

    def _listing(self) -> str:
        return ", ".join(f"{c} ({n})" for c, n in self.categories.items()) or "(none)"


__all__ = [
    "CORE_TOOL_NAMES",
    "LOAD_TOOLS_NAME",
    "TurnToolSelection",
    "categorize",
    "is_unclassified",
    "load_tools_schema",
    "selection_mode",
]
