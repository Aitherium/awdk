"""``adk home teach start``: a teacher's agent from one click, and the page that sees it.

Two halves, both pinned here:

* ``GET /browser/status`` on the local channel -- what a setup page on an allowlisted
  origin may learn BEFORE it pairs: booleans and the awdk version, never a name, a
  token or a path; refused for any other origin; answered for the Classroom page.
* :func:`adk.home.teach_setup.start` -- setup, start at logon, start now, wait, and
  open the page with a one-time code in the URL fragment. It reports ``ready`` only
  when a serve of this home answered WITH the classroom tools, restarts a serve that
  was running before the setup, and never claims a step it did not do.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

pytest.importorskip("starlette")
httpx = pytest.importorskip("httpx")

import adk  # noqa: E402
from adk.home import config as hc  # noqa: E402
from adk.home import models, serve, teach_setup  # noqa: E402
from adk.home.transports import browser as br  # noqa: E402
from adk.home.transports import local as lt  # noqa: E402
from adk.tools import ToolRegistry  # noqa: E402

BASE = "https://api.example.test"
ACADEMY = "https://academy.aitherium.com"
EVIL = "https://evil.example"


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "agent-home"
    monkeypatch.setenv(hc.HOME_ENV, str(root))
    monkeypatch.delenv(br.ORIGINS_ENV, raising=False)
    monkeypatch.delenv(lt.PORT_ENV, raising=False)
    monkeypatch.delenv(serve.TEACHER_FLAG_ENV, raising=False)
    hc.init_home(name="Ms Rivera private agent", root=root)
    return root


def _transport(home, tools=(), **kw):
    reg = ToolRegistry()
    for name in tools:
        def fn() -> str:
            """A tool."""
            return "{}"

        fn.__name__ = name
        reg.register(fn)
    agent = SimpleNamespace(name="Ms Rivera private agent", _tools=reg,
                            llm=SimpleNamespace(model="bonsai-selfhost"))
    t = lt.LocalTransport(root=home, port=0, user_id="localowner", **kw)
    t.core = SimpleNamespace(agent=agent)
    return t


def _get(t, path, headers=None, host="127.0.0.1"):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=t.app),
                                     base_url=f"http://{host}") as c:
            return await c.get(path, headers=headers or {})

    return asyncio.run(go())


# ── /browser/status ─────────────────────────────────────────────────────────────

def test_the_classroom_page_origin_is_allowed_by_default():
    assert ACADEMY in br.DEFAULT_ORIGINS
    assert br.BrowserPairing(br.DEFAULT_ORIGINS).origin_allowed(ACADEMY)


def test_status_answers_the_classroom_page_without_a_bearer(home, monkeypatch):
    monkeypatch.setattr(lt, "STATUS_PROBE_S", 0.2)
    from adk.home import connector_tools

    monkeypatch.setattr(connector_tools, "home_signed_in", lambda: True)
    monkeypatch.setattr(models, "probe", lambda cfg, timeout=3.0: {"ok": True})
    t = _transport(home, tools=("class_brief", "struggle_report"))
    r = _get(t, br.STATUS_PATH, {"Origin": ACADEMY})
    assert r.status_code == 200, r.text
    assert r.headers["access-control-allow-origin"] == ACADEMY
    assert r.json() == {"running": True, "version": r.json()["version"], "teacher": True,
                        "local_model": True, "model_up": True, "signed_in": True}


def test_status_carries_nothing_personal(home, monkeypatch):
    monkeypatch.setattr(models, "probe", lambda cfg, timeout=3.0: {"ok": False})
    t = _transport(home, tools=("class_brief",))
    body = _get(t, br.STATUS_PATH, {"Origin": ACADEMY}).text
    assert set(json.loads(body)) == {"running", "version", "teacher", "local_model",
                                     "model_up", "signed_in"}
    for secret in ("Rivera", t._token, str(home), "bonsai-selfhost", "localowner"):
        assert secret not in body


def test_status_says_tools_off_when_the_running_agent_lacks_them(home, monkeypatch):
    monkeypatch.setattr(models, "probe", lambda cfg, timeout=3.0: {"ok": True})
    # teach setup ran (flag on disk) but THIS process started before it.
    teach_setup.write_teacher_state(BASE, home)
    assert serve.teacher_enabled(home)
    t = _transport(home, tools=("remind_me",))
    assert _get(t, br.STATUS_PATH, {"Origin": ACADEMY}).json()["teacher"] is False


def test_status_reports_a_remote_model_as_not_local_and_never_probes_it(home, monkeypatch):
    cfg = hc.load_config(home)
    cfg.model = models.choose_model("openai")
    hc.save_config(cfg, home)
    probed: List[Any] = []
    monkeypatch.setattr(models, "probe",
                        lambda cfg, timeout=3.0: probed.append(cfg) or {"ok": True})
    data = _get(_transport(home), br.STATUS_PATH, {"Origin": ACADEMY}).json()
    assert data["local_model"] is False and data["model_up"] is False and probed == []


@pytest.mark.parametrize("origin", [EVIL, "null", "http://academy.aitherium.com",
                                    "https://academy.aitherium.com.evil.example"])
def test_status_is_refused_for_any_other_origin(home, origin):
    r = _get(_transport(home), br.STATUS_PATH, {"Origin": origin})
    assert r.status_code == 403 and "access-control-allow-origin" not in r.headers


def test_status_is_refused_for_a_rebound_host(home):
    r = _get(_transport(home), br.STATUS_PATH, {"Origin": ACADEMY}, host="academy.aitherium.com")
    assert r.status_code == 403


def test_status_is_the_only_bearerless_browser_path(home):
    t = _transport(home)
    for path in ("/browser/state",):
        assert _get(t, path, {"Origin": ACADEMY}).status_code == 401
    # an allowlisted page still cannot reach the token-guarded local API
    assert _get(t, "/receipts", {"Origin": ACADEMY}).status_code == 403


def test_status_is_cached_so_a_loop_costs_one_model_probe(home, monkeypatch):
    calls: List[int] = []
    monkeypatch.setattr(models, "probe", lambda cfg, timeout=3.0: calls.append(1) or {"ok": True})
    t = _transport(home)
    for _ in range(5):
        assert _get(t, br.STATUS_PATH, {"Origin": ACADEMY}).status_code == 200
    assert len(calls) == 1


# ── teach start ─────────────────────────────────────────────────────────────────

class FakeOps:
    """The machine, scripted: ``statuses`` is what each status() call answers."""

    def __init__(self, statuses, *, autostart=False, install="windows-task:aither-hearth",
                 spawn=True, code="ABCD-EFGH", opens=True, model_ok=True, current=True):
        self.statuses = list(statuses)
        self.present = autostart
        self.current = current
        self.install_result = install
        self.spawn_result = spawn
        self.code = code
        self.opens = opens
        self.model_ok = model_ok
        self.calls: List[str] = []
        self.opened: List[str] = []
        self.slept = 0.0

    def status(self) -> Optional[Dict[str, Any]]:
        self.calls.append("status")
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def stop(self) -> bool:
        self.calls.append("stop")
        return True

    def autostart_present(self) -> bool:
        return self.present

    def autostart_current(self) -> bool:
        return self.present and self.current

    def install_autostart(self) -> Optional[str]:
        self.calls.append("install")
        if self.install_result:
            self.present = self.current = True
        return self.install_result

    def spawn(self) -> bool:
        self.calls.append("spawn")
        return self.spawn_result

    def browser_code(self) -> Dict[str, Any]:
        self.calls.append("code")
        if not self.code:
            raise hc.HomeError("nothing is serving; start `adk home serve`")
        return {"code": self.code}

    def open_page(self, url: str) -> bool:
        self.opened.append(url)
        return self.opens

    def model_probe(self) -> Dict[str, Any]:
        return {"ok": self.model_ok, "detail": "http://127.0.0.1:8080/v1/models unreachable"}

    def sleep(self, seconds: float) -> None:
        self.slept += seconds


UP = {"running": True, "teacher": True, "signed_in": True, "version": adk.__version__}
UP_NO_TOOLS = {"running": True, "teacher": False, "signed_in": False,
               "version": adk.__version__}
UP_OLDER = dict(UP, version="0.0.1")


def _start(home, ops, lines=None, **kw):
    kw.setdefault("probe", lambda u: {"online": True, "url": u})
    kw.setdefault("signed_in", lambda: True)
    return teach_setup.start(BASE, root=home, ops=ops,
                             out=(lines if lines is not None else []).append, **kw)


def test_start_from_nothing_installs_starts_and_opens_the_page_paired(home):
    ops = FakeOps([None, None, UP])
    lines: List[str] = []
    out = _start(home, ops, lines)
    assert out["ready"] is True and out["serve"] == "started"
    assert out["autostart"] == "windows-task:aither-hearth" and out["page"] == "opened"
    assert ops.calls.index("install") < ops.calls.index("spawn") < ops.calls.index("code")
    assert "stop" not in ops.calls
    assert ops.opened == [teach_setup.DEFAULT_PAGE + "#pair=ABCD-EFGH"]
    assert serve.teacher_enabled(home)                       # setup ran first
    assert any(line == "agent: running, signed in, classroom tools on." for line in lines)


def test_the_code_rides_in_the_fragment_never_the_query():
    url = teach_setup.pair_url(teach_setup.DEFAULT_PAGE + "#old", "WXYZ-2345")
    assert url == "https://academy.aitherium.com/classroom/agent#pair=WXYZ-2345"
    assert "?" not in url


def test_start_leaves_a_ready_agent_alone(home):
    ops = FakeOps([UP], autostart=True)
    out = _start(home, ops)
    assert out["serve"] == "already running" and out["autostart"] == "existing"
    assert out["ready"] is True
    assert not {"stop", "spawn", "install"} & set(ops.calls)


def test_start_restarts_a_serve_that_ran_before_the_setup(home):
    # status: before (no tools) -> old process still answering -> the new one with tools
    ops = FakeOps([UP_NO_TOOLS, UP_NO_TOOLS, UP], autostart=True)
    out = _start(home, ops)
    assert ops.calls.index("stop") < ops.calls.index("spawn")
    assert out["serve"] == "started" and out["ready"] is True


def test_start_restarts_after_a_fresh_sign_in(home):
    ops = FakeOps([UP, UP], autostart=True)
    signins: List[int] = []
    out = _start(home, ops, signed_in=lambda: False,
                 do_signin=lambda: signins.append(1) or 0)
    assert signins == [1] and out["signin"] == "done"
    assert "stop" in ops.calls and "spawn" in ops.calls       # the bearer is read at start


# -- an existing home, an existing logon entry, an upgraded awdk ------------------

def test_start_on_a_home_with_another_model_says_what_it_replaced(home):
    """One double-click must not silently change the model of an agent that already
    answers its owner on other channels."""
    cfg = hc.load_config(home)
    cfg.model = models.choose_model("openai")
    hc.save_config(cfg, home)
    lines: List[str] = []
    out = _start(home, FakeOps([None, UP]), lines)
    assert out["init"] == "existing" and out["model"] == "bonsai"
    assert out["model_previous"] == "openai"
    said = [line for line in lines if line.startswith("model: this agent used openai before")]
    assert len(said) == 1 and "on this computer" in said[0]
    assert hc.load_config(home).model.provider == "bonsai"


def test_start_on_a_home_already_on_local_bonsai_says_nothing_about_the_model(home):
    lines: List[str] = []
    out = _start(home, FakeOps([UP], autostart=True), lines)
    assert "model_previous" not in out
    assert not any("before" in line for line in lines)


def test_setup_on_a_brand_new_home_reports_no_previous_model(tmp_path):
    root = tmp_path / "fresh-home"
    out = teach_setup.setup(BASE, root=root, signin=False, signed_in=lambda: True,
                            probe=lambda u: {"online": True, "url": u}, out=lambda s: None)
    assert out["init"] == "created" and "model_previous" not in out


def test_start_replaces_a_logon_entry_left_by_another_python(home):
    """`aither-hearth` is shared with any earlier `serve --install`: one that names
    another interpreter is rewritten, and what it started is restarted from ours."""
    ops = FakeOps([UP, UP], autostart=True, current=False)
    lines: List[str] = []
    out = _start(home, ops, lines)
    assert out["autostart"] == "windows-task:aither-hearth" and out["autostart_replaced"]
    assert ops.calls.index("install") < ops.calls.index("stop") < ops.calls.index("spawn")
    assert out["serve"] == "started" and out["ready"] is True
    assert any(line.startswith("start at logon: an older entry") for line in lines)


def test_real_ops_judge_the_logon_entry_by_the_interpreter_it_names(home, tmp_path,
                                                                    monkeypatch):
    import sys

    from adk import agent_daemon
    from adk.home.cli import SERVE_AUTOSTART

    monkeypatch.setattr(agent_daemon, "AITHER_HOME", tmp_path / "aither")
    monkeypatch.setattr(agent_daemon, "systemd_user_dir", lambda: tmp_path / "systemd")
    monkeypatch.setattr(agent_daemon, "launchd_agents_dir", lambda: tmp_path / "launchd")
    ops = teach_setup.StartOps(home)
    entry = ops.autostart_file()
    assert tmp_path in entry.parents and SERVE_AUTOSTART in entry.name
    assert ops.autostart_present() is False and ops.autostart_current() is False
    entry.parent.mkdir(parents=True, exist_ok=True)
    other = ["/opt/pipx/venvs/awdk/bin/python", "-m", "adk.home", "serve"]
    entry.write_text(agent_daemon._LAUNCHER_TEMPLATE.format(argv=other, log="x", env={}),
                     encoding="utf-8")
    assert ops.autostart_present() is True and ops.autostart_current() is False
    # the Windows launcher stores the argv as a repr (doubled backslashes)
    entry.write_text(agent_daemon._LAUNCHER_TEMPLATE.format(
        argv=ops.serve_argv(), log="x", env={}), encoding="utf-8")
    assert ops.autostart_current() is True
    entry.write_text("ExecStart=" + " ".join(ops.serve_argv()) + "\n", encoding="utf-8")
    assert ops.autostart_current() is True
    assert sys.executable == ops.serve_argv()[0]


def test_real_spawn_never_starts_through_an_entry_that_names_another_python(
        home, tmp_path, monkeypatch):
    import subprocess

    from adk import agent_daemon

    monkeypatch.setattr(agent_daemon, "AITHER_HOME", tmp_path / "aither")
    monkeypatch.setattr(agent_daemon, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(agent_daemon, "systemd_user_dir", lambda: tmp_path / "systemd")
    monkeypatch.setattr(agent_daemon, "launchd_agents_dir", lambda: tmp_path / "launchd")
    ran: List[Any] = []
    started: List[Any] = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: ran.append(argv))
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: started.append(argv))
    ops = teach_setup.StartOps(home)
    entry = ops.autostart_file()
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("ARGV = ['/opt/pipx/venvs/awdk/bin/python', '-m', 'adk.home', 'serve']",
                     encoding="utf-8")
    assert ops.spawn() is True
    # no launchctl / systemctl on the foreign entry, and the process is THIS python's
    assert ran == [] and started == [ops.serve_argv()]


def test_start_restarts_a_serve_from_an_older_awdk(home):
    """The launcher upgrades awdk on every open; the process started from the old
    files must not go on running while it lazily imports the new ones."""
    # before (old) -> the old one still answering on its way out -> the new one
    ops = FakeOps([UP_OLDER, UP_OLDER, UP], autostart=True)
    out = _start(home, ops)
    assert ops.calls.index("stop") < ops.calls.index("spawn")
    assert out["serve"] == "started" and out["ready"] is True
    assert out["status"]["version"] == adk.__version__      # never the one on its way out
    assert ops.slept >= 3.0                                  # 2 s after stop + one wait


def test_start_is_honest_when_the_older_serve_never_goes_away(home):
    ops = FakeOps([UP_OLDER], autostart=True)
    out = _start(home, ops, wait_s=2.0)
    assert "stop" in ops.calls and out["status"]["version"] == "0.0.1"
    assert ops.slept == 4.0                                  # bounded


def test_stop_serve_signals_only_a_serve_that_answered(home):
    ops = FakeOps([None])
    assert teach_setup.stop_serve(home, ops) is False and "stop" not in ops.calls
    ops = FakeOps([UP])
    assert teach_setup.stop_serve(home, ops) is True and ops.calls == ["status", "stop"]


def test_stop_serve_ignores_a_stale_token_file(home, monkeypatch):
    """A local.token left by a dead serve names a pid that may be another program."""
    import os

    lt.token_path(home).write_text(json.dumps({"token": "x" * 43, "port": 1, "pid": 4242}),
                                   encoding="utf-8")
    killed: List[int] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
    assert teach_setup.stop_serve(home) is False and killed == []


def test_cli_teach_stop_exits_by_whether_it_stopped_one(home, monkeypatch, capsys):
    from adk.home import cli

    p = argparse.ArgumentParser()
    cli._build(p)
    args = p.parse_args(["teach", "stop"])
    monkeypatch.setattr(teach_setup, "stop_serve", lambda: True)
    assert cli.cmd_teach(args) == cli.EXIT_OK and "stopped" in capsys.readouterr().out
    monkeypatch.setattr(teach_setup, "stop_serve", lambda: False)
    assert cli.cmd_teach(args) == cli.EXIT_FAIL
    assert "none was running" in capsys.readouterr().out


def test_start_says_not_running_when_nothing_answers(home):
    ops = FakeOps([None])
    lines: List[str] = []
    out = _start(home, ops, lines, wait_s=3.0)
    assert out["ready"] is False and out["status"] == {"running": False}
    assert "page" not in out and ops.opened == [] and "code" not in ops.calls
    assert any(line.startswith("agent: NOT running") for line in lines)
    assert ops.slept == 3.0                                   # bounded, not forever


def test_start_does_not_wait_when_the_start_itself_failed(home):
    ops = FakeOps([None], spawn=False)
    out = _start(home, ops)
    assert out["serve"] == "could not start" and out["ready"] is False and ops.slept == 0.0


def test_start_is_not_ready_when_the_tools_never_come_on(home):
    ops = FakeOps([UP_NO_TOOLS], autostart=True)
    lines: List[str] = []
    out = _start(home, ops, lines, wait_s=2.0)
    assert out["ready"] is False
    assert any("classroom tools are OFF" in line for line in lines)


def test_start_is_not_ready_without_a_sign_in_and_says_how_to_fix_it(home):
    """Tools on but no sign-in cannot read one class: that is not "Done"."""
    ops = FakeOps([dict(UP, signed_in=False)], autostart=True)
    lines: List[str] = []
    out = _start(home, ops, lines, signin=False, signed_in=lambda: False)
    assert out["signin"] == "skipped" and out["ready"] is False
    assert any("NOT signed in" in line and "launcher" in line for line in lines)
    assert out["page"] == "opened"                            # the page shows the same state


def test_start_never_tells_the_teacher_to_type_a_command(home):
    """Whatever goes wrong, the fix named is the launcher, never a command line."""
    scenarios = [
        FakeOps([None]), FakeOps([None], spawn=False), FakeOps([UP_NO_TOOLS], autostart=True),
        FakeOps([None, UP], install=None, opens=False), FakeOps([UP], model_ok=False),
        FakeOps([dict(UP, signed_in=False)]),
        FakeOps([None, UP], code=""),
        FakeOps([UP_OLDER, UP]), FakeOps([UP, UP], autostart=True, current=False),
    ]
    for ops in scenarios:
        lines: List[str] = []
        _start(home, ops, lines, wait_s=1.0)
        text = "\n".join(lines)
        for typed in ("adk ", "pip ", "iwr ", "curl ", "powershell", "--", "`"):
            assert typed not in text, (typed, text)
    assert "adk home serve" in _start(home, FakeOps([None, UP], code=""))["page_error"]


def test_probe_asks_the_preset_endpoint_of_a_fresh_home(home, monkeypatch):
    """`adk home init` stores no base_url; the probe must dial what build_llm dials."""
    cfg = hc.load_config(home)
    assert cfg.model.provider == "bonsai" and not cfg.model.base_url
    asked: List[str] = []

    def fake_get(url, timeout=None):
        asked.append(url)
        return SimpleNamespace(status_code=200, json=lambda: {"data": []})

    monkeypatch.setattr(httpx, "get", fake_get)
    assert models.probe(cfg.model)["ok"] is True
    assert asked == ["http://127.0.0.1:8080/v1/models"]


def test_start_reports_a_failed_autostart_and_a_browser_that_did_not_open(home):
    ops = FakeOps([None, UP], install=None, opens=False)
    lines: List[str] = []
    out = _start(home, ops, lines)
    assert out["autostart"] == "not installed" and out["page"] == "not opened"
    assert out["ready"] is True
    assert any("could NOT be installed" in line for line in lines)
    assert any("open " + teach_setup.DEFAULT_PAGE + " yourself" in line for line in lines)


def test_start_says_when_the_model_is_not_answering(home):
    lines: List[str] = []
    out = _start(home, FakeOps([UP], autostart=True, model_ok=False), lines)
    assert out["model_up"] is False
    assert any(line.startswith("model: not answering") for line in lines)


def test_start_refuses_a_page_the_agent_would_not_pair_with(home):
    ops = FakeOps([UP])
    with pytest.raises(hc.HomeError, match="not a page this agent pairs with"):
        _start(home, ops, page="https://evil.example/classroom/agent")
    assert ops.calls == [] and not (home / serve.TEACHER_STATE).exists()


def test_start_without_a_browser_mints_no_code(home):
    ops = FakeOps([UP], autostart=True)
    out = _start(home, ops, open_page=False)
    assert "page" not in out and "code" not in ops.calls and ops.opened == []


def test_start_never_prints_the_pairing_code(home):
    lines: List[str] = []
    _start(home, FakeOps([None, UP]), lines)
    assert not any("ABCD" in line for line in lines)


@pytest.mark.parametrize("page,origin", [
    ("https://academy.aitherium.com/classroom/agent", "https://academy.aitherium.com"),
    ("HTTPS://Academy.Aitherium.com/x#y", "https://academy.aitherium.com"),
    ("http://localhost:3000/classroom/agent", "http://localhost:3000"),
    ("javascript:alert(1)", ""), ("", ""), ("academy.aitherium.com", ""),
])
def test_page_origin(page, origin):
    assert teach_setup.page_origin(page) == origin


def test_cli_parses_teach_start_and_exits_by_readiness(home, monkeypatch, capsys):
    from adk.home import cli

    p = argparse.ArgumentParser()
    cli._build(p)
    args = p.parse_args(["teach", "start", "--no-open", "--no-autostart", "--json",
                         "--url", BASE])
    assert args.teach_command == "start" and args.no_open and args.no_autostart
    seen: Dict[str, Any] = {}

    def fake_start(url, **kw):
        seen.update(kw, url=url)
        return {"ready": seen.get("_ready", False), "url": url}

    monkeypatch.setattr(teach_setup, "start", fake_start)
    assert cli.cmd_teach(args) == cli.EXIT_FAIL               # not ready is not success
    assert seen["open_page"] is False and seen["autostart"] is False
    assert seen["page"] == teach_setup.DEFAULT_PAGE and seen["url"] == BASE
    assert json.loads(capsys.readouterr().out)["ready"] is False
    seen["_ready"] = True
    assert cli.cmd_teach(args) == cli.EXIT_OK


def test_real_ops_see_no_serve_in_an_empty_home(home):
    ops = teach_setup.StartOps(home)
    assert ops.status() is None and ops.stop() is False
    assert ops.serve_argv()[1:] == ["-m", "adk.home", "serve"]


def test_real_ops_stop_never_signals_this_process(home, monkeypatch):
    import os

    lt.token_path(home).write_text(json.dumps({"token": "x" * 43, "port": 1,
                                               "pid": os.getpid()}), encoding="utf-8")
    killed: List[int] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
    assert teach_setup.StartOps(home).stop() is False and killed == []


# ── the real ops against a real listening serve ─────────────────────────────────

@pytest.mark.asyncio
async def test_start_against_a_real_serve_pairs_the_page(home, monkeypatch):
    """Real socket, real token proof, real one-time code: only the browser is stubbed.
    The code that reaches the page URL must be the one the serve will accept from the
    Classroom origin, exactly once."""
    pytest.importorskip("uvicorn")
    from adk.home import connector_tools

    monkeypatch.setattr(models, "probe", lambda cfg, timeout=3.0: {"ok": True})
    monkeypatch.setattr(connector_tools, "home_signed_in", lambda: True)
    t = _transport(home, tools=("class_brief",))
    await t._runner.start(t.app, lt.CHANNEL, public=False)
    lt.write_endpoint(t._token, t.bound_port, home)
    opened: List[str] = []

    class Ops(teach_setup.StartOps):
        def open_page(self, url):
            opened.append(url)
            return True

        def autostart_present(self):
            return True

        def autostart_current(self):
            return True

        def install_autostart(self):
            raise AssertionError("a test never writes a logon entry on this machine")

        def spawn(self):
            raise AssertionError("a ready serve must not be restarted")

    try:
        ops = Ops(home)
        status = await asyncio.to_thread(ops.status)
        assert status["running"] is True and status["teacher"] is True
        out = await asyncio.to_thread(
            lambda: teach_setup.start(BASE, root=home, ops=ops, out=lambda s: None,
                                      probe=lambda u: {"online": True, "url": u},
                                      signed_in=lambda: True))
        assert out["ready"] is True and out["serve"] == "already running"
        assert len(opened) == 1 and opened[0].startswith(teach_setup.DEFAULT_PAGE + "#pair=")
        code = opened[0].split("#pair=", 1)[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{t.bound_port}",
                                     trust_env=False) as c:
            r = await c.post("/browser/pair", json={"code": code}, headers={"Origin": EVIL})
            assert r.status_code == 403
            r = await c.post("/browser/pair", json={"code": code}, headers={"Origin": ACADEMY})
            assert r.status_code == 200 and r.json()["token"]
            r = await c.post("/browser/pair", json={"code": code}, headers={"Origin": ACADEMY})
            assert r.status_code == 401                       # single use
    finally:
        await t._runner.stop()
        lt.remove_endpoint(t._token, home)


@pytest.mark.asyncio
async def test_real_ops_do_not_trust_a_stranger_on_the_port(home):
    """A listener that cannot prove it holds this home's token is 'not running'."""
    pytest.importorskip("uvicorn")
    t = _transport(home, tools=("class_brief",))
    await t._runner.start(t.app, lt.CHANNEL, public=False)
    lt.write_endpoint(lt.new_token(), t.bound_port, home)     # another token on disk
    try:
        assert await asyncio.to_thread(teach_setup.StartOps(home).status) is None
    finally:
        await t._runner.stop()


def test_spawn_starts_the_serve_directly_when_the_service_manager_is_missing(tmp_path, monkeypatch):
    """No `systemctl` (a container, WSL without systemd): spawn must not raise. It falls
    through and starts the serve itself. Found 2026-10-02 by running the public launcher
    in a clean container, where this call ended the setup with a FileNotFoundError."""
    from adk import agent_daemon

    home = tmp_path / "home"
    home.mkdir()
    ops = teach_setup.StartOps(home)
    import subprocess
    import sys

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ops, "autostart_current", lambda: True)
    monkeypatch.setattr(ops, "serve_argv", lambda: ["python", "-m", "adk.home", "serve"])
    monkeypatch.setattr(agent_daemon, "LOG_DIR", tmp_path / "logs")

    def no_systemctl(*_a, **_k):
        raise FileNotFoundError(2, "No such file or directory", "systemctl")

    started: List[List[str]] = []

    def fake_popen(argv, **_k):
        started.append(list(argv))
        return SimpleNamespace(pid=1)

    monkeypatch.setattr(subprocess, "run", no_systemctl)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert ops.spawn() is True
    assert started == [["python", "-m", "adk.home", "serve"]]
