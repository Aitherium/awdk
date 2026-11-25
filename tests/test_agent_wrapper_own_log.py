"""The Windows logon wrapper must not redirect into the log `adk up` itself opens.

Measured 2026-11-16 in a clean Windows Sandbox: after the installer's `adk up`, the
`AitherAgent` logon task ran `aither-agent.cmd` (`python -m adk.cli up ... 1>>agent.log`),
`adk up` then opened the same agent.log for the detached server, and Windows refused
("PermissionError: [Errno 13] ... agent.log": cmd holds its `>>` handle for as long as
`adk up` runs). Last Result 1 at every logon: the agent never came back after a reboot.
"""

from __future__ import annotations

import re
import sys

from adk import agent_daemon as d


def test_wrapper_redirects_to_its_own_log_not_the_servers(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(d, "AITHER_HOME", home)
    monkeypatch.setattr(d, "LOG_DIR", home / "logs")

    d._install_windows_task(["python", "-m", "adk.cli", "up", "--yes"], dry_run=True)

    text = (home / "aither-agent.cmd").read_text(encoding="utf-8")
    target = re.search(r'1>>"([^"]+)"', text)
    assert target, text
    redirect = target.group(1).replace("\\", "/")
    # the file `adk up` opens for the server (cli.cmd_up: daemon.LOG_DIR / "agent.log")
    assert not redirect.endswith("/agent.log"), redirect
    assert redirect.endswith("/agent-launch.log"), redirect
