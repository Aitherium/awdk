"""`adk claude setup` / `adk claude doctor`: the checker can fail, and setup is dry-runnable."""

from __future__ import annotations

import json
from pathlib import Path

from adk import claude_code_doctor as ccd


def _write(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")


def _codes(rep) -> set[str]:
    return {f.code for f in rep.findings}


def test_self_test_passes():
    assert ccd.self_test() == 0


def test_automode_in_project_scope_is_dead(tmp_path):
    home, proj = tmp_path / "h", tmp_path / "p"
    (proj / ".git").mkdir(parents=True)
    _write(proj / ".claude" / "settings.local.json", {"autoMode": {"allow": []}})
    rep = ccd.run_doctor(
        home, proj, timing=False, managed=tmp_path / "none.json",
        use_binary=False, probe_devices=False,
    )
    assert "CCD001" in _codes(rep)
    assert rep.exit_code == 1


def test_automode_in_user_scope_is_fine(tmp_path):
    home, proj = tmp_path / "h", tmp_path / "p"
    (proj / ".git").mkdir(parents=True)
    _write(home / ".claude" / "settings.json", {"autoMode": {"allow": []}})
    rep = ccd.run_doctor(
        home, proj, timing=False, managed=tmp_path / "none.json",
        use_binary=False, probe_devices=False,
    )
    assert rep.exit_code == 0, rep.findings


def test_duplicate_hook_across_scopes(tmp_path):
    home, proj = tmp_path / "h", tmp_path / "p"
    (proj / ".git").mkdir(parents=True)
    hooks = {"Stop": [{"hooks": [{"type": "command", "command": "echo  hi"}]}]}
    _write(home / ".claude" / "settings.json", {"hooks": hooks})
    hooks2 = {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}
    _write(proj / ".claude" / "settings.json", {"hooks": hooks2})
    rep = ccd.run_doctor(
        home, proj, timing=False, managed=tmp_path / "none.json",
        use_binary=False, probe_devices=False,
    )
    assert "CCD002" in _codes(rep)


def test_mcpjson_ghost_and_omission(tmp_path):
    home, proj = tmp_path / "h", tmp_path / "p"
    (proj / ".git").mkdir(parents=True)
    _write(proj / ".claude" / "settings.json", {"enabledMcpjsonServers": ["ghost"]})
    _write(proj / ".mcp.json", {"mcpServers": {"real": {}, "off": {}}})
    _write(proj / ".claude" / "settings.local.json", {"disabledMcpjsonServers": ["off"]})
    rep = ccd.run_doctor(
        home, proj, timing=False, managed=tmp_path / "none.json",
        use_binary=False, probe_devices=False,
    )
    msgs = [f.message for f in rep.findings if f.code == "CCD003"]
    assert any("ghost" in m for m in msgs)
    assert any("'real'" in m for m in msgs)
    assert not any("'off'" in m for m in msgs)


def test_unknown_key_and_allowlist(tmp_path):
    home, proj = tmp_path / "h", tmp_path / "p"
    (proj / ".git").mkdir(parents=True)
    _write(home / ".claude" / "settings.json", {"aitherLane": "x", "modle": "opus"})
    rep = ccd.run_doctor(
        home, proj, timing=False, managed=tmp_path / "none.json",
        use_binary=False, probe_devices=False,
    )
    msgs = [f.message for f in rep.findings if f.code == "CCD005"]
    assert any("'modle'" in m for m in msgs)
    assert not any("aitherLane" in m for m in msgs)


def test_voice_is_warning_only(tmp_path):
    home, proj = tmp_path / "h", tmp_path / "p"
    (proj / ".git").mkdir(parents=True)
    _write(home / ".claude" / "settings.json", {"voice": {"enabled": True}})
    _write(home / ".claude.json", {})
    rep = ccd.run_doctor(
        home, proj, timing=False, managed=tmp_path / "none.json",
        use_binary=False, probe_devices=False,
    )
    assert "CCD006" in _codes(rep)
    assert rep.exit_code == 0  # warn only


def test_no_settings_is_unjudged(tmp_path):
    home, proj = tmp_path / "h", tmp_path / "p"
    (proj / ".git").mkdir(parents=True)
    rep = ccd.run_doctor(
        home, proj, timing=False, managed=tmp_path / "none.json",
        use_binary=False, probe_devices=False,
    )
    assert rep.exit_code == 2


def test_setup_dry_run_changes_nothing(tmp_path, capsys):
    rc = ccd.run_setup(tmp_path, dry_run=True, use_cli=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "would merge" in out and ccd.PLUGIN_ID in out
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_setup_merges_plugin_without_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(ccd.shutil, "which", lambda name: None)
    _write(tmp_path / ".claude" / "settings.json", {"theme": "dark"})
    assert ccd.run_setup(tmp_path, dry_run=False, use_cli=False) == 0
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert data["enabledPlugins"][ccd.PLUGIN_ID] is True
    assert ccd.MARKETPLACE in data["extraKnownMarketplaces"]


def test_cli_registers_setup_and_doctor():
    from adk.cli import get_parser

    args = get_parser().parse_args(["claude", "doctor", "--no-timing", "--json"])
    assert args.claude_command == "doctor" and args.no_timing and args.json
    args = get_parser().parse_args(["claude", "setup", "--dry-run"])
    assert args.claude_command == "setup" and args.dry_run
