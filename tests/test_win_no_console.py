"""The harness daemon's console children must not open terminal tabs."""

from __future__ import annotations

import subprocess
import sys

import pytest

from adk.harnesses import _win_no_console as wnc

NO_WINDOW = 0x08000000


def test_plain_child_gets_no_window():
    assert wnc.patched_flags(0) & NO_WINDOW


def test_existing_flags_are_kept():
    assert wnc.patched_flags(0x200) == 0x200 | NO_WINDOW  # CREATE_NEW_PROCESS_GROUP


@pytest.mark.parametrize("explicit", [0x10, 0x8])  # CREATE_NEW_CONSOLE, DETACHED_PROCESS
def test_explicit_console_choice_is_respected(explicit):
    assert wnc.patched_flags(explicit) == explicit


def test_serve_installs_the_guard():
    from pathlib import Path

    src = Path(wnc.__file__).with_name("daemon.py").read_text(encoding="utf-8")
    serve = src[src.index("def serve("):]
    assert "_win_no_console" in serve[: serve.index("bind_host =")]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only patch")
def test_install_patches_popen(monkeypatch):
    seen = {}
    original = subprocess.Popen.__init__

    def fake_init(self, *args, **kwargs):
        seen["flags"] = kwargs.get("creationflags")
        raise RuntimeError("stop")

    monkeypatch.setattr(subprocess.Popen, "__init__", fake_init)
    monkeypatch.setattr(wnc, "_installed", False)
    try:
        assert wnc.install(force=True)
        with pytest.raises(RuntimeError):
            subprocess.Popen(["git", "--version"])
        assert seen["flags"] & NO_WINDOW
    finally:
        subprocess.Popen.__init__ = original  # type: ignore[method-assign]
        wnc._installed = False
