"""`adk home chat` turns a failed model call into one actionable line, exit 1.

Clean-machine buyer run (2026-09-30): with a wrong ANTHROPIC_API_KEY, step
`adk home chat "hello"` printed a 40-line httpx traceback. The init hint also
pointed a buyer at a Bonsai server they do not run; it now names the shop's path.
"""

from __future__ import annotations

import argparse

import httpx
import pytest

from adk.home import cli as home_cli
from adk.home import config as hc
from adk.home import harness

SECRET_LOOKING = "placeholder-value-for-tests"


class _Agent:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def chat(self, _message: str):
        raise self.exc


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return httpx.HTTPStatusError(f"{code}", request=req,
                                 response=httpx.Response(code, request=req))


@pytest.fixture
def byo_home(monkeypatch, tmp_path):
    monkeypatch.setenv(hc.HOME_ENV, str(tmp_path / "home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET_LOOKING)
    assert home_cli.main(["init"]) == home_cli.EXIT_OK
    assert home_cli.main(["model", "--byo", "anthropic"]) == home_cli.EXIT_OK
    return monkeypatch


@pytest.mark.parametrize("exc, want", [
    (_status_error(401), "rejected the key in $ANTHROPIC_API_KEY (HTTP 401)"),
    (_status_error(500), "answered HTTP 500"),
    (httpx.ConnectError("refused"), "could not reach the anthropic API (ConnectError)"),
])
def test_chat_model_failure_is_one_line_exit_1(byo_home, capsys, exc, want):
    byo_home.setattr(harness, "build_native_agent", lambda _cfg: _Agent(exc))
    rc = home_cli.cmd_chat(argparse.Namespace(message="hello"))
    err = capsys.readouterr().err
    assert rc == home_cli.EXIT_FAIL
    assert want in err
    assert "Traceback" not in err and SECRET_LOOKING not in err
    assert len(err.strip().splitlines()) == 1


def test_chat_success_still_prints_the_reply(byo_home, capsys):
    class _Ok:
        async def chat(self, _m):
            return type("R", (), {"content": "hi there"})()

    byo_home.setattr(harness, "build_native_agent", lambda _cfg: _Ok())
    assert home_cli.cmd_chat(argparse.Namespace(message="hello")) == home_cli.EXIT_OK
    assert capsys.readouterr().out.strip() == "hi there"


def test_init_hint_installs_bonsai_before_pointing_at_it(monkeypatch, tmp_path, capsys):
    """The 09-30 bug was pointing a buyer at a Bonsai server they did not run. The
    hint now names the install step FIRST, then `--local bonsai`; BYO stays offered."""
    monkeypatch.setenv(hc.HOME_ENV, str(tmp_path / "home"))
    assert home_cli.main(["init"]) == home_cli.EXIT_OK
    out = capsys.readouterr().out
    assert "adk home model --byo anthropic" in out and "adk home chat" in out
    assert "install-bonsai" in out
    assert out.index("install-bonsai") < out.index("--local bonsai")
