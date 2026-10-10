"""ConPTY's startup DA1 handshake is answered by the daemon (2026-10-10).

Measured that day (pywinpty 3.0.5, Windows 11): ConPTY opens with ``ESC[c`` and
holds every byte after it until the terminal answers. With no xterm attached at
that instant, a terminal opened from the OS IDE printed its banner and then nothing,
ever. The daemon answers once and drops the query from the stream; the second test
pins that a later ``ESC[c`` from a program is passed through untouched.
"""

from __future__ import annotations

import sys

import pytest

from adk.harnesses import proc_priority
from adk.harnesses.pty_session import PtyHarnessSession
from adk.harnesses.registry import SPECS
from adk.harnesses.session import SessionConfig


class _FakePty:
    kind = "winpty"

    def __init__(self) -> None:
        self.written: list[str] = []

    def write(self, data: str) -> int:
        self.written.append(data)
        return len(data)


def _session(tmp_path) -> PtyHarnessSession:
    return PtyHarnessSession(SPECS["terminal"], SessionConfig(harness="terminal"), root=tmp_path)


def test_the_startup_query_is_answered_once_and_stripped(tmp_path):
    s, pty = _session(tmp_path), _FakePty()
    out = s._answer_conpty_handshake(pty, "\x1b[1t\x1b[c\x1b[?1004h")
    assert out == "\x1b[1t\x1b[?1004h"
    assert pty.written == ["\x1b[?1;0c"]
    # Answered once: a program that asks later gets the real terminal's answer.
    assert s._answer_conpty_handshake(pty, "app asks \x1b[c") == "app asks \x1b[c"
    assert pty.written == ["\x1b[?1;0c"]


def test_a_query_after_the_startup_window_is_program_output(tmp_path):
    s, pty = _session(tmp_path), _FakePty()
    assert s._answer_conpty_handshake(pty, "x" * 5000) == "x" * 5000
    assert s._answer_conpty_handshake(pty, "\x1b[c") == "\x1b[c"
    assert pty.written == []


def test_posix_ptys_are_left_alone(tmp_path):
    s, pty = _session(tmp_path), _FakePty()
    pty.kind = "ptyprocess"
    assert s._answer_conpty_handshake(pty, "\x1b[c") == "\x1b[c"
    assert pty.written == []


def test_priority_is_raised_through_the_three_windows_calls():
    calls = []

    class K32:
        def GetCurrentProcess(self):  # noqa: N802 -- the Win32 name
            return -1

        def SetPriorityClass(self, h, cls):  # noqa: N802 -- the Win32 name
            calls.append(("cpu", cls))
            return 1

        def SetProcessInformation(self, h, kind, ptr, size):  # noqa: N802 -- the Win32 name
            calls.append(("memory", kind))
            return 1

    class Nt:
        def NtSetInformationProcess(self, h, kind, ptr, size):  # noqa: N802 -- the Win32 name
            calls.append(("io", kind))
            return 0

    done = proc_priority.normalize_process_priority((K32(), Nt()))
    assert done == {"cpu": True, "io": True, "memory": True}
    assert ("cpu", proc_priority.NORMAL_PRIORITY_CLASS) in calls


@pytest.mark.skipif(sys.platform == "win32", reason="the no-op is the non-Windows path")
def test_priority_is_a_noop_off_windows():
    assert proc_priority.normalize_process_priority() == {}
