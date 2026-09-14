"""
AitherSaga Writer — Terminal Writing Environment
==================================================

A Textual-based TUI for writing novels connected to AitherSaga workspaces.

Layout:
    ┌──────────┬──────────────────────┬─────────────┐
    │ OUTLINE  │ EDITOR               │ CONTEXT     │
    │          │                      │ Characters  │
    │ chapters │ prose editing area   │ Lorebook    │
    │ + scenes │                      │ AI Assist   │
    ├──────────┴──────────────────────┴─────────────┤
    │ NORMAL │ word count │ streak │ Ctrl+? help    │
    └──────────────────────────────────────────────────┘

Usage:
    # From AitherShell REPL
    /saga write <project_id>

    # Direct launch
    python -m aithershell.tui.saga_writer <project_id>

    # With custom Saga URL
    python -m aithershell.tui.saga_writer <project_id> --url https://localhost:8793
"""

import argparse
from adk._tls import tls_verify
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, ScrollableContainer
    from textual.widgets import (
        Header, Footer, Static, TextArea, Tree, Label, RichLog,
    )
    from textual.widgets.tree import TreeNode
    from textual.css.query import NoMatches
    TEXTUAL_AVAILABLE = True
except ImportError:
    TEXTUAL_AVAILABLE = False


def _default_saga_url() -> str:
    return os.environ.get(
        "AITHERSAGA_URL",
        os.environ.get("SAGA_URL", "https://localhost:8793"),
    )


# =============================================================================
# SAGA API CLIENT (lightweight, for TUI use)
# =============================================================================


class SagaClient:
    """Minimal async client for AitherSaga API."""

    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _headers(self) -> Dict[str, str]:
        h: Dict[str, str] = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def get(self, path: str) -> Dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=self._headers(),
            timeout=15, verify=tls_verify(),
        ) as c:
            resp = await c.get(path)
            resp.raise_for_status()
            return resp.json()

    async def post(self, path: str, data: Dict[str, Any] = None) -> Dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=self._headers(),
            timeout=120, verify=tls_verify(),
        ) as c:
            resp = await c.post(path, json=data or {})
            resp.raise_for_status()
            return resp.json()

    async def put(self, path: str, data: Dict[str, Any] = None) -> Dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=self._headers(),
            timeout=15, verify=tls_verify(),
        ) as c:
            resp = await c.put(path, json=data or {})
            resp.raise_for_status()
            return resp.json()


# =============================================================================
# TUI APPLICATION
# =============================================================================

if TEXTUAL_AVAILABLE:

    class SagaWriter(App):
        """Terminal writing environment for AitherSaga."""

        TITLE = "AitherSaga Writer"

        CSS = """
        #main-container {
            height: 1fr;
        }
        #outline-panel {
            width: 28;
            border-right: solid $surface-lighten-2;
            overflow-y: auto;
        }
        #outline-tree {
            width: 1fr;
        }
        #editor-panel {
            width: 1fr;
        }
        #editor {
            width: 1fr;
            height: 1fr;
        }
        #context-panel {
            width: 32;
            border-left: solid $surface-lighten-2;
            overflow-y: auto;
            padding: 1;
        }
        .context-section {
            margin-bottom: 1;
        }
        .context-header {
            text-style: bold;
            color: $accent;
            margin-bottom: 0;
        }
        .context-body {
            color: $text-muted;
        }
        #status-info {
            dock: bottom;
            height: 1;
            background: $surface;
            color: $text-muted;
            padding: 0 1;
        }
        """

        BINDINGS = [
            Binding("ctrl+s", "save", "Save", priority=True),
            Binding("ctrl+q", "quit_app", "Quit", priority=True),
            Binding("ctrl+n", "next_scene", "Next Scene"),
            Binding("ctrl+p", "prev_scene", "Prev Scene"),
            Binding("ctrl+shift+n", "new_scene", "New Scene"),
            Binding("ctrl+f", "toggle_focus", "Focus Mode"),
            Binding("f1", "show_help", "Help"),
        ]

        def __init__(self, project_id: str, saga_url: str = "",
                     token: str = "", **kwargs):
            super().__init__(**kwargs)
            self.project_id = project_id
            self.client = SagaClient(saga_url or _default_saga_url(), token)

            # State
            self.project: Dict[str, Any] = {}
            self.manuscript: Dict[str, Any] = {}
            self.current_chapter_id: Optional[str] = None
            self.current_scene_id: Optional[str] = None
            self.current_scene_version: int = 0
            self.dirty = False
            self.focus_mode = False

            # Flattened scene list for navigation
            self._scene_list: List[Dict[str, str]] = []

        def compose(self) -> ComposeResult:
            yield Header()
            with Horizontal(id="main-container"):
                with Vertical(id="outline-panel"):
                    yield Tree("Manuscript", id="outline-tree")
                with Vertical(id="editor-panel"):
                    yield TextArea(id="editor", language="markdown",
                                   show_line_numbers=True, tab_size=4)
                with Vertical(id="context-panel"):
                    yield Static("", id="context-chars",
                                 classes="context-section")
                    yield Static("", id="context-location",
                                 classes="context-section")
                    yield Static("", id="context-lore",
                                 classes="context-section")
            yield Static("Loading...", id="status-info")
            yield Footer()

        async def on_mount(self) -> None:
            """Load project and manuscript on startup."""
            try:
                self.project = await self.client.get(
                    f"/projects/{self.project_id}"
                )
                self.manuscript = await self.client.get(
                    f"/projects/{self.project_id}/manuscript"
                )
            except Exception as e:
                self.query_one("#status-info", Static).update(
                    f"[red]Failed to load project: {e}[/red]"
                )
                return

            self._build_outline()
            self._build_scene_list()

            # Open first scene
            if self._scene_list:
                first = self._scene_list[0]
                await self._load_scene(first["chapter_id"], first["scene_id"])
            else:
                self._update_status("No chapters yet. Press Ctrl+Shift+N to create one.")

        # ── Outline Tree ──────────────────────────────────────────────

        def _build_outline(self) -> None:
            tree = self.query_one("#outline-tree", Tree)
            tree.clear()

            title = (self.project.get("name")
                     or self.project.get("title", "Untitled"))
            self.sub_title = title

            chapters = self.manuscript.get("chapters", [])
            current_act = 0

            for ch in chapters:
                act = ch.get("act", 1)
                if act != current_act:
                    current_act = act
                    tree.root.add(f"[bold]Act {act}[/bold]")

                ch_label = ch.get("title") or f"Chapter {ch.get('order', 0) + 1}"
                wc = ch.get("word_count", 0)
                ch_node = tree.root.add(
                    f"{ch_label} [dim]({wc}w)[/dim]",
                    data={"type": "chapter", "id": ch["id"]},
                )

                for sc in ch.get("scenes", []):
                    sc_label = sc.get("title") or f"Scene {sc.get('order', 0) + 1}"
                    sc_wc = sc.get("word_count", 0)
                    ch_node.add_leaf(
                        f"{sc_label} [dim]({sc_wc}w)[/dim]",
                        data={"type": "scene", "chapter_id": ch["id"],
                              "id": sc["id"]},
                    )
                ch_node.expand()
            tree.root.expand()

        def _build_scene_list(self) -> None:
            self._scene_list = []
            for ch in self.manuscript.get("chapters", []):
                for sc in ch.get("scenes", []):
                    self._scene_list.append({
                        "chapter_id": ch["id"],
                        "scene_id": sc["id"],
                        "chapter_title": ch.get("title", ""),
                        "scene_title": sc.get("title", ""),
                    })

        # ── Scene Loading & Saving ────────────────────────────────────

        async def _load_scene(self, chapter_id: str, scene_id: str) -> None:
            if self.dirty:
                await self._save_current()

            try:
                scene = await self.client.get(
                    f"/projects/{self.project_id}/manuscript"
                    f"/chapters/{chapter_id}/scenes/{scene_id}"
                )
            except Exception as e:
                self._update_status(f"[red]Failed to load scene: {e}[/red]")
                return

            self.current_chapter_id = chapter_id
            self.current_scene_id = scene_id
            self.current_scene_version = scene.get("version", 1)

            editor = self.query_one("#editor", TextArea)
            editor.load_text(scene.get("content", ""))
            self.dirty = False

            self._update_context(scene)
            self._update_status()

        async def _save_current(self) -> None:
            if not self.current_chapter_id or not self.current_scene_id:
                return

            editor = self.query_one("#editor", TextArea)
            content = editor.text

            try:
                result = await self.client.put(
                    f"/projects/{self.project_id}/manuscript"
                    f"/chapters/{self.current_chapter_id}"
                    f"/scenes/{self.current_scene_id}",
                    data={
                        "content": content,
                        "version": self.current_scene_version,
                    },
                )
                self.current_scene_version = result.get("version",
                                                         self.current_scene_version + 1)
                self.dirty = False
                self._update_status()
            except Exception as e:
                self._update_status(f"[red]Save failed: {e}[/red]")

        # ── Context Panel ─────────────────────────────────────────────

        def _update_context(self, scene: Dict[str, Any]) -> None:
            # Characters
            char_ids = scene.get("character_ids", [])
            pov = scene.get("pov_character_id")
            if pov and pov not in char_ids:
                char_ids = [pov] + char_ids

            chars_text = "[bold cyan]Characters[/bold cyan]\n"
            all_chars = self.project.get("characters", [])
            for c in all_chars:
                if c.get("id") in char_ids:
                    marker = "(POV) " if c.get("id") == pov else ""
                    chars_text += f"  {marker}{c.get('name', '?')}\n"
            if not char_ids:
                chars_text += "  [dim]none assigned[/dim]\n"

            try:
                self.query_one("#context-chars", Static).update(chars_text)
            except NoMatches:
                pass

            # Location
            loc_id = scene.get("location_id")
            loc_text = "[bold cyan]Location[/bold cyan]\n"
            if loc_id:
                for loc in self.project.get("locations", []):
                    if loc.get("id") == loc_id:
                        loc_text += f"  {loc.get('name', '?')}\n"
                        if loc.get("atmosphere"):
                            loc_text += f"  [dim]{loc['atmosphere'][:80]}[/dim]\n"
                        break
            else:
                loc_text += "  [dim]none[/dim]\n"

            try:
                self.query_one("#context-location", Static).update(loc_text)
            except NoMatches:
                pass

            # Lorebook (show always-active entries)
            lore_text = "[bold cyan]Lorebook[/bold cyan]\n"
            lore_entries = self.project.get("lorebook", [])
            shown = 0
            for entry in lore_entries:
                if entry.get("enabled") and (
                    entry.get("always_active") or shown < 5
                ):
                    lore_text += f"  {entry.get('name', '?')}\n"
                    shown += 1
            if shown == 0:
                lore_text += "  [dim]no entries[/dim]\n"

            try:
                self.query_one("#context-lore", Static).update(lore_text)
            except NoMatches:
                pass

        # ── Status Bar ────────────────────────────────────────────────

        def _update_status(self, message: str = "") -> None:
            if message:
                try:
                    self.query_one("#status-info", Static).update(message)
                except NoMatches:
                    pass
                return

            editor = self.query_one("#editor", TextArea)
            wc = len(editor.text.split()) if editor.text.strip() else 0
            dirty_marker = " [yellow]*[/yellow]" if self.dirty else ""

            # Find current scene info
            scene_label = ""
            for item in self._scene_list:
                if (item["chapter_id"] == self.current_chapter_id
                        and item["scene_id"] == self.current_scene_id):
                    ch = item["chapter_title"] or "Chapter"
                    sc = item["scene_title"] or "Scene"
                    scene_label = f"{ch} / {sc}"
                    break

            total_wc = self.manuscript.get("total_word_count", 0)
            goals = self.manuscript.get("goals", {})
            daily = goals.get("daily_word_target", 1000)
            streak = goals.get("streak_days", 0)

            status = (
                f"{scene_label}{dirty_marker}  |  "
                f"scene: {wc}w  |  total: {total_wc}w  |  "
                f"goal: {daily}w/day  |  streak: {streak}d  |  "
                f"Ctrl+S save  F1 help"
            )

            try:
                self.query_one("#status-info", Static).update(status)
            except NoMatches:
                pass

        # ── Event Handlers ────────────────────────────────────────────

        async def on_text_area_changed(self, event: TextArea.Changed) -> None:
            self.dirty = True
            self._update_status()

        async def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
            data = event.node.data
            if not data:
                return
            if data.get("type") == "scene":
                await self._load_scene(data["chapter_id"], data["id"])
            elif data.get("type") == "chapter":
                # Expand/collapse
                if event.node.is_expanded:
                    event.node.collapse()
                else:
                    event.node.expand()

        # ── Actions ───────────────────────────────────────────────────

        async def action_save(self) -> None:
            await self._save_current()

        async def action_quit_app(self) -> None:
            if self.dirty:
                await self._save_current()
            self.exit()

        async def action_next_scene(self) -> None:
            idx = self._current_scene_index()
            if idx is not None and idx + 1 < len(self._scene_list):
                nxt = self._scene_list[idx + 1]
                await self._load_scene(nxt["chapter_id"], nxt["scene_id"])

        async def action_prev_scene(self) -> None:
            idx = self._current_scene_index()
            if idx is not None and idx > 0:
                prev = self._scene_list[idx - 1]
                await self._load_scene(prev["chapter_id"], prev["scene_id"])

        async def action_new_scene(self) -> None:
            if not self.current_chapter_id:
                # Create a chapter first
                try:
                    ch = await self.client.post(
                        f"/projects/{self.project_id}/manuscript/chapters",
                        data={"title": "Chapter 1"},
                    )
                    self.current_chapter_id = ch["id"]
                except Exception as e:
                    self._update_status(f"[red]Failed to create chapter: {e}[/red]")
                    return

            try:
                sc = await self.client.post(
                    f"/projects/{self.project_id}/manuscript"
                    f"/chapters/{self.current_chapter_id}/scenes",
                    data={"title": ""},
                )
                # Reload manuscript
                self.manuscript = await self.client.get(
                    f"/projects/{self.project_id}/manuscript"
                )
                self._build_outline()
                self._build_scene_list()
                await self._load_scene(self.current_chapter_id, sc["id"])
            except Exception as e:
                self._update_status(f"[red]Failed to create scene: {e}[/red]")

        async def action_toggle_focus(self) -> None:
            self.focus_mode = not self.focus_mode
            try:
                outline = self.query_one("#outline-panel")
                context = self.query_one("#context-panel")
                outline.display = not self.focus_mode
                context.display = not self.focus_mode
            except NoMatches:
                pass

        async def action_show_help(self) -> None:
            self._update_status(
                "Ctrl+S save | Ctrl+N/P next/prev scene | "
                "Ctrl+Shift+N new scene | Ctrl+F focus | Ctrl+Q quit"
            )

        # ── Helpers ───────────────────────────────────────────────────

        def _current_scene_index(self) -> Optional[int]:
            for i, item in enumerate(self._scene_list):
                if (item["chapter_id"] == self.current_chapter_id
                        and item["scene_id"] == self.current_scene_id):
                    return i
            return None


# =============================================================================
# CLI ENTRY POINT
# =============================================================================


def main():
    if not TEXTUAL_AVAILABLE:
        print("Error: textual is required for the Saga Writer TUI.")
        print("Install it: pip install textual>=0.86.0")
        sys.exit(1)

    if httpx is None:
        print("Error: httpx is required for the Saga Writer TUI.")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="AitherSaga Writer")
    parser.add_argument("project_id", help="Saga project ID to open")
    parser.add_argument("--url", default="", help="AitherSaga API URL")
    parser.add_argument("--token", default="", help="Auth token")
    args = parser.parse_args()

    app = SagaWriter(
        project_id=args.project_id,
        saga_url=args.url,
        token=args.token,
    )
    app.run()


if __name__ == "__main__":
    main()
