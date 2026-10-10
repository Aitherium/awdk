"""appliance-*: the owner reaches a tenant appliance this host deployed, by NAME only.

The engine, container, repo dir and deploy script come from the host's own
``~/.aither/appliances.json`` (written by the tenant repo's deploy script); the
server supplies only a name. These pin: a name off the registry is refused before
anything runs; a malicious registry entry is dropped as if absent; every argv is
exactly the fixed shape built from the entry; the deploy runs detached and its exit
code comes back through appliance-status; the channel's signing is unchanged.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from adk import node_commands as nc

KEY = "ab" * 32
NODE = "home-pc"
SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "aither"
    h.mkdir()
    monkeypatch.setenv("AITHER_HOME", str(h))
    return h


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "acme-repo"
    (r / "deploy").mkdir(parents=True)
    (r / ".git").mkdir()
    (r / "deploy" / "deploy.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (r / "deploy" / "deploy.ps1").write_text("Write-Host hi\n", encoding="utf-8")
    return r


def _entry(repo, **over):
    e = {"name": "acmebot", "repo_dir": str(repo), "script": "deploy.sh",
         "engine": "docker", "container": "acmebot"}
    e.update(over)
    return e


def _register(home, *entries, raw=None):
    apps = raw if raw is not None else {e["name"]: e for e in entries}
    (home / "appliances.json").write_text(json.dumps({"appliances": apps}), encoding="utf-8")


def _cmd(args, verb="appliance-status", cid="cmd_a1"):
    c = {"id": cid, "tenant_id": "tnt_acme", "node_id": NODE, "verb": verb, "args": args,
         "issued_by": "u-owner", "issued_at": int(time.time()),
         "expires_at": int(time.time()) + 600}
    c["sig"] = nc.sign(KEY, nc.canonical(c))
    return c


class _Proc:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = rc, stdout, stderr


@pytest.fixture
def tools(monkeypatch):
    """shutil.which finds fixed fake binaries; subprocess.run is recorded."""
    import shutil

    found = {"docker": "/usr/bin/docker", "podman": "/usr/bin/podman", "git": "/usr/bin/git",
             "bash": "/bin/bash", "pwsh": "/usr/bin/pwsh"}
    monkeypatch.setattr(shutil, "which", lambda name: found.get(name))
    calls = []
    answers = {}

    def fake_run(argv, **kw):
        calls.append((list(argv), kw))
        for prefix, proc in answers.items():
            if tuple(argv[1:1 + len(prefix)]) == prefix:
                return proc
        return _Proc(0, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return {"calls": calls, "answers": answers, "found": found}


# ── the registry ────────────────────────────────────────────────────────────


def test_a_valid_entry_is_loaded_and_published(home, repo):
    _register(home, _entry(repo))
    assert nc.load_appliances()["acmebot"]["container"] == "acmebot"
    assert nc.appliance_names() == ["acmebot"]


def test_no_registry_means_no_appliances(home):
    assert nc.load_appliances() == {}
    (home / "appliances.json").write_text("not json", encoding="utf-8")
    assert nc.appliance_names() == []


def test_a_bom_written_registry_still_reads(home, repo):
    text = json.dumps({"appliances": {"acmebot": _entry(repo)}})
    (home / "appliances.json").write_bytes(b"\xef\xbb\xbf" + text.encode())
    assert nc.appliance_names() == ["acmebot"]


@pytest.mark.parametrize("over", [
    {"repo_dir": "relative/acme"},
    {"repo_dir": "/definitely/not/here/acme"},
    {"repo_dir": "\\\\evil-host\\share\\acme"},
    {"repo_dir": "//evil-host/share/acme"},
    {"repo_dir": 7},
    {"engine": "sh"},
    {"engine": "/usr/bin/docker"},
    {"engine": "docker; rm -rf /"},
    {"container": "--format={{.Config.Env}}"},
    {"container": "-acmebot"},
    {"container": "acme bot"},
    {"container": "acme;rm"},
    {"container": "acme/../x"},
    {"container": ""},
    {"script": "../../evil.sh"},
    {"script": "deploy.bat"},
    {"script": "/bin/sh"},
    {"name": "otherbot"},
])
def test_a_malicious_registry_entry_is_dropped_as_if_absent(home, repo, over):
    _register(home, raw={"acmebot": _entry(repo, **over)})
    assert nc.load_appliances() == {}
    assert "not on this host" in nc.verify(_cmd({"name": "acmebot"}), KEY, NODE, seen=[])


@pytest.mark.parametrize("name", ["Acme Bot", "../acme", "-acme", "", "g" * 65, "acme\n"])
def test_a_badly_named_entry_is_dropped(home, repo, name):
    _register(home, raw={name: _entry(repo, name=name)})
    assert nc.load_appliances() == {}


def test_the_registry_is_capped(home, tmp_path):
    raw = {}
    for i in range(nc.MAX_APPLIANCES + 5):
        r = tmp_path / f"r{i}"
        r.mkdir()
        raw[f"app{i}"] = {"name": f"app{i}", "repo_dir": str(r), "script": "deploy.sh",
                          "engine": "podman", "container": f"app{i}"}
    _register(home, raw=raw)
    assert len(nc.appliance_names()) == nc.MAX_APPLIANCES


def test_the_heartbeat_publishes_the_registered_names(home, repo, monkeypatch):
    import asyncio

    from adk import enrollment, node_beat

    monkeypatch.setattr(node_beat, "install_autostart", lambda: {"autostart": "test-disabled"})
    _register(home, _entry(repo))
    posts = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"status": "ok"}

    class _Client:
        async def post(self, url, json=None, headers=None):
            posts.append((url, json))
            return _Resp()

    monkeypatch.setattr(enrollment, "build_registration", lambda node_id, **kw: {
        "node_id": node_id, "inference_ready": False, "available_models": [],
        "gpu_vram_mb": 0, "inference_url": "", "inference_kind": "none"})
    asyncio.run(enrollment._heartbeat_beats(
        _Client(), "https://idp.test", {"Authorization": "Bearer t"}, NODE, interval=0,
        inference_url=None, node_class="laptop", max_beats=1, reach_provider=None,
        harness_provider=None, beat_immediately=True))
    beat = [b for u, b in posts if u.endswith("/heartbeat")][0]
    assert beat["appliances"] == ["acmebot"]


# ── verify: the name must be on THIS host's registry ───────────────────────


def test_verify_accepts_only_a_registered_name(home, repo):
    _register(home, _entry(repo))
    for verb in ("appliance-status", "appliance-logs", "appliance-redeploy"):
        assert verb in nc.VERBS
        assert nc.verify(_cmd({"name": "acmebot"}, verb=verb), KEY, NODE, seen=[]) == ""
        assert "not on this host" in nc.verify(_cmd({"name": "otherbot"}, verb=verb), KEY,
                                               NODE, seen=[])
        assert "takes exactly name" in nc.verify(_cmd({}, verb=verb), KEY, NODE, seen=[])
        assert "takes exactly name" in nc.verify(
            _cmd({"name": "acmebot", "repo_dir": "/tmp"}, verb=verb), KEY, NODE, seen=[])


def test_an_unknown_name_runs_nothing_and_answers_nothing(home, repo):
    _register(home, _entry(repo))
    ran = []
    handlers = {v: (lambda a, v=v: ran.append(v) or {"ok": True}) for v in nc.VERBS}
    assert nc.run_commands([_cmd({"name": "otherbot"})], NODE, KEY, handlers=handlers) == []
    assert ran == []


def test_signing_is_unchanged_by_the_new_verbs():
    rec = {"id": "cmd_x", "tenant_id": "t", "node_id": "n", "verb": "appliance-status",
           "args": {"name": "acmebot"}, "issued_by": "u", "issued_at": 1, "expires_at": 2,
           "status": "queued"}
    assert nc.canonical(rec) == (
        b'{"args":{"name":"acmebot"},"expires_at":2,"id":"cmd_x","issued_at":1,'
        b'"issued_by":"u","node_id":"n","tenant_id":"t","verb":"appliance-status"}')
    assert nc.SIGNED_FIELDS == ("id", "tenant_id", "node_id", "verb", "args", "issued_by",
                                "issued_at", "expires_at")
    assert nc.RESULT_FIELDS == ("id", "node_id", "ok", "output")


# ── appliance-status ───────────────────────────────────────────────────────


def test_status_runs_exactly_inspect_and_rev_parse(home, repo, tools):
    _register(home, _entry(repo))
    inspect = [{"State": {"Status": "running", "Running": True, "ExitCode": 0,
                          "StartedAt": "2026-10-08T00:00:00Z",
                          "Health": {"Status": "healthy"}},
                "RestartCount": 1, "Config": {"Image": "acmebot-app:local"}}]
    tools["answers"][("inspect",)] = _Proc(0, json.dumps(inspect))
    tools["answers"][("-C",)] = _Proc(0, SHA + "\n")
    out = nc._appliance_status({"name": "acmebot"})
    argvs = [c[0] for c in tools["calls"]]
    assert argvs == [["/usr/bin/docker", "inspect", "--type", "container", "acmebot"],
                     ["/usr/bin/git", "-C", str(repo.resolve()), "rev-parse", "HEAD"]]
    assert all("shell" not in kw for _, kw in tools["calls"])
    assert out["ok"] is True and out["commit"] == SHA
    assert out["container_state"]["health"] == "healthy"
    assert out["container_state"]["running"] is True


def test_status_of_a_missing_container_is_reported_not_raised(home, repo, tools):
    _register(home, _entry(repo, engine="podman"))
    tools["answers"][("inspect",)] = _Proc(125, "", "no such container acmebot")
    out = nc._appliance_status({"name": "acmebot"})
    assert tools["calls"][0][0][0] == "/usr/bin/podman"
    assert out["ok"] is False and out["container_state"]["found"] is False


def test_status_never_raises(home, monkeypatch):
    def boom():
        raise RuntimeError("disk gone")
    monkeypatch.setattr(nc, "load_appliances", boom)
    out = nc._appliance_status({"name": "acmebot"})
    assert out["ok"] is False and "disk gone" in out["error"]


def test_status_for_an_unregistered_name_runs_nothing(home, repo, tools):
    _register(home, _entry(repo))
    assert nc._appliance_status({"name": "otherbot"})["ok"] is False
    assert tools["calls"] == []


# ── appliance-logs ─────────────────────────────────────────────────────────


def test_logs_run_exactly_the_tail_argv_and_fit_the_channel(home, repo, tools):
    _register(home, _entry(repo))
    huge = "".join(f"line {i} — \x1b[1mbold\x1b[0m\n" for i in range(5000))
    tools["answers"][("logs",)] = _Proc(0, huge)
    (res,) = nc.run_commands([_cmd({"name": "acmebot"}, verb="appliance-logs")], NODE, KEY)
    argv, kw = tools["calls"][0]
    assert argv == ["/usr/bin/docker", "logs", "--tail", "200", "acmebot"]
    assert kw.get("stderr") == subprocess.STDOUT
    assert len(res["output"]) <= nc.MAX_OUTPUT_CHARS
    body = json.loads(res["output"])  # still whole JSON after truncation
    assert res["ok"] is True and body["logs"].rstrip().endswith("line 4999 — \x1b[1mbold"
                                                                 "\x1b[0m")


# ── appliance-redeploy ─────────────────────────────────────────────────────


@pytest.fixture
def popen(monkeypatch):
    class _Started(list):
        behaviour: dict

    started = _Started()
    behaviour = {"rc": 0, "hang": False, "log": ""}

    class _P:
        pid = 4242

        def __init__(self, kw):
            self.kw, self.killed = kw, False
            out = kw.get("stdout")
            if behaviour["log"] and hasattr(out, "write"):
                out.write(behaviour["log"])
                out.flush()

        def wait(self, timeout=None):
            if behaviour["hang"] and not self.killed:
                raise subprocess.TimeoutExpired("deploy", timeout)
            return behaviour["rc"]

        def kill(self):
            self.killed = True

    def fake_popen(argv, **kw):
        started.append((list(argv), kw))
        return _P(kw)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    started.behaviour = behaviour
    return started


def test_redeploy_refuses_a_repo_that_is_not_git(home, repo, tools, popen):
    (repo / ".git").rmdir()
    _register(home, _entry(repo))
    out = nc._appliance_redeploy({"name": "acmebot"})
    assert out["ok"] is False and "not a git repository" in out["error"]
    assert tools["calls"] == [] and popen == []


def test_redeploy_refuses_a_missing_script(home, repo, tools, popen):
    (repo / "deploy" / "deploy.sh").unlink()
    _register(home, _entry(repo))
    out = nc._appliance_redeploy({"name": "acmebot"})
    assert out["ok"] is False and "missing" in out["error"]
    assert tools["calls"] == [] and popen == []


def test_a_failed_pull_never_starts_the_deploy(home, repo, tools, popen):
    _register(home, _entry(repo))
    tools["answers"][("-C", str(repo.resolve()), "pull")] = _Proc(1, "", "not fast-forward")
    out = nc._appliance_redeploy({"name": "acmebot"})
    assert out["ok"] is False and out["pull"]["rc"] == 1 and popen == []


def test_redeploy_pulls_ff_only_then_starts_the_detached_worker(home, repo, tools, popen):
    _register(home, _entry(repo))
    tools["answers"][("-C", str(repo.resolve()), "rev-parse")] = _Proc(0, SHA)
    out = nc._appliance_redeploy({"name": "acmebot"})
    assert out["ok"] is True and out["deploy"]["pid"] == 4242
    pull = [c for c in tools["calls"] if "pull" in c[0]]
    assert [c[0] for c in pull] == [["/usr/bin/git", "-C", str(repo.resolve()), "pull",
                                     "--ff-only"]]
    assert pull[0][1]["env"]["GIT_TERMINAL_PROMPT"] == "0"
    (argv, kw), = popen
    assert argv == [sys.executable, "-m", "adk.node_commands", "appliance-redeploy-worker",
                    "acmebot"]
    assert kw["cwd"] == str(home) and kw["stdin"] == subprocess.DEVNULL
    # A second redeploy while the first runs is refused, and runs nothing.
    again = nc._appliance_redeploy({"name": "acmebot"})
    assert again["ok"] is False and "already running" in again["error"] and len(popen) == 1


@pytest.mark.parametrize("script,want", [
    ("deploy.sh", lambda s: ["/bin/bash", s]),
    ("deploy.ps1", lambda s: ["/usr/bin/pwsh", "-NoProfile", "-NonInteractive"]
     + (["-ExecutionPolicy", "Bypass"] if sys.platform == "win32" else []) + ["-File", s]),
])
def test_the_worker_runs_the_registered_script_and_status_reports_its_rc(
        home, repo, tools, popen, script, want):
    _register(home, _entry(repo, script=script, engine="podman"))
    popen.behaviour.update(rc=3, log="build ok\nAuthorization: Bearer eyJhbGciOi.payload.sig\n"
                                      "DB_PASSWORD=hunter2 key AKI" "AABCDEFGHIJKLMNOP\n")
    assert nc._redeploy_worker("acmebot") == 1
    (argv, kw), = popen
    assert argv == want(str((repo / "deploy" / script).resolve()))
    assert kw["cwd"] == str(repo.resolve()) and kw["env"]["CONTAINER_ENGINE"] == "podman"
    if sys.platform != "win32":
        assert kw["start_new_session"] is True  # its own group: a timeout kills the tree
    status = nc._appliance_status({"name": "acmebot"})
    last = status["last_redeploy"]
    assert last["rc"] == 3 and last["state"] == "failed"
    assert "build ok" in last["tail"]
    for leaked in ("eyJhbGciOi", "hunter2", "AKI" "AABCDEFGHIJKLMNOP"):
        assert leaked not in last["tail"], leaked


def test_a_timed_out_deploy_has_its_whole_tree_killed(home, repo, tools, popen, monkeypatch):
    _register(home, _entry(repo))
    popen.behaviour.update(hang=True)
    killed = []
    if sys.platform == "win32":
        tools["answers"][("/T",)] = _Proc(0)
    else:
        monkeypatch.setattr(nc.os, "killpg", lambda pid, sig: killed.append((pid, sig)),
                            raising=False)
    assert nc._redeploy_worker("acmebot") == 1
    if sys.platform == "win32":
        assert [c[0] for c in tools["calls"]] == [["taskkill", "/T", "/F", "/PID", "4242"]]
    else:
        import signal
        assert killed == [(4242, signal.SIGKILL)]
    last = nc._read_state("acmebot")
    assert last["state"] == "failed" and "timed out" in last["error"]


def test_a_redeploy_is_refused_while_a_live_worker_holds_the_state(home, repo, tools, popen,
                                                                   monkeypatch):
    import os

    _register(home, _entry(repo))
    nc._write_state("acmebot", {"state": "running", "started_at": int(time.time()),
                                "pid": os.getpid()})  # this test process: alive
    out = nc._appliance_redeploy({"name": "acmebot"})
    assert out["ok"] is False and "already running" in out["error"] and popen == []
    # The same state with a dead worker is stale: the redeploy goes ahead.
    monkeypatch.setattr(nc, "_pid_alive", lambda pid: False)
    assert nc._appliance_redeploy({"name": "acmebot"})["ok"] is True and len(popen) == 1


def test_pid_alive_tells_a_live_process_from_a_gone_one():
    import os

    assert nc._pid_alive(os.getpid()) is True
    assert nc._pid_alive(0) is False and nc._pid_alive("x") is False
    assert nc._pid_alive(2 ** 22 + 12345) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_state_and_log_files_are_owner_only(home, repo, tools, popen):
    _register(home, _entry(repo))
    popen.behaviour.update(log="x\n")
    nc._redeploy_worker("acmebot")
    d = home / "appliances"
    assert d.stat().st_mode & 0o777 == 0o700
    for f in (d / "acmebot.redeploy.json", d / "acmebot.redeploy.log"):
        assert f.stat().st_mode & 0o777 == 0o600, f


def test_git_never_waits_on_a_person():
    env = nc._git_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GCM_INTERACTIVE"] == "never"
    assert env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"


def test_appliance_logs_are_redacted(home, repo, tools):
    _register(home, _entry(repo))
    tools["answers"][("logs",)] = _Proc(0, "\n".join([
        "GET /api Authorization: Bearer abc.DEF-123_xyz",
        "boot password=s3cret-pw passwd=pw2 secret=topsecret token=tok123",
        '{"client_secret": "cs-value", "ACCESS_TOKEN":"at-value"}',
        "aws AKI" "AIOSFODNN7EXAMPLE ok",
        "clone https://user:" + "ghtoken" + "@github.com/o/r.git",
        "plain line stays",
    ]))
    out = nc._appliance_logs({"name": "acmebot"})
    for leaked in ("abc.DEF-123_xyz", "s3cret-pw", "pw2", "topsecret", "tok123", "cs-value",
                   "at-value", "AKI" "AIOSFODNN7EXAMPLE", "ghtoken"):
        assert leaked not in out["logs"], leaked
    assert "plain line stays" in out["logs"] and "[REDACTED]" in out["logs"]


def test_on_windows_deploy_sh_runs_under_git_bash_never_the_wsl_launcher(
        tmp_path, monkeypatch):
    import shutil

    git_root = tmp_path / "Git"
    (git_root / "cmd").mkdir(parents=True)
    (git_root / "bin").mkdir()
    (git_root / "bin" / "bash.exe").write_text("", encoding="utf-8")
    found = {"bash": "C:\\Windows\\System32\\bash.exe", "git": str(git_root / "cmd" / "git.exe")}
    monkeypatch.setattr(shutil, "which", lambda name: found.get(name))
    monkeypatch.setattr(nc, "os", _os_named("nt"))
    assert nc._bash() == str(git_root / "bin" / "bash.exe")
    found.pop("git")
    assert nc._bash() is None  # no Git bash: refused, never the WSL launcher


def test_the_worker_refuses_a_name_off_the_registry(home, repo, tools):
    _register(home, _entry(repo))
    assert nc._redeploy_worker("otherbot") == 2
    assert nc._redeploy_worker("../etc") == 2
    assert tools["calls"] == []
    assert not (Path(home) / "appliances" / "..etc.redeploy.json").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need developer mode on Windows")
def test_a_symlinked_script_out_of_the_repo_is_refused(home, repo, tmp_path, tools, popen):
    outside = tmp_path / "evil.sh"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    (repo / "deploy" / "deploy.sh").unlink()
    (repo / "deploy" / "deploy.sh").symlink_to(outside)
    _register(home, _entry(repo))
    out = nc._appliance_redeploy({"name": "acmebot"})
    assert out["ok"] is False and popen == []


def _os_named(name):
    """A stand-in ``os`` that reports ``name``, for the module under test only.

    Patching the real ``os.name`` changes it for the whole process: on 3.10 every
    ``Path()`` built meanwhile -- pytest's own report formatting included -- picks the
    wrong flavour and raises "cannot instantiate 'WindowsPath' on your system", which
    crashed the ADK payload run with an INTERNALERROR (2026-10-08).
    """
    import os as _real_os
    import types as _types

    fake = _types.ModuleType("os")
    fake.__dict__.update(_real_os.__dict__)
    fake.name = name
    return fake
