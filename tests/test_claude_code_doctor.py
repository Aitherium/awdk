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


# --- setup: the awsettings step ---------------------------------------------------


class _Proc:
    def __init__(self, rc: int, out: str = "", err: str = ""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _fake_awsettings(monkeypatch, list_result: _Proc) -> list[list[str]]:
    """shutil.which finds only awsettings; subprocess.run records every argv."""
    calls: list[list[str]] = []

    def run(argv, **_kw):
        calls.append(list(argv))
        if list(argv[1:3]) == ["preset", "list"]:
            return list_result
        return _Proc(0, "applied")

    monkeypatch.setattr(ccd.shutil, "which", lambda n: "awsettings" if n == "awsettings" else None)
    monkeypatch.setattr(ccd.subprocess, "run", run)
    return calls


def _applied(calls: list[list[str]]) -> bool:
    return any(c[1:3] == ["preset", "apply"] for c in calls)


def test_setup_skips_awsettings_without_preset_verb(tmp_path, monkeypatch, capsys):
    err = "awsettings: error: argument cmd: invalid choice: 'preset' (choose from status)"
    calls = _fake_awsettings(monkeypatch, _Proc(2, "", err))
    assert ccd.run_setup(tmp_path, dry_run=False, use_cli=False) == 0
    out = capsys.readouterr().out
    assert "skipped: awsettings preset apply" in out and "no `preset` verb" in out
    assert not _applied(calls)


def test_setup_skips_awsettings_that_lacks_the_preset(tmp_path, monkeypatch, capsys):
    calls = _fake_awsettings(monkeypatch, _Proc(0, "someone-else  -- other\n"))
    assert ccd.run_setup(tmp_path, dry_run=False, use_cli=False) == 0
    assert "does not know the preset" in capsys.readouterr().out
    assert not _applied(calls)


def test_setup_applies_preset_when_supported(tmp_path, monkeypatch, capsys):
    calls = _fake_awsettings(monkeypatch, _Proc(0, f"{ccd.AWSETTINGS_PRESET}  -- baseline\n"))
    assert ccd.run_setup(tmp_path, dry_run=False, use_cli=False) == 0
    assert ["awsettings", "preset", "apply", ccd.AWSETTINGS_PRESET] in calls
    assert f"ran: awsettings preset apply {ccd.AWSETTINGS_PRESET}" in capsys.readouterr().out


# --- setup: atomic write + timestamped backups --------------------------------------


def test_setup_write_keeps_a_timestamped_backup_per_run(tmp_path, monkeypatch):
    monkeypatch.setattr(ccd.shutil, "which", lambda name: None)
    path = tmp_path / ".claude" / "settings.json"
    _write(path, {"theme": "dark"})
    assert ccd.run_setup(tmp_path, dry_run=False, use_cli=False) == 0
    assert ccd.run_setup(tmp_path, dry_run=False, use_cli=False) == 0
    backups = sorted(path.parent.glob("settings.json.bak-adk-setup-*"))
    assert len(backups) == 2, backups  # the second run did not overwrite the first
    assert json.loads(backups[0].read_text(encoding="utf-8")) == {"theme": "dark"}
    assert not list(path.parent.glob(".settings.json.*.tmp"))


def test_atomic_write_leaves_old_file_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text('{"keep": 1}', encoding="utf-8")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(ccd.os, "replace", boom)
    raised = False
    try:
        ccd.atomic_write_json(path, {"new": 2})
    except OSError:
        raised = True
    assert raised
    assert json.loads(path.read_text(encoding="utf-8")) == {"keep": 1}
    assert not list(tmp_path.glob(".settings.json.*.tmp"))


# --- CCD004: timing never runs a hook with the real HOME or live sync hooks ---------


def test_ccd004_runs_hooks_sandboxed(tmp_path):
    import sys

    out = tmp_path / "seen.json"
    script = tmp_path / "hook.py"
    keys = ("HOME", "USERPROFILE", "AWSETTINGS_HOOKS_DISABLED", "AWRELAY_HOOKS_DISABLED")
    script.write_text(
        "import json, os, sys\n"
        f"keys = {keys!r}\n"
        f"open({str(out)!r}, 'w').write(json.dumps({{k: os.environ.get(k, '') for k in keys}}))\n",
        encoding="utf-8",
    )
    cmd = f'"{sys.executable}" "{script}"'.replace("\\", "/")
    ms, why = ccd.time_hook(cmd, "PreToolUse", tmp_path, runs=1)
    assert ms is not None, why
    seen = json.loads(out.read_text(encoding="utf-8"))
    real_home = str(Path.home())
    assert seen["HOME"] and seen["HOME"] != real_home
    assert seen["USERPROFILE"] and seen["USERPROFILE"] != real_home
    assert seen["AWSETTINGS_HOOKS_DISABLED"] == "1"
    assert seen["AWRELAY_HOOKS_DISABLED"] == "1"
    assert not Path(seen["HOME"]).exists()  # the sandbox is thrown away


# --- CCD005: the key list is the judge, not a grep of the binary ---------------------


def test_dead_keys_reported_even_when_binary_mentions_them(tmp_path):
    home, proj = tmp_path / "h", tmp_path / "p"
    (proj / ".git").mkdir(parents=True)
    fake = tmp_path / "claude-bin"
    fake.write_bytes(b"...mcpServers:{}...apiKey:''...allowedTools:[]...modle:x...")
    _write(
        home / ".claude" / "settings.json",
        {"mcpServers": {}, "apiKey": "x", "allowedTools": [], "modle": "o", "model": "o"},
    )
    rep = ccd.run_doctor(
        home, proj, timing=False, managed=tmp_path / "none.json",
        binary=fake, probe_devices=False,
    )
    msgs = [f.message for f in rep.findings if f.code == "CCD005"]
    for k in ("mcpServers", "apiKey", "allowedTools", "modle"):
        assert any(f"'{k}'" in m for m in msgs), (k, msgs)
    assert not any("'model'" in m for m in msgs)
    assert any("dead key" in m and "'mcpServers'" in m for m in msgs)
    assert rep.exit_code == 1


def test_allow_key_allowlists_an_intentional_custom_key(tmp_path):
    home, proj = tmp_path / "h", tmp_path / "p"
    (proj / ".git").mkdir(parents=True)
    _write(home / ".claude" / "settings.json", {"myTeamKey": 1})
    kw = dict(timing=False, managed=tmp_path / "none.json", use_binary=False,
              probe_devices=False)
    assert "CCD005" in _codes(ccd.run_doctor(home, proj, **kw))
    rep = ccd.run_doctor(home, proj, allow_keys=["myTeamKey"], **kw)
    assert rep.exit_code == 0, rep.findings
    assert any("myTeamKey" in n for n in rep.notes)  # the allowlist is printed
