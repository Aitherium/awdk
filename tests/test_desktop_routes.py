"""The daemon's /desk/tools route: the fleet's reach to the Windows desktop tools.

Every tool here is a FAKE that records its call; nothing moves a window, launches a
workspace or writes a dump. Pins: the allowlist is DESKTOP_TOOLS (unknown -> 404),
arguments are bound and type-checked (400), a write tool acting live needs
confirm=true (403 otherwise) while its dry run needs nothing, the route sits behind
the daemon's bearer, and a /desk-scoped bearer (the gateway's) reaches it while a
phone-link scope does not.
"""
from __future__ import annotations

import json
from typing import List

import pytest
from fastapi.testclient import TestClient

from adk.harnesses import desktop_routes as dr

TOKEN = "test-token-for-testing-only"


def _fake_table(calls):
    def fake_read(query: str = "", limit: int = 0) -> str:
        """Read something.

        query: filter.
        """
        calls.append(("fake_read", query, limit))
        return json.dumps({"ok": True, "rows": [query]})

    def fake_write(items: List[str], dry_run: bool = True) -> str:
        """Change something (write).

        items: things.
        dry_run: plan only.
        """
        calls.append(("fake_write", items, dry_run))
        return json.dumps({"ok": True, "dry_run": dry_run})

    def fake_write_no_dry(pid: int) -> str:
        """Always acts."""
        calls.append(("fake_write_no_dry", pid))
        return json.dumps({"ok": True})

    def fake_crash() -> str:
        """Boom."""
        raise RuntimeError("kaboom")

    return {"fake_read": (fake_read, ""), "fake_write": (fake_write, "write"),
            "fake_write_no_dry": (fake_write_no_dry, "write"),
            "fake_crash": (fake_crash, "")}


@pytest.fixture
def calls():
    return []


@pytest.fixture
def table(calls):
    return _fake_table(calls)


def test_unknown_tool_is_404_and_runs_nothing(table, calls):
    status, out = dr.invoke("desktop_kill_everything", {}, table)
    assert status == 404 and out["reason"] == "unknown_tool"
    assert calls == []


def test_read_tool_runs_with_bound_args(table, calls):
    status, out = dr.invoke("fake_read", {"args": {"query": "py", "limit": 3}}, table)
    assert status == 200 and out["ok"] is True and out["result"]["rows"] == ["py"]
    assert out["action_class"] == "read"
    assert calls == [("fake_read", "py", 3)]


@pytest.mark.parametrize("body", [
    {"args": {"nope": 1}},                 # unknown parameter
    {"args": {"limit": "3"}},              # wrong type
    {"args": {"limit": True}},             # bool is not an integer
    {"args": []},                          # args not an object
    ["not", "an", "object"],               # body not an object
])
def test_bad_args_are_400_and_run_nothing(table, calls, body):
    status, out = dr.invoke("fake_read", body, table)
    assert status == 400 and out["reason"] == "bad_args"
    assert calls == []


def test_array_items_are_type_checked(table, calls):
    status, _ = dr.invoke("fake_write", {"args": {"items": ["a", 2]}}, table)
    assert status == 400 and calls == []


def test_write_dry_run_needs_no_confirm(table, calls):
    status, out = dr.invoke("fake_write", {"args": {"items": ["a"]}}, table)
    assert status == 200 and out["result"]["dry_run"] is True
    assert calls == [("fake_write", ["a"], True)]


@pytest.mark.parametrize("confirm", [None, False, "true", 1])
def test_live_write_without_exact_confirm_is_403(table, calls, confirm):
    body = {"args": {"items": ["a"], "dry_run": False}}
    if confirm is not None:
        body["confirm"] = confirm
    status, out = dr.invoke("fake_write", body, table)
    assert status == 403 and out["reason"] == "confirm_required"
    assert calls == []


def test_live_write_with_confirm_runs(table, calls):
    status, out = dr.invoke("fake_write", {"args": {"items": ["a"], "dry_run": False},
                                           "confirm": True}, table)
    assert status == 200 and out["confirmed"] is True and out["action_class"] == "write"
    assert calls == [("fake_write", ["a"], False)]


def test_write_tool_without_dry_run_always_needs_confirm(table, calls):
    status, _ = dr.invoke("fake_write_no_dry", {"args": {"pid": 9}}, table)
    assert status == 403 and calls == []
    status, _ = dr.invoke("fake_write_no_dry", {"args": {"pid": 9}, "confirm": True}, table)
    assert status == 200 and calls == [("fake_write_no_dry", 9)]


def test_dry_run_must_be_a_real_boolean(table, calls):
    status, _ = dr.invoke("fake_write", {"args": {"items": ["a"], "dry_run": "false"}}, table)
    assert status == 400 and calls == []


def test_crash_is_a_500_payload(table):
    status, out = dr.invoke("fake_crash", {}, table)
    assert status == 500 and "kaboom" in out["error"]


def test_real_table_is_desktop_tools_and_write_classes_are_pinned():
    from adk.desktop.tools import DESKTOP_TOOLS

    listed = dr.list_tools()["tools"]
    assert [t["name"] for t in listed] == sorted(fn.__name__ for fn, _ in DESKTOP_TOOLS)
    writes = {t["name"] for t in listed if t["action_class"] == "write"}
    assert writes == {"desktop_arrange", "desktop_procdump", "desktop_workspace_launch",
                      "desktop_sandbox_install", "desktop_sandbox_detonate",
                      "desktop_sandbox_run", "desktop_sandbox_install_test"}
    for t in listed:
        assert t["description"], t["name"]
        assert t["parameters"]["type"] == "object"
    # A write tool with a dry run must default to it, so an unconfirmed call can
    # only plan. The sandbox execution tools have no dry run and are therefore
    # always gated on confirm=true (pinned in test_desktop_sandbox.py).
    import inspect

    for fn, cls in DESKTOP_TOOLS:
        params = inspect.signature(fn).parameters
        if cls == "write" and "dry_run" in params:
            assert params["dry_run"].default is True, fn.__name__


# -- through the real daemon app ------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch, table):
    monkeypatch.setenv("AITHER_HARNESS_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setenv("AITHER_STEER_DISPATCH_STATUS", str(tmp_path / "dispatch.json"))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", TOKEN)
    monkeypatch.setenv("AITHER_HARNESS_PRINCIPALS", str(tmp_path / "harness_tokens.json"))
    monkeypatch.setenv("AITHER_HOME_OVERRIDE", str(tmp_path / "nohome"))

    import adk.harnesses.daemon as daemon
    from adk.harnesses import rooms as rooms_mod
    from adk.harnesses import session as session_mod
    from adk.harnesses import session_directory as directory_mod
    from adk.harnesses.manager import SessionManager

    monkeypatch.setattr(daemon, "PRINCIPALS_PATH", tmp_path / "harness_tokens.json")
    monkeypatch.setattr(rooms_mod, "_registry", None)
    monkeypatch.setattr(directory_mod, "_directory",
                        directory_mod.SessionDirectory(discover_fn=lambda: []))
    monkeypatch.setattr(session_mod.HarnessSession, "start", lambda self: None)
    monkeypatch.setattr(dr, "_table", lambda: table)
    app = daemon.create_app(manager=SessionManager(root=tmp_path / "sessions"), token=TOKEN)
    return {"client": TestClient(app), "daemon": daemon,
            "registry": tmp_path / "harness_tokens.json"}


def test_route_needs_the_bearer(client, calls):
    c = client["client"]
    assert c.get("/desk/tools").status_code == 401
    assert c.post("/desk/tools/fake_read", json={}).status_code == 401
    assert c.post("/desk/tools/fake_read", json={},
                  headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert calls == []


def test_route_lists_and_invokes_with_owner_bearer(client, calls):
    c = client["client"]
    h = {"Authorization": f"Bearer {TOKEN}"}
    names = [t["name"] for t in c.get("/desk/tools", headers=h).json()["tools"]]
    assert names == sorted(["fake_read", "fake_write", "fake_write_no_dry", "fake_crash"])
    r = c.post("/desk/tools/fake_read", json={"args": {"query": "x"}}, headers=h)
    assert r.status_code == 200 and r.json()["result"]["rows"] == ["x"]
    r = c.post("/desk/tools/fake_write", json={"args": {"items": ["a"], "dry_run": False}},
               headers=h)
    assert r.status_code == 403 and r.json()["reason"] == "confirm_required"
    assert c.post("/desk/tools/nope", json={}, headers=h).status_code == 404
    assert calls == [("fake_read", "x", 0)]


def test_desk_scoped_gateway_token_reaches_it_and_a_link_token_does_not(client, calls):
    daemon, c = client["daemon"], client["client"]
    gw = daemon.mint_scoped_token("gateway", paths=("/desk",), plan="agent",
                                  path=client["registry"])
    link = daemon.mint_scoped_token("phone", path=client["registry"])
    ok = c.post("/desk/tools/fake_read", json={}, headers={"Authorization": f"Bearer {gw}"})
    assert ok.status_code == 200
    refused = c.post("/desk/tools/fake_read", json={},
                     headers={"Authorization": f"Bearer {link}"})
    assert refused.status_code == 403
    assert calls == [("fake_read", "", 0)]
