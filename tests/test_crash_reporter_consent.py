"""A crash report is sent only when a person at a terminal says yes.

The reporter used to default to yes, answer yes by itself when there was no terminal,
and include the machine's hostname. Its fallback opens a public GitHub issue.
"""
import builtins
import io
import sys

import pytest

from adk.shell import crash_reporter as cr


class _Tty(io.StringIO):
    def isatty(self):
        return True


class _NoTty(io.StringIO):
    def isatty(self):
        return False


def _answer(monkeypatch, text):
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr(builtins, "input", lambda prompt="": text)
    return cr._ask_confirm("send? ")


def test_no_terminal_means_no(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _NoTty())
    monkeypatch.setattr(builtins, "input", lambda prompt="": pytest.fail("must not prompt without a terminal"))
    assert cr._ask_confirm("send? ") is False


@pytest.mark.parametrize("text", ["", " ", "n", "no", "maybe", "Y es"])
def test_anything_but_yes_means_no(monkeypatch, text):
    assert _answer(monkeypatch, text) is False


@pytest.mark.parametrize("text", ["y", "Y", "yes", " YES "])
def test_yes_means_yes(monkeypatch, text):
    assert _answer(monkeypatch, text) is True


def test_interrupt_means_no(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _Tty())

    def boom(prompt=""):
        raise EOFError

    monkeypatch.setattr(builtins, "input", boom)
    assert cr._ask_confirm("send? ") is False


def test_report_carries_no_hostname():
    import platform

    try:
        raise ValueError("boom")
    except ValueError as exc:
        report = cr._build_report(exc)
    assert "hostname" not in report["context"]
    node = platform.node()
    if node:
        assert node not in repr({k: v for k, v in report["context"].items() if k != "command"})


def test_declined_crash_sends_nothing(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", _NoTty())
    monkeypatch.setattr(cr, "_submit_via_genesis", lambda report: pytest.fail("must not submit"))
    monkeypatch.setattr(cr, "_submit_via_gh_cli", lambda report: pytest.fail("must not open an issue"))
    cr.handle_crash(RuntimeError("x"))
    assert "No report sent" in capsys.readouterr().err
