"""adk.tool_capture: every ToolRegistry.execute call becomes one redacted JSONL row."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from adk import tool_capture
from adk.tools import ToolRegistry

FAKE_OPENAI = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
FAKE_GH = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123"
FAKE_AITHER = "aither_sk_" + "live_0123456789abcdef"


@pytest.fixture
def capture_dir(tmp_path, monkeypatch):
    d = tmp_path / "tool_calls"
    monkeypatch.setenv("AITHER_TOOL_CALLS_DIR", str(d))
    monkeypatch.setenv("AITHER_TOOL_CAPTURE", "1")
    monkeypatch.delenv("AITHER_PRIVATE", raising=False)
    monkeypatch.delenv("AITHER_TELEMETRY_PRIVACY", raising=False)
    return d


def _rows(d: Path) -> list[dict]:
    out: list[dict] = []
    for f in sorted(d.glob("*.jsonl")) if d.exists() else []:
        out.extend(json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line)
    return out


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.agent_name = "tester"

    def echo(text: str) -> str:
        """Echo."""
        return f"echo:{text}"

    async def boom(x: int) -> str:
        """Fail."""
        raise RuntimeError("kaboom")

    def leak(path: str) -> dict:
        """Return a secret-bearing result."""
        return {"path": path, "token": FAKE_GH, "note": f"key {FAKE_OPENAI}"}

    reg.register(echo)
    reg.register(boom)
    reg.register(leak)
    return reg


def test_successful_call_writes_one_row_with_full_shape(capture_dir):
    out = asyncio.run(_registry().execute("echo", {"text": "hi"}))
    assert out == "echo:hi"
    rows = _rows(capture_dir)
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {
        "ts",
        "session_id",
        "agent",
        "tool",
        "args",
        "result",
        "ok",
        "error_kind",
        "latency_ms",
        "surface",
    }
    assert row["tool"] == "echo" and row["agent"] == "tester"
    assert row["args"] == {"text": "hi"} and row["result"] == "echo:hi"
    assert row["ok"] is True and row["error_kind"] == "" and row["surface"] == "awdk"
    assert row["latency_ms"] >= 0
    assert (capture_dir / f"{row['ts'][:10]}.jsonl").exists()


def test_failed_and_unknown_calls_are_labelled(capture_dir):
    reg = _registry()
    asyncio.run(reg.execute("boom", {"x": 1}))
    asyncio.run(reg.execute("nope", {}))
    rows = _rows(capture_dir)
    assert [(r["tool"], r["ok"], r["error_kind"]) for r in rows] == [
        ("boom", False, "RuntimeError"),
        ("nope", False, "unknown_tool"),
    ]


def test_session_id_comes_from_the_turn_context(capture_dir):
    from adk.agent import TOOL_SESSION_CTX

    token = TOOL_SESSION_CTX.set("conv-123")
    try:
        asyncio.run(_registry().execute("echo", {"text": "x"}))
    finally:
        TOOL_SESSION_CTX.reset(token)
    assert _rows(capture_dir)[0]["session_id"] == "conv-123"


def test_args_and_result_are_redacted(capture_dir):
    reg = _registry()
    args = {
        "path": "/tmp/a",
        "password": "hunter2hunter2",
        "api_key": "abc",
        "headers": {"Authorization": "Bearer abcdefghijklmnop"},
        "cmd": f"curl -H 'Authorization: Bearer {FAKE_AITHER}' --token={FAKE_GH}",
        "max_tokens": 512,
    }
    reg.register(lambda **kw: "ok", name="anything")
    asyncio.run(reg.execute("leak", {"path": "/x"}))
    asyncio.run(reg.execute("anything", args))
    blob = "".join(f.read_text(encoding="utf-8") for f in capture_dir.glob("*.jsonl"))
    for secret in (FAKE_OPENAI, FAKE_GH, FAKE_AITHER, "hunter2hunter2", "abcdefghijklmnop"):
        assert secret not in blob
    rows = _rows(capture_dir)
    a = rows[1]["args"]
    assert a["password"] == "[REDACTED]" and a["api_key"] == "[REDACTED]"
    assert a["headers"]["Authorization"] == "[REDACTED]"
    assert a["max_tokens"] == 512 and a["path"] == "/tmp/a"


def test_result_is_truncated(capture_dir):
    reg = ToolRegistry()
    reg.register(lambda: "x" * 50_000, name="big")
    asyncio.run(reg.execute("big", {}))
    assert len(_rows(capture_dir)[0]["result"]) == tool_capture.RESULT_MAX_CHARS


@pytest.mark.parametrize(
    "env",
    [
        ("AITHER_PRIVATE", "1"),
        ("AITHER_TELEMETRY_PRIVACY", "private"),
        ("AITHER_TOOL_CAPTURE", "0"),
    ],
)
def test_private_or_off_writes_nothing(capture_dir, monkeypatch, env):
    monkeypatch.setenv(*env)
    assert asyncio.run(_registry().execute("echo", {"text": "secret plan"})) == "echo:secret plan"
    assert not capture_dir.exists() or _rows(capture_dir) == []


def test_private_mode_context_writes_nothing(capture_dir):
    with tool_capture.private_mode():
        asyncio.run(_registry().execute("echo", {"text": "a"}))
    assert not capture_dir.exists()
    asyncio.run(_registry().execute("echo", {"text": "b"}))
    assert len(_rows(capture_dir)) == 1


def test_writer_failure_does_not_break_the_tool_call(capture_dir, monkeypatch):
    def explode(row):
        raise OSError("disk full")

    monkeypatch.setattr(tool_capture, "_append", explode)
    assert asyncio.run(_registry().execute("echo", {"text": "still works"})) == "echo:still works"
    assert asyncio.run(_registry().execute("boom", {"x": 1})).startswith('{"error"')


def test_unwritable_dir_does_not_break_the_tool_call(tmp_path, monkeypatch):
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    monkeypatch.setenv("AITHER_TOOL_CAPTURE", "1")
    monkeypatch.delenv("AITHER_PRIVATE", raising=False)
    monkeypatch.setenv("AITHER_TOOL_CALLS_DIR", str(blocker / "sub"))
    assert asyncio.run(_registry().execute("echo", {"text": "y"})) == "echo:y"


def test_outcome_row(capture_dir):
    assert tool_capture.record_outcome("conv-9", ok=True, reward=1.0, note=f"done {FAKE_GH}")
    row = _rows(capture_dir)[0]
    assert row["kind"] == "outcome" and row["session_id"] == "conv-9" and row["reward"] == 1.0
    assert FAKE_GH not in row["note"]


def test_cli_private_flag_sets_the_capture_switch():
    src = (Path(__file__).resolve().parents[1] / "adk" / "shell" / "cli.py").read_text(
        encoding="utf-8"
    )
    i = src.index('aither_config.privacy_level = "private"')
    assert 'os.environ["AITHER_PRIVATE"] = "1"' in src[i : i + 400]
