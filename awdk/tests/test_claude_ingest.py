"""Tests for adk.shell.claude_ingest (session → brain ingest) and the pure
logic of adk.shell.session_browser. No real ~/.claude, no GraphMemory disk
writes, no processes."""

import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from adk.shell import claude_ingest as ci
from adk.shell import claude_sessions as cs

UUID_A = "11111111-2222-3333-4444-555555555555"
UUID_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _entries(cwd, title, prompt, reply):
    now = datetime.now(timezone.utc).isoformat()
    return [
        {"type": "user", "cwd": cwd, "gitBranch": "main", "timestamp": now,
         "message": {"role": "user", "content": prompt}},
        {"type": "assistant", "cwd": cwd, "timestamp": now,
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": reply}]}},
        {"type": "ai-title", "aiTitle": title},
        {"type": "last-prompt", "lastPrompt": prompt},
    ]


def _write_journal(root, project, sid, entries):
    proj = root / project
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / f"{sid}.jsonl"
    f.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return f


@pytest.fixture
def fake_graph(monkeypatch):
    """Stub adk.graph_memory so local storage is captured, not written."""
    stored = []

    class _Graph:
        def __init__(self, agent_name="default"):
            pass

        async def add_node(self, **kwargs):
            stored.append(kwargs)

    mod = types.ModuleType("adk.graph_memory")
    mod.GraphMemory = _Graph
    monkeypatch.setitem(sys.modules, "adk.graph_memory", mod)
    return stored


@pytest.fixture
def workdir(tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    root = tmp_path / "projects"
    state = tmp_path / "state.json"
    return root, str(cwd), state


async def test_ingest_creates_chunks_and_advances_watermark(workdir, fake_graph):
    root, cwd, state = workdir
    f = _write_journal(root, "p", UUID_A,
                       _entries(cwd, "My session", "how do I dedupe compose files",
                                "Use one canonical file. " * 20))
    res = await ci.ingest_sessions(days=0, projects_root=root, brain_sync=False,
                                   state_path=state, min_new_bytes=1)
    assert res.sessions_ingested == 1
    assert res.chunks_created >= 1
    assert len(fake_graph) == res.chunks_created
    node = fake_graph[0]
    assert node["metadata"]["source"] == f"claude-session:{UUID_A}"
    assert node["metadata"]["session_title"] == "My session"
    assert "how do I dedupe compose files" in node["content"]

    # Second run with no journal growth: nothing new.
    fake_graph.clear()
    res2 = await ci.ingest_sessions(days=0, projects_root=root, brain_sync=False,
                                    state_path=state, min_new_bytes=1)
    assert res2.chunks_created == 0
    assert fake_graph == []

    # Append a new turn: only the delta is ingested.
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "cwd": cwd,
                             "message": {"role": "user",
                                         "content": "brand new follow-up question"}}) + "\n")
    res3 = await ci.ingest_sessions(days=0, projects_root=root, brain_sync=False,
                                    state_path=state, min_new_bytes=1)
    assert res3.chunks_created >= 1
    joined = " ".join(n["content"] for n in fake_graph)
    assert "brand new follow-up question" in joined
    assert "how do I dedupe compose files" not in joined  # not re-ingested


async def test_ingest_skips_secret_chunks(workdir, fake_graph):
    root, cwd, state = workdir
    secret = "sk-" + "a1B2" * 8
    _write_journal(root, "p", UUID_A,
                   _entries(cwd, "Leaky", f"my key is {secret}", "noted"))
    res = await ci.ingest_sessions(days=0, projects_root=root, brain_sync=False,
                                   state_path=state, min_new_bytes=1)
    assert res.chunks_skipped_secrets >= 1
    assert all(secret not in n["content"] for n in fake_graph)


async def test_ingest_dry_run_persists_nothing(workdir, fake_graph):
    root, cwd, state = workdir
    _write_journal(root, "p", UUID_A, _entries(cwd, "T", "question " * 30, "answer"))
    res = await ci.ingest_sessions(days=0, projects_root=root, brain_sync=False,
                                   state_path=state, dry_run=True, min_new_bytes=1)
    assert res.chunks_created >= 1
    assert fake_graph == []
    assert not state.exists()


async def test_ingest_truncated_journal_resets(workdir, fake_graph):
    root, cwd, state = workdir
    f = _write_journal(root, "p", UUID_A, _entries(cwd, "T", "original text here", "ok"))
    await ci.ingest_sessions(days=0, projects_root=root, brain_sync=False,
                             state_path=state, min_new_bytes=1)
    fake_graph.clear()
    # Journal rewritten smaller (compaction) — watermark must reset, not seek past EOF.
    _write_journal(root, "p", UUID_A, _entries(cwd, "T", "rewritten", "ok"))
    res = await ci.ingest_sessions(days=0, projects_root=root, brain_sync=False,
                                   state_path=state, min_new_bytes=1)
    assert res.chunks_created >= 1
    assert any("rewritten" in n["content"] for n in fake_graph)


def test_extract_new_text_header_and_roles(workdir):
    root, cwd, _ = workdir
    f = _write_journal(root, "p", UUID_A, _entries(cwd, "T", "the question", "the answer"))
    meta = cs.parse_session_meta(f)
    text, end = ci.extract_new_text(meta, 0)
    assert text.startswith("Claude Code session: T")
    assert "[you" in text and "[claude" in text
    assert "the question" in text and "the answer" in text
    assert end > 0


# ─── browser pure logic ──────────────────────────────────────────────────────

def _metas(n=3):
    out = []
    for i in range(n):
        out.append(cs.SessionMeta(
            id=f"{i}" * 8, file="", title=f"Session {i}",
            cwd=f"C:/proj{i}", branch="main",
            last_prompt=f"prompt about topic{i}",
            when=datetime.now(timezone.utc),
        ))
    return out


def test_browser_state_filter_and_selection():
    from adk.shell.session_browser import _State
    st = _State()
    st.sessions = _metas(3)
    assert len(st.visible()) == 3
    st.filter_text = "topic1"
    vis = st.visible()
    assert [s.title for s in vis] == ["Session 1"]
    st.selected = 99
    assert st.current().title == "Session 1"   # clamped
    st.filter_text = "no-match-xyz"
    assert st.visible() == []
    assert st.current() is None


def test_browser_render_smoke(tmp_path, monkeypatch):
    """Render functions must produce formatted-text tuples without a terminal."""
    from adk.shell import session_browser as sb
    monkeypatch.setattr(cs, "CRASH_SNAPSHOT", tmp_path / "crash.json")
    st = sb._State()
    st.sessions = _metas(2)
    # Point one session at a real journal so the preview pane has content.
    root = tmp_path / "projects"
    f = _write_journal(root, "p", UUID_A,
                       _entries(str(tmp_path), "T", "preview question", "preview answer"))
    st.sessions[0].file = str(f)
    for fragments in (sb._render_header(st), sb._render_list(st), sb._render_preview(st)):
        assert isinstance(fragments, list)
        assert all(isinstance(t, tuple) and len(t) == 2 for t in fragments)
    text = "".join(frag for _, frag in sb._render_preview(st))
    assert "preview question" in text and "preview answer" in text


@pytest.mark.skipif(not __import__("importlib").util.find_spec("prompt_toolkit"),
                    reason="prompt_toolkit not installed")
def test_browser_app_constructs_headless():
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from adk.shell import session_browser as sb
    st = sb._State()
    st.sessions = _metas(1)
    with create_pipe_input() as pipe:
        app = sb._build_app(st, _input=pipe, _output=DummyOutput())
        assert app.full_screen
