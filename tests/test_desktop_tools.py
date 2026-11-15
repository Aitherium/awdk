"""Tool-layer contracts for adk.desktop.tools -- a fake backend, no real windows."""

import inspect
import json

import pytest

from adk.desktop import tools as dt
from adk.desktop import sandbox as sb
from adk.desktop import windows as dw
from adk.tools import ToolRegistry
from tests.test_desktop_windows import FakeBackend, win


class _Agent:
    def __init__(self):
        self._tools = ToolRegistry()


@pytest.fixture
def agent():
    a = _Agent()
    assert dt.register_desktop_tools(a) == len(dt.DESKTOP_TOOLS) == 23
    return a


def _tool(agent, name):
    return agent._tools._tools[name]


def test_registers_two_with_write_class(agent):
    assert set(agent._tools._tools) >= {"desktop_arrange", "desktop_list_windows"}
    assert _tool(agent, "desktop_arrange").action_class == "write"
    assert _tool(agent, "desktop_list_windows").action_class == ""


def test_schema_uses_type_hints(agent):
    # tools.py uses postponed annotations, so these are strings until resolved.
    assert dt.desktop_arrange.__annotations__["dry_run"] == "bool"
    props = _tool(agent, "desktop_arrange").parameters["properties"]
    assert props["windows"] == {"type": "array", "items": {"type": "string"},
                                "description": props["windows"]["description"]}
    assert props["dry_run"]["type"] == "boolean"
    assert props["monitor"]["type"] == "integer"
    assert props["windows"]["description"]
    assert _tool(agent, "desktop_arrange").parameters["required"] == ["windows"]


def test_default_dry_run_is_true():
    assert inspect.signature(dt.desktop_arrange).parameters["dry_run"].default is True


def test_dry_run_receipt_is_json_and_moves_nothing(monkeypatch):
    be = FakeBackend([win(1, "A"), win(2, "B")])
    monkeypatch.setattr(dt, "_backend", lambda: be)
    out = json.loads(dt.desktop_arrange(["A", "B"]))
    assert out["ok"] is True and out["dry_run"] is True
    assert be.calls == []
    listed = json.loads(dt.desktop_list_windows())
    assert listed["ok"] is True
    assert [r["title"] for r in listed["windows"]] == ["A", "B"]


def test_elevated_refusal_receipt(monkeypatch):
    be = FakeBackend([win(1, "A"), win(2, "B")], elevated={20: True})
    monkeypatch.setattr(dt, "_backend", lambda: be)
    out = json.loads(dt.desktop_arrange(["A", "B"], dry_run=False))
    assert out["ok"] is False and out["needs_admin_lane"] is True
    assert be.calls == []


def test_errors_are_json(monkeypatch):
    be = FakeBackend([win(1, "A")])
    monkeypatch.setattr(dt, "_backend", lambda: be)
    out = json.loads(dt.desktop_arrange(["nope"]))
    assert out == {"ok": False, "reason": "not_found", "error": out["error"],
                   "needs_admin_lane": False}
    bad = json.loads(dt.desktop_arrange(["A"], layout="weird"))
    assert bad["ok"] is False and bad["reason"] == "bad_layout"


def test_not_windows_is_json(monkeypatch):
    monkeypatch.setattr(dw, "IS_WINDOWS", False)
    out = json.loads(dt.desktop_list_windows())
    assert out["reason"] == "not_windows" and out["windows"] == []


def test_fancyzones_refuses_named_windows(monkeypatch):
    from adk.desktop import fancyzones

    monkeypatch.setattr(fancyzones, "apply_layout",
                        lambda *a, **k: pytest.fail("must not reach the CLI"))
    out = json.loads(dt.desktop_arrange(
        ["A", "B"], layout="fancyzones:12345678-1234-1234-1234-123456789abc"))
    assert out["ok"] is False and out["reason"] == "bad_layout"


def test_fancyzones_monitor_is_one_based(monkeypatch):
    from adk.desktop import fancyzones

    seen = {}
    monkeypatch.setattr(fancyzones, "apply_layout",
                        lambda uuid, monitor, dry_run: seen.update(m=monitor) or {"ok": True})
    uuid = "12345678-1234-1234-1234-123456789abc"
    dt.desktop_arrange([], monitor=0, layout="fancyzones:" + uuid)
    assert seen["m"] == 1
    dt.desktop_arrange([], monitor=-1, layout="fancyzones:" + uuid)
    assert seen["m"] == -1


def test_fancyzones_cli_ignores_empty_localappdata(monkeypatch, tmp_path):
    from adk.desktop import fancyzones

    monkeypatch.setenv("LOCALAPPDATA", "")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "PowerToys").mkdir()
    (tmp_path / "PowerToys" / "FancyZonesCLI.exe").write_bytes(b"")
    found = fancyzones.cli_path()
    assert found is not None and found.is_absolute()


def test_sysinternals_tool_wraps_rows(monkeypatch):
    from adk.desktop import sysinternals as si

    class Fake:
        def ports(self):
            return si.RunResult("tcpvcon", [], 0, rows=[{"process": "x.exe"}])

    monkeypatch.setattr(si, "Sysinternals", Fake)
    out = json.loads(dt.desktop_ports())
    assert out["ok"] is True and out["rows"] == [{"process": "x.exe"}]


def test_sysinternals_timeout_is_not_ok(monkeypatch):
    from adk.desktop import sysinternals as si

    class Fake:
        def who_holds(self, name):
            return si.RunResult("handle", [], None, timed_out=True, stderr="timed out")

    monkeypatch.setattr(si, "Sysinternals", Fake)
    out = json.loads(dt.desktop_who_holds("x"))
    assert out["ok"] is False and out["timed_out"] is True and "timed out" in out["error"]


def test_sysinternals_missing_is_json(monkeypatch):
    from adk.desktop import sysinternals as si

    def boom():
        raise si.SysinternalsError("not found")

    monkeypatch.setattr(si, "Sysinternals", boom)
    out = json.loads(dt.desktop_ports())
    assert out == {"ok": False, "reason": "not_installed", "error": "not found", "rows": []}


def test_detonate_tool_keeps_run_folder_exit_code_digest_and_counts(tmp_path, monkeypatch):
    """A capped detonate receipt still points at the run folder and the executed name."""
    target = tmp_path / "sample.exe"
    target.write_bytes(b"MZ")
    tasks = "C:\\Windows\\System32\\Tasks\\"
    data = {
        "file": "sample.exe", "sha256": "c" * 64, "exitCode": 0,
        "tree": [{"pid": i, "commandLine": "q" * 5000} for i in range(2)],
        "lookups": [{"name": "example.com"}],
        "connections": [],
        "diff": {"files": {"added": [{"path": tasks + "One"}]},
                 "values": [{"name": "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\X"}],
                 "services": [{"change": "added", "name": "svc"}]},
        "findings": [{"level": "high", "text": "w" * 5000}],
    }

    class Fake:
        def detonate(self, *args, **kwargs):
            return sb.SbXResult(command="sbx detonate --json sample.exe", argv=[],
                                exit_code=0, data=data, run_dir=str(tmp_path / "run"),
                                report={"found": True, "json": data})

    monkeypatch.setattr(sb, "Sandbox", lambda *a, **k: Fake())
    out = json.loads(dt.desktop_sandbox_detonate(str(target)))
    assert out["truncated"] is True, "the fixture must exceed the cap to test it"
    assert out["run_dir"] == str(tmp_path / "run")
    assert out["exit_code"] == 0 and out["sha256"] == "c" * 64
    assert out["counts"] == {"processes": 2, "lookups": 1, "connections": 0,
                             "services_added": 1, "persistence_values": 1,
                             "scheduled_tasks": 1, "findings": 1}
