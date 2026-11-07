"""``adk join --workspace``: browser sign-in, enroll-self once, bearer 600, idempotent rerun.

The device flow and the platform are mocked; nothing leaves the test.
"""

from __future__ import annotations

import os
import stat
import sys
import types
from types import SimpleNamespace

import pytest

from adk import credentials
from adk import invite_enroll as ie

USER_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhbGljZSJ9.c2ln"


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


class FakePlatform:
    def __init__(self):
        self.enrolls = 0
        self.beats = []
        self.revoked = set()

    def get(self, path):
        return _Resp(404)

    def post(self, path, json=None):  # noqa: A002 - mirrors httpx
        if path == ie.ENROLL_SELF:
            self.enrolls += 1
            return _Resp(201, {"device_id": f"dev{self.enrolls}",
                               "device_token": f"tok{self.enrolls}",
                               "workspace_id": "ws_aitherium", "profile_kind": "member",
                               "heartbeat_s": 300})
        if path == ie.HEARTBEAT:
            self.beats.append(json)
            return _Resp(410 if json["token"] in self.revoked else 200, {})
        raise AssertionError(path)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AWSETTINGS_HOME", str(tmp_path / "aws"))
    monkeypatch.delenv("AWSETTINGS_URL", raising=False)
    monkeypatch.setattr(credentials, "is_user_token", lambda t, kind="": t == USER_TOKEN)
    saved = {}
    fake_cfg = SimpleNamespace(
        save=lambda values: saved.update(values) or tmp_path / "aws" / "config.json",
        get=lambda name: "", load=lambda: dict(saved),
        member_key=lambda: str(saved.get("member", "")).lower())
    pkg = types.ModuleType("awsettings")
    pkg.config = fake_cfg
    monkeypatch.setitem(sys.modules, "awsettings", pkg)
    monkeypatch.setitem(sys.modules, "awsettings.config", fake_cfg)
    runs = []
    signed = []

    def sign_in(ws):
        signed.append(ws)
        return USER_TOKEN, {"id": "u-alice"}

    return SimpleNamespace(
        platform=FakePlatform(), runs=runs, signed=signed, sign_in=sign_in,
        run=lambda argv, **kw: runs.append(argv) or SimpleNamespace(returncode=0),
        state=tmp_path / "join.json", bearer=tmp_path / "session-bearer", tmp=tmp_path)


def _join(env, lines=None):
    return ie.run_workspace_join("ws_aitherium", client=env.platform, sign_in=env.sign_in,
                                 run=env.run, out=(lines.append if lines is not None
                                                   else lambda s: None),
                                 state_file=env.state, bearer_file=env.bearer)


def test_join_signs_in_by_browser_and_enrolls_self_once(env):
    assert _join(env) == ie.EXIT_OK
    assert env.signed == ["ws_aitherium"]
    assert env.platform.enrolls == 1
    assert env.bearer.read_text(encoding="utf-8").strip() == USER_TOKEN


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_bearer_file_is_mode_600(env):
    _join(env)
    assert stat.S_IMODE(os.stat(env.bearer).st_mode) == 0o600


def test_rerun_is_idempotent(env):
    assert _join(env) == ie.EXIT_OK
    lines = []
    assert _join(env, lines) == ie.EXIT_OK
    assert env.platform.enrolls == 1
    assert env.platform.beats and env.platform.beats[0]["token"] == "tok1"
    assert any("already joined" in ln for ln in lines)


def test_revoked_device_re_enrolls_on_next_join(env):
    _join(env)
    env.platform.revoked.add("tok1")
    assert _join(env) == ie.EXIT_OK
    assert env.platform.enrolls == 2


def test_member_settings_are_keyed_by_the_signed_in_user(env):
    from awsettings import config as aws_config
    _join(env)
    assert aws_config.load()["member"] == "u-alice"
    assert aws_config.member_key() == "u-alice"
    assert [a[a.index("--domain") + 1] for a in env.runs] == list(ie.MEMBER_DOMAINS)


def test_unfinished_sign_in_enrolls_nothing(env):
    env.sign_in = lambda ws: ("", {})
    assert _join(env) == ie.EXIT_FAIL
    assert env.platform.enrolls == 0


def test_cli_registers_workspace_on_join():
    import argparse

    from adk.cli import _register_commands
    p = argparse.ArgumentParser()
    _register_commands(p.add_subparsers(dest="command"))
    args = p.parse_args(["join", "--workspace"])
    assert args.workspace == ""
    assert p.parse_args(["join"]).workspace is None
