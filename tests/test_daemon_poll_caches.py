"""The harness daemon's polled reads: cached, never stale, never a 500.

Three measured defects (2026-09-27) pinned here:

* ``GET /decisions?status=all`` answered 500 on a card whose ``answer_note``
  held a lone surrogate (``\\udc9d``): the response could not encode as UTF-8.
* ``/decisions`` and ``/wakes`` re-read and re-parsed every card file and the
  whole awrise ledger on every poll -- the daemon's largest idle CPU cost.
* The context well re-parsed a ~2 MB lease store and re-dialled an unreachable
  fleet engine on every 5 s rebuild.

Every cache below is asserted to see a change on disk, so a speed-up can never
turn into a stale answer.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from adk.decisions import store as store_mod
from adk.decisions.store import (
    DecisionCard,
    DecisionOption,
    DecisionStore,
)

LONE = "\udc9d"


def _bump_mtime(path: Path) -> None:
    """Force a visibly different mtime, whatever the filesystem's resolution."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


# ── decisions store ─────────────────────────────────────────────────────────


def _card(card_id: str = "d-5hqj", **kw) -> DecisionCard:
    return DecisionCard(
        id=card_id, title="Pick one", default_key="a",
        options=[DecisionOption(key="a", label="Plan A"), DecisionOption(key="b", label="Plan B")],
        **kw,
    )


def _poison(path: Path) -> None:
    """Rewrite a card file the way the measured one was: an escaped lone surrogate."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["status"] = "answered"
    raw["answer"] = "a"
    raw["answer_note"] = "â" + LONE + "¯ [Image #1]"
    path.write_text(json.dumps(raw), encoding="utf-8")  # ensure_ascii -> "\udc9d" escape
    _bump_mtime(path)


class TestDecisionStoreSurrogates:
    def test_a_poisoned_card_reads_back_encodable(self, tmp_path: Path) -> None:
        store = DecisionStore(tmp_path)
        store.create(_card())
        _poison(tmp_path / "d-5hqj.json")
        assert "\\udc9d" in (tmp_path / "d-5hqj.json").read_text(encoding="utf-8")

        cards = store.list(status=None)
        assert len(cards) == 1
        note = cards[0].answer_note or ""
        assert LONE not in note and "[Image #1]" in note
        json.dumps(cards[0].to_dict(), ensure_ascii=False).encode("utf-8")

    def test_a_write_never_persists_a_lone_surrogate(self, tmp_path: Path) -> None:
        store = DecisionStore(tmp_path)
        card = _card()
        card.answer_note = f"bad{LONE}"
        store.create(card)
        text = (tmp_path / "d-5hqj.json").read_text(encoding="utf-8")
        assert "\\udc9d" not in text.lower()


class TestDecisionStoreCache:
    def test_a_rewrite_on_disk_is_seen(self, tmp_path: Path) -> None:
        store = DecisionStore(tmp_path)
        store.create(_card())
        assert store.list()[0].title == "Pick one"
        path = tmp_path / "d-5hqj.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["title"] = "Pick one, edited by hand"
        path.write_text(json.dumps(raw), encoding="utf-8")
        _bump_mtime(path)
        assert store.list()[0].title == "Pick one, edited by hand"

    def test_a_deleted_card_disappears_and_leaves_the_cache(self, tmp_path: Path) -> None:
        store = DecisionStore(tmp_path)
        store.create(_card())
        store.create(_card("d-7bky"))
        assert len(store.list()) == 2
        (tmp_path / "d-7bky.json").unlink()
        assert [c.id for c in store.list()] == ["d-5hqj"]
        assert "d-7bky.json" not in store_mod._RAW_CACHE.get(str(tmp_path), {})

    def test_a_returned_card_is_a_copy(self, tmp_path: Path) -> None:
        store = DecisionStore(tmp_path)
        store.create(_card())
        first = store.list()[0]
        first.title = "mutated in memory"
        first.options.clear()
        again = store.list()[0]
        assert again.title == "Pick one" and len(again.options) == 2

    def test_closed_filter_still_returns_cards_that_expire_on_read(self, tmp_path: Path) -> None:
        store = DecisionStore(tmp_path)
        store.create(_card(deadline=time.time() - 1))
        expired = store.list(status="expired")
        assert [c.id for c in expired] == ["d-5hqj"]
        assert expired[0].answer == "a"

    def test_an_unchanged_card_is_not_reparsed(self, tmp_path: Path, monkeypatch) -> None:
        store = DecisionStore(tmp_path)
        store.create(_card())
        store.list()
        calls = []
        real = json.loads

        def counting(text, *a, **k):
            calls.append(1)
            return real(text, *a, **k)

        monkeypatch.setattr(store_mod.json, "loads", counting)
        for _ in range(3):
            assert len(store.list()) == 1
        assert calls == [], "an unchanged card file must be served from the cache"


class TestDecisionsEndpointSurrogate:
    def test_status_all_is_200_with_a_poisoned_card(self, tmp_path: Path, monkeypatch) -> None:
        from fastapi.testclient import TestClient

        folder = tmp_path / "decisions"
        monkeypatch.setenv("AITHER_DECISIONS_DIR", str(folder))
        monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
        monkeypatch.setenv("AITHER_HARNESS_TOKEN", "test-token-for-testing-only")
        monkeypatch.setattr(store_mod, "_STORE", None, raising=False)
        from adk.harnesses.daemon import create_app

        DecisionStore(folder).create(_card())
        _poison(folder / "d-5hqj.json")
        client = TestClient(create_app())
        client.headers = {"Authorization": "Bearer test-token-for-testing-only"}
        for status in ("all", "answered"):
            resp = client.get(f"/decisions?status={status}")
            assert resp.status_code == 200, resp.text
            assert resp.json()["count"] == 1


# ── wakes ledger ────────────────────────────────────────────────────────────


def _row(job: str, event: str, wake: str, ts: str) -> str:
    return json.dumps({"job": job, "event": event, "wake_id": wake, "ts": ts}) + "\n"


class TestWakesLedgerCache:
    @pytest.fixture
    def home(self, tmp_path: Path) -> Path:
        (tmp_path / "ledger").mkdir()
        (tmp_path / "jobs.json").write_text(json.dumps({"schema": 2, "jobs": {
            "nightly": {"run": "echo hi", "enabled": True, "interval_s": 60},
        }}), encoding="utf-8")
        return tmp_path

    def test_appended_rows_are_seen(self, home: Path) -> None:
        from adk import wakes

        day = home / "ledger" / "2026-09-27.jsonl"
        day.write_text(_row("nightly", "started", "w-1", "2026-09-27T10:00:00+00:00"),
                       encoding="utf-8")
        snap = wakes.snapshot(home)
        assert snap["running"] == 1
        with open(day, "a", encoding="utf-8") as fh:
            fh.write(_row("nightly", "finished", "w-1", "2026-09-27T10:01:00+00:00"))
        _bump_mtime(day)
        assert wakes.snapshot(home)["running"] == 0
        assert wakes.read_ledger(home)["count"] == 2

    def test_a_half_written_line_is_skipped_then_counted_once_complete(self, home: Path) -> None:
        from adk import wakes

        day = home / "ledger" / "2026-09-27.jsonl"
        full = _row("nightly", "started", "w-2", "2026-09-27T11:00:00+00:00")
        day.write_text(full[:20], encoding="utf-8")
        first = wakes.read_ledger(home)
        assert first["count"] == 0 and first["skipped"] == 1
        with open(day, "a", encoding="utf-8") as fh:
            fh.write(full[20:])
        _bump_mtime(day)
        second = wakes.read_ledger(home)
        assert second["count"] == 1 and second["skipped"] == 0

    def test_a_rewritten_shorter_file_is_reparsed(self, home: Path) -> None:
        from adk import wakes

        day = home / "ledger" / "2026-09-27.jsonl"
        day.write_text(_row("nightly", "started", "w-3", "2026-09-27T12:00:00+00:00") * 3,
                       encoding="utf-8")
        assert wakes.read_ledger(home)["count"] == 3
        day.write_text(_row("nightly", "finished", "w-3", "2026-09-27T12:01:00+00:00"),
                       encoding="utf-8")
        _bump_mtime(day)
        rows = wakes.read_ledger(home)["rows"]
        assert [r["event"] for r in rows] == ["finished"]


# ── context well ────────────────────────────────────────────────────────────


class TestWellCaches:
    def test_lease_store_change_is_seen(self, tmp_path: Path, monkeypatch) -> None:
        from adk.harnesses import well

        monkeypatch.setenv("VCS_DATA_ROOT", str(tmp_path))
        store = tmp_path / "leases.json"

        def write(n: int) -> None:
            store.write_text(json.dumps({"leases": [
                {"lease_id": f"l{i}", "status": "active", "target": f"f{i}",
                 "expires_ts": "2999-01-01T00:00:00+00:00"} for i in range(n)
            ]}), encoding="utf-8")
            _bump_mtime(store)

        write(1)
        assert well.lease_state()["count"] == 1
        write(3)
        assert well.lease_state()["count"] == 3

    def test_fleet_briefing_backs_off_after_a_failure(self, monkeypatch) -> None:
        from adk.harnesses import well

        calls: list[int] = []

        def failing(timeout: float = 3.0):
            calls.append(1)
            return {"ok": False, "reason": "ConnectionRefusedError"}

        monkeypatch.setattr(well, "fleet_briefing", failing)
        cw = well.ContextWell(session_lister=lambda: [])
        first = cw._fleet()
        second = cw._fleet()
        assert len(calls) == 1, "a failed fleet must not be re-dialled inside the backoff"
        assert first["ok"] is False and second["ok"] is False and "retry_at" in second

        monkeypatch.setattr(well, "fleet_briefing",
                            lambda timeout=3.0: {"ok": True, "briefing": "b"})
        cw._fleet_next_try = 0.0  # backoff elapsed
        assert cw._fleet()["ok"] is True
        assert cw._fleet_failures == 0


# ── spool tailer, heartbeat, node link ──────────────────────────────────────


class _Room:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, event: dict) -> None:
        self.events.append(event)


class _Registry:
    def __init__(self, room: _Room) -> None:
        self.room = room

    def get_or_create(self, name: str) -> _Room:
        return self.room


class TestSpoolScandir:
    def test_appends_are_published_without_a_stat_per_file(self, tmp_path: Path,
                                                             monkeypatch) -> None:
        from adk.harnesses import spool

        room = _Room()
        tailer = spool.SpoolTailer(registry=_Registry(room), directory=tmp_path)
        for i in range(5):
            (tmp_path / f"s{i}.jsonl").write_text("", encoding="utf-8")
        (tmp_path / "ignored.txt").write_text("x\n", encoding="utf-8")
        assert tailer.drain_once() == 0  # first sight: resume at the end

        stats: list[str] = []
        real_stat = Path.stat

        def counting_stat(self, *a, **k):
            stats.append(str(self))
            return real_stat(self, *a, **k)

        monkeypatch.setattr(Path, "stat", counting_stat)
        with open(tmp_path / "s3.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"room": "main", "type": "message"}) + "\n")
        assert tailer.drain_once() == 1
        assert room.events and room.events[0]["type"] == "message"
        per_file = [p for p in stats if p.endswith(".jsonl")]
        assert per_file == [], "sizes must come from the directory listing"


class TestHeartbeatClientReuse:
    def test_one_client_serves_every_beat(self, monkeypatch) -> None:
        import asyncio

        import httpx
        from adk import enrollment

        made: list[int] = []
        posts: list[str] = []

        class _Resp:
            status_code = 200

            @staticmethod
            def json() -> dict:
                return {"status": "ok"}

        class _Client:
            def __init__(self, *a, **k) -> None:
                made.append(1)

            async def post(self, url: str, **k):
                posts.append(url)
                return _Resp()

            async def aclose(self) -> None:
                return None

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        monkeypatch.setattr(enrollment, "build_registration", lambda node_id, **k: {
            "inference_ready": False, "available_models": [], "gpu_vram_mb": 0,
            "inference_url": "", "inference_kind": "",
        })
        asyncio.run(enrollment.heartbeat_loop("https://example.invalid", "t", "n",
                                              interval=0, max_beats=3))
        assert len(posts) == 3
        assert made == [1], "a client (and its SSL context) per beat is the CPU leak"


class TestNodeLinkSslContext:
    def test_the_context_is_built_once(self) -> None:
        from adk import node_link

        assert node_link._client_ssl_context() is node_link._client_ssl_context()


# ── session directory pending-tool cache, well git TTL ──────────────────────


class TestPendingToolCache:
    def test_a_pending_tool_ages_without_rereading_the_transcript(
            self, tmp_path: Path, monkeypatch) -> None:
        from adk.harnesses import session_directory as sd

        started = time.time() - 10
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(started)) + "Z"
        line = {"type": "assistant", "timestamp": stamp, "message": {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "t1", "name": "Bash"}]}}
        transcript = tmp_path / "s.jsonl"
        transcript.write_text(json.dumps(line) + "\n", encoding="utf-8")

        first = sd._derive_status_from_transcript(str(transcript))
        assert first[0] == "working" and "Bash" in first[2]

        def boom(*a, **k):
            raise AssertionError("an unchanged transcript must not be re-read")

        monkeypatch.setattr(sd, "_derive_status_uncached", boom)
        real_time = time.time
        monkeypatch.setattr(sd.time, "time",
                            lambda: real_time() + sd.PENDING_TOOL_BLOCKED_SECONDS + 5)
        aged = sd._derive_status_from_transcript(str(transcript))
        assert aged[0] == "blocked?", "age must still turn working into blocked?"

    def test_a_result_arriving_is_seen(self, tmp_path: Path) -> None:
        from adk.harnesses import session_directory as sd

        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"
        transcript = tmp_path / "s.jsonl"
        use = {"type": "assistant", "timestamp": stamp, "message": {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "t1", "name": "Bash"}]}}
        transcript.write_text(json.dumps(use) + "\n", encoding="utf-8")
        assert sd._derive_status_from_transcript(str(transcript))[0] == "working"
        result = {"type": "user", "timestamp": stamp, "message": {
            "content": [{"type": "tool_result", "tool_use_id": "t1"}]}}
        done = {"type": "system", "subtype": "turn_duration", "timestamp": stamp}
        with open(transcript, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(result) + "\n" + json.dumps(done) + "\n")
        _bump_mtime(transcript)
        assert sd._derive_status_from_transcript(str(transcript))[0] == "waiting-input"


class TestWellGitTtl:
    def test_git_is_reused_inside_the_interval_and_rerun_after(self, monkeypatch) -> None:
        from adk.harnesses import well

        calls: list[str] = []
        monkeypatch.setattr(well, "git_state",
                            lambda root: calls.append(root) or {"ok": True, "branch": "b"})
        cw = well.ContextWell(session_lister=lambda: [])
        now = 1000.0
        cw._git("/repo", now)
        cw._git("/repo", now + well.GIT_REFRESH_INTERVAL - 1)
        assert calls == ["/repo"]
        cw._git("/repo", now + well.GIT_REFRESH_INTERVAL + 1)
        assert calls == ["/repo", "/repo"]
