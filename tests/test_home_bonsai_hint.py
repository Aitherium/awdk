"""The Bonsai install hint matches the OS the user is on.

Clean-venv run on Windows (2026-10-01): `adk home model --local bonsai --check`
told a Windows user to run `curl ... | sh` -- there is no `sh` in PowerShell, so
the first step of the private-model path failed for every Windows first-timer.
"""

from __future__ import annotations

import httpx

from adk.home import models
from adk.home.config import ModelConfig


def test_windows_hint_is_powershell_and_runs_a_saved_file():
    h = models.bonsai_install_hint("win32")
    assert models.BONSAI_PS1 in h and "-OutFile install-bonsai.ps1" in h
    assert r"-File .\install-bonsai.ps1" in h
    assert "| sh" not in h and "| iex" not in h


def test_posix_hint_downloads_then_runs_never_pipes():
    for plat in ("linux", "darwin"):
        h = models.bonsai_install_hint(plat)
        assert models.BONSAI_SH in h and "sh install-bonsai.sh" in h
        assert "| sh" not in h


def test_describe_and_probe_use_the_os_hint(monkeypatch):
    monkeypatch.setattr(models.sys, "platform", "win32")
    cfg = models.choose_model("bonsai", base_url="http://127.0.0.1:9/v1")
    assert models.describe(cfg)["hint"].startswith("install (PowerShell)")

    def _boom(*_a, **_k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", _boom)
    detail = models.probe(cfg)["detail"]
    assert "unreachable" in detail and models.BONSAI_PS1 in detail


def test_other_presets_keep_their_hint():
    assert models.hint_for("ollama") == models.PRESETS["ollama"].hint
    assert models.hint_for("nope") == ""
    assert isinstance(models.describe(ModelConfig(provider="deepseek", mode="byo"))["hint"], str)
