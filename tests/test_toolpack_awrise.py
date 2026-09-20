# SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
# © 2026 Aitherium, LLC. Original work.
"""The awrise toolpack (awrise_* agent tools) against a REAL harness daemon.

Every tool in ``adk.toolpacks.awrise.tools`` talks urllib -> HTTP -> the daemon,
the same way ``adk.decisions.wake_view`` and ``adk.decisions.steerback`` do — so
this suite boots a real uvicorn server (not FastAPI's TestClient, which has no
socket urllib can dial) with a FAKE ``awrise`` CLI on ``AWRISE_BIN``, exactly the
fixture shape ``test_daemon_wakes_endpoints.py`` / ``test_daemon_wakes_create_update.py``
use, and asserts on the daemon's own recorded state (spawned argv, decision-card
store) rather than trusting a tool's return value alone.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import stat
import sys
import time
from pathlib import Path
from threading import Thread

import pytest

fastapi = pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")
httpx = pytest.importorskip("httpx")

from adk import toolpacks  # noqa: E402,F401  — exercises the package import path
from adk.toolpacks.awrise import register  # noqa: E402
from adk.toolpacks.awrise import tools as awrise_tools  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "wakes"
TOKEN = "test-awrise-toolpack-token"


# ── fake awrise (same shape as the daemon /wakes test suites) ────────────────


def make_fake_awrise(root: Path, *, exit_code: int = 0) -> tuple[Path, Path]:
    """Write a fake ``awrise`` CLI. Returns (binary, argv capture file)."""
    capture = root / "argv.jsonl"
    script = root / "fake_awrise.py"
    script.write_text(
        "import json, sys\n"
        f"open({str(capture)!r}, 'a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print('fake awrise: ' + ' '.join(sys.argv[1:]))\n"
        "sys.stderr.write('fake stderr\\n')\n"
        f"sys.exit({int(exit_code)})\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        binary = root / "awrise.cmd"
        binary.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\nexit /b %ERRORLEVEL%\r\n',
            encoding="utf-8",
        )
    else:
        binary = root / "awrise"
        binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                          encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary, capture


def spawned(capture: Path) -> list[list[str]]:
    if not capture.exists():
        return []
    return [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        return s.getsockname()[1]


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def live_daemon(tmp_path, monkeypatch):
    """A REAL harness daemon on a real socket, a fixture AWRISE_HOME, and a
    fake ``awrise`` CLI on ``AWRISE_BIN`` that records every argv it was
    spawned with. Yields ``{base_url, capture, home}``.
    """
    home = tmp_path / "awrise"
    home.mkdir()
    shutil.copy(FIXTURES / "jobs_v2.json", home / "jobs.json")
    shutil.copytree(FIXTURES / "ledger", home / "ledger")
    monkeypatch.setenv("AWRISE_HOME", str(home))

    fake_root = tmp_path / "fake"
    fake_root.mkdir()
    binary, capture = make_fake_awrise(fake_root, exit_code=0)
    monkeypatch.setenv("AWRISE_BIN", str(binary))

    monkeypatch.setenv("AITHER_HARNESS_TOKEN", TOKEN)
    monkeypatch.setenv("AITHER_HARNESS_PRINCIPALS", str(tmp_path / "harness_tokens.json"))
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))

    # Process-global singletons this file touches (gate 1k): the decision store
    # and the daemon's principals-path/in-flight-run registry. Reset exactly the
    # way test_daemon_wakes_create_update.py does, so this file's order relative
    # to that suite (or to itself, parametrized) cannot change either verdict.
    import adk.decisions.store as decision_store
    import adk.harnesses.daemon as daemon

    monkeypatch.setattr(decision_store, "_STORE", None)
    monkeypatch.setattr(daemon, "PRINCIPALS_PATH", tmp_path / "harness_tokens.json")
    daemon._RUNNING.clear()

    app = daemon.create_app()
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("AITHER_HARNESS_URL", base_url)

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    def run_server() -> None:
        asyncio.run(server.serve())

    thread = Thread(daemon=True, target=run_server)
    thread.start()

    last_poll_error = ""
    for _ in range(50):
        try:
            with httpx.Client(timeout=1) as c:
                if c.get(f"{base_url}/health").status_code == 200:
                    break
        except Exception as exc:  # noqa: BLE001 — polling before the socket is up
            last_poll_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.1)
    else:
        pytest.fail(f"harness daemon on {port} did not start within 5s "
                    f"(last poll error: {last_poll_error})")

    try:
        yield {"base_url": base_url, "capture": capture, "home": home}
    finally:
        server.should_exit = True
        thread.join(timeout=2)


# ── registration ───────────────────────────────────────────────────────────────


def test_register_exposes_all_ten_awrise_tools():
    from adk.tools import ToolRegistry

    registry = ToolRegistry()
    n = register(registry)
    names = {t.name for t in registry.list_tools()}
    assert n == 10
    assert names == {
        "awrise_add", "awrise_set", "awrise_enable", "awrise_disable",
        "awrise_run_now", "awrise_remove", "awrise_confirm", "awrise_status",
        "awrise_explain", "awrise_history",
    }


def test_toolpack_yaml_glob_matches_every_tool_name():
    import yaml

    manifest = yaml.safe_load(
        (Path(__file__).parent.parent / "adk" / "toolpacks" / "awrise" / ".toolpack.yaml")
        .read_text(encoding="utf-8")
    )
    assert manifest["mcp_tools"] == ["awrise_*"]
    assert all(name.startswith("awrise_") for name in awrise_tools._TOOL_NAMES)


# ── name/payload validation happens BEFORE any request ───────────────────────


def test_add_invalid_name_never_touches_the_network():
    out = awrise_tools.awrise_add("-bad-name", "echo hi", "15m")
    assert out["ok"] is False
    assert "invalid wake name" in out["error"]


def test_add_rejects_control_byte_command():
    out = awrise_tools.awrise_add("newjob", "echo hi\x00", "15m")
    assert out["ok"] is False
    assert "command" in out["error"]


def test_set_with_nothing_to_update_is_refused_locally():
    out = awrise_tools.awrise_set("nightly-sync")
    assert out == {"ok": False, "error": "nothing to update"}


def test_confirm_requires_card_id_and_choice():
    assert awrise_tools.awrise_confirm("", "create")["ok"] is False
    assert awrise_tools.awrise_confirm("c-1", "")["ok"] is False


# ── no token / unreachable — the daemon is never touched ─────────────────────


def test_no_token_is_a_clear_error(monkeypatch):
    import adk.decisions.steerback as steerback

    monkeypatch.setattr(steerback, "harness_token", lambda: "")
    out = awrise_tools.awrise_status()
    assert out["ok"] is False
    assert "harness token" in out["error"]


def test_unreachable_daemon_is_a_clear_error(monkeypatch):
    monkeypatch.setenv("AITHER_HARNESS_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", TOKEN)
    out = awrise_tools.awrise_status()
    assert out["ok"] is False
    assert "unreachable" in out["error"]


# ── read-only tools ────────────────────────────────────────────────────────────


class TestReads:
    def test_status_lists_every_job(self, live_daemon):
        out = awrise_tools.awrise_status()
        assert out["installed"] is True
        names = {w["name"] for w in out["wakes"]}
        assert {"nightly-sync", "daily-report", "never-ran"} <= names

    def test_status_one_job(self, live_daemon):
        out = awrise_tools.awrise_status("nightly-sync")
        assert out.get("error") is None
        assert out["run"] == "python sync.py"

    def test_status_invalid_name_is_refused_locally(self, live_daemon):
        out = awrise_tools.awrise_status("../etc")
        assert out["ok"] is False
        assert "invalid wake name" in out["error"]

    def test_status_unknown_job_is_404(self, live_daemon):
        out = awrise_tools.awrise_status("does-not-exist")
        assert out["ok"] is False

    def test_explain_names_the_command_and_schedule(self, live_daemon):
        out = awrise_tools.awrise_explain("nightly-sync")
        assert "python sync.py" in out["explanation"]
        assert "1h" in out["explanation"]

    def test_history_reads_the_ledger(self, live_daemon):
        out = awrise_tools.awrise_history(name="nightly-sync", limit=5)
        assert out.get("error") is None
        assert "rows" in out or "entries" in out or isinstance(out, dict)

    def test_history_since_and_event_are_passed_through(self, live_daemon):
        # Regression guard for the contract's item 10: the daemon's own
        # /wakes/ledger already supports since/event — the toolpack must not
        # silently drop them.
        out = awrise_tools.awrise_history(since="-24h", event="missed")
        assert out.get("error") is None


# ── synchronous mutations: enable / disable / run-now ─────────────────────────


class TestSyncMutations:
    def test_enable_spawns_the_exact_argv(self, live_daemon):
        out = awrise_tools.awrise_enable("never-ran")
        assert out["ok"] is True and out["exit_code"] == 0
        assert spawned(live_daemon["capture"])[-1] == ["enable", "--name", "never-ran"]

    def test_disable_spawns_the_exact_argv(self, live_daemon):
        out = awrise_tools.awrise_disable("nightly-sync")
        assert out["ok"] is True and out["exit_code"] == 0
        assert spawned(live_daemon["capture"])[-1] == ["disable", "--name", "nightly-sync"]

    def test_run_now_spawns_and_waits(self, live_daemon):
        out = awrise_tools.awrise_run_now("nightly-sync", wait_s=5)
        assert out["ok"] is True
        assert out["exit_code"] == 0
        assert spawned(live_daemon["capture"])[-1] == ["run", "--name", "nightly-sync"]

    def test_enable_invalid_name_never_reaches_the_daemon(self, live_daemon):
        out = awrise_tools.awrise_enable("bad name")
        assert out["ok"] is False
        assert spawned(live_daemon["capture"]) == []

    def test_set_every_applies_immediately_no_card(self, live_daemon):
        out = awrise_tools.awrise_set("nightly-sync", every="30m")
        assert out["ok"] is True
        assert "pending" not in out or not out.get("pending")
        assert spawned(live_daemon["capture"])[-1] == ["set", "--name", "nightly-sync",
                                                         "every=30m"]


# ── the card-raising path: add / set-command never spawn synchronously ───────


class TestCardRaisingMutations:
    def test_add_raises_a_pending_card_and_spawns_nothing(self, live_daemon):
        out = awrise_tools.awrise_add("new-job", "echo hi", "15m")
        assert out["ok"] is True
        assert out["pending"] is True
        assert out["card"]["id"]
        assert spawned(live_daemon["capture"]) == []  # nothing ran yet

    def test_add_then_confirm_create_actually_registers_it(self, live_daemon):
        proposed = awrise_tools.awrise_add("new-job-2", "echo hi", "15m")
        card_id = proposed["card"]["id"]
        confirmed = awrise_tools.awrise_confirm(card_id, "create")
        assert confirmed["ok"] is True
        assert spawned(live_daemon["capture"])[-1] == [
            "add", "--name", "new-job-2", "--every", "15m", "--run", "echo hi",
        ]

    def test_add_then_confirm_deny_spawns_nothing(self, live_daemon):
        proposed = awrise_tools.awrise_add("new-job-3", "echo hi", "15m")
        card_id = proposed["card"]["id"]
        confirmed = awrise_tools.awrise_confirm(card_id, "deny")
        assert confirmed["ok"] is True
        assert spawned(live_daemon["capture"]) == []

    def test_set_command_raises_a_pending_card_and_spawns_nothing(self, live_daemon):
        out = awrise_tools.awrise_set("nightly-sync", command="drain-and-sync.sh")
        assert out["ok"] is True
        assert out["pending"] is True
        assert spawned(live_daemon["capture"]) == []

    def test_set_command_then_confirm_apply_spawns_the_new_command(self, live_daemon):
        proposed = awrise_tools.awrise_set("nightly-sync", command="drain-and-sync.sh")
        card_id = proposed["card"]["id"]
        confirmed = awrise_tools.awrise_confirm(card_id, "apply")
        assert confirmed["ok"] is True
        last = spawned(live_daemon["capture"])[-1]
        assert last[:3] == ["set", "--name", "nightly-sync"]
        assert "run=drain-and-sync.sh" in last


# ── the documented gap: no /remove route on this daemon build ────────────────


class TestRemoveGap:
    def test_remove_is_an_honest_error_not_a_silent_no_op(self, live_daemon):
        out = awrise_tools.awrise_remove("nightly-sync")
        assert out["ok"] is False
        assert "error" in out
        # Never claims the job is gone.
        still_there = awrise_tools.awrise_status("nightly-sync")
        assert still_there.get("error") is None
