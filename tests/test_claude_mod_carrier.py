"""The awdk Claude Code plugin carrier: manifests, vendored skills, brick MCP wrapper, hooks."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

AWDK = Path(__file__).resolve().parent.parent
MOD = AWDK / "adk" / "harnesses" / "claude_mod"
GEN = AWDK / "scripts" / "sync_claude_mod.py"
HOOK = MOD / "hooks" / "portable_hook.py"
MISSING = "awnosuchbrick-zz"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


# ------------------------------------------------------------------- manifests
def test_plugin_manifest_shape():
    m = _json(MOD / ".claude-plugin" / "plugin.json")
    assert m["name"] == "awsh" and m["version"]
    for key in ("hooks", "mcpServers", "outputStyles"):
        assert (MOD / m[key]).exists(), key
    assert _json(MOD / "hooks" / "hooks.json")["modules"] == ["./register.ts"]


def test_public_marketplace_points_at_the_plugin():
    root = _json(AWDK / ".claude-plugin" / "marketplace.json")
    nested = _json(MOD / ".claude-plugin" / "marketplace.json")
    plugin = _json(MOD / ".claude-plugin" / "plugin.json")
    assert root["name"] == nested["name"] == "awsh"
    (entry,) = root["plugins"]
    assert entry["name"] == plugin["name"]
    assert (AWDK / entry["source"]).resolve() == MOD.resolve()
    assert entry["version"] == nested["plugins"][0]["version"] == plugin["version"]


def test_mcp_servers_all_go_through_the_tolerant_wrapper():
    servers = _json(MOD / ".mcp.json")["mcpServers"]
    assert servers
    for name, spec in servers.items():
        assert spec["args"] == ["-m", "adk.harnesses.brick_mcp", name], name


def test_portable_hooks_are_by_path_and_never_fail():
    hooks = _json(MOD / "hooks" / "portable.json")["hooks"]
    modes = set()
    for event in ("PostToolUse", "UserPromptSubmit", "Stop"):
        cmd = hooks[event][0]["hooks"][0]["command"]
        assert "${CLAUDE_PLUGIN_ROOT}/hooks/portable_hook.py" in cmd
        assert cmd.rstrip().endswith("|| true")
        modes.add(cmd.split("portable_hook.py\" ")[1].split()[0])
    assert modes == {"relay-inbox", "awm-recall", "awvoice-reply"}


def test_output_style_is_generic():
    text = (MOD / "output-styles" / "aither.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: Aither\n")
    for private in ("David", "wzns", "C:\\", "AitherOS-Fresh", "awgit", "Discord"):
        assert private not in text, private


# ------------------------------------------------------------------- generator
def _fake_repo(tmp: Path) -> Path:
    (tmp / "awskills" / "skills").mkdir(parents=True)
    (tmp / "awskills" / "skills" / "alpha.md").write_bytes(b"---\nname: alpha\n---\r\nA\r\n")
    (tmp / "awskills" / "skills" / "awfoo.md").write_text("---\nname: awfoo\n---\nold\n")
    (tmp / "awskills" / "skills" / "leaky.md").write_text("see ." + "DEPLOYMENT/x.yaml\n")
    # an internal checker rule id / ledger id would fail the wheel boundary gate (ADK001)
    # (assembled at runtime so this test file does not itself carry the shapes it fixtures)
    (tmp / "awskills" / "skills" / "ruleid.md").write_text("gated by " + "SEC" + "004\n")
    (tmp / "awskills" / "skills" / "ledger.md").write_text("tracked as " + "D-" + "2718\n")
    (tmp / "awskills" / "skills" / "decoy.md").write_text("SECTION 001, D-12, ASEC0010\n")
    (tmp / "awskills" / "skills" / "code-like-david.md").write_text("personal\n")
    brick = tmp / ".claude" / "skills" / "awfoo"
    (brick / "ref").mkdir(parents=True)
    (brick / "SKILL.md").write_text("---\nname: awfoo\n---\nbrick\n")
    (brick / "ref" / "notes.md").write_text("n\n")
    (tmp / ".claude" / "skills" / "not-a-brick").mkdir()
    (tmp / ".claude" / "skills" / "not-a-brick" / "SKILL.md").write_text("x\n")
    (tmp / "AitherOS" / "packages" / "awfoo").mkdir(parents=True)
    return tmp


def _tree(d: Path) -> dict[str, bytes]:
    files = sorted(d.rglob("*"))
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in files if p.is_file()}


def test_generator_is_idempotent_and_owns_the_tree(tmp_path):
    gen = _load(GEN, "sync_claude_mod")
    root = _fake_repo(tmp_path)
    dest = root / gen.PLUGIN_REL / "skills"
    (dest / "stale").mkdir(parents=True)
    (dest / "stale" / "SKILL.md").write_text("gone\n")
    assert gen.main(["--root", str(root), "--check"]) == 1
    assert gen.main(["--root", str(root)]) == 0
    first = _tree(dest)
    assert gen.main(["--root", str(root)]) == 0
    assert _tree(dest) == first
    assert gen.main(["--root", str(root), "--check"]) == 0
    assert set(first) == {
        "README.md", "alpha/SKILL.md", "awfoo/SKILL.md", "awfoo/ref/notes.md", "decoy/SKILL.md",
    }
    assert first["alpha/SKILL.md"] == b"---\nname: alpha\n---\nA\n"
    assert b"brick" in first["awfoo/SKILL.md"]
    (dest / "alpha" / "SKILL.md").write_text("hand edit\n")
    assert gen.main(["--root", str(root), "--check"]) == 1


def test_generator_cannot_run_without_sources(tmp_path):
    gen = _load(GEN, "sync_claude_mod")
    assert gen.main(["--root", str(tmp_path), "--check"]) == 2


@pytest.mark.skipif(
    not (AWDK.parent / "awskills" / "skills").is_dir(),
    reason="public mirror: the skill sources live in the monorepo",
)
def test_vendored_skills_match_the_sources():
    r = subprocess.run([sys.executable, str(GEN), "--check"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout + r.stderr


# --------------------------------------------------------- wrapper and hooks
def test_brick_mcp_missing_brick_exits_zero_with_one_stderr_line():
    r = subprocess.run([sys.executable, "-m", "adk.harnesses.brick_mcp", MISSING],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=AWDK)
    assert r.returncode == 0
    assert r.stdout == ""
    assert len(r.stderr.strip().splitlines()) == 1 and MISSING in r.stderr


def test_brick_mcp_runs_the_brick_mcp_subcommand(monkeypatch):
    from adk.harnesses import brick_mcp

    calls = []
    monkeypatch.setattr(brick_mcp.shutil, "which", lambda n: f"/bin/{n}")
    monkeypatch.setattr(brick_mcp.os, "name", "nt")
    monkeypatch.setattr(brick_mcp.subprocess, "call", lambda cmd: calls.append(cmd) or 7)
    assert brick_mcp.main(["awfind", "--x"]) == 7
    assert calls == [["/bin/awfind", "mcp", "--x"]]
    assert brick_mcp.resolve("../evil") is None


@pytest.mark.parametrize("mode", ["relay-inbox", "awm-recall", "awvoice-reply", "unknown"])
def test_portable_hooks_are_silent_when_bricks_are_missing(mode, monkeypatch, capsys):
    hook = _load(HOOK, "portable_hook")
    monkeypatch.setattr(hook.shutil, "which", lambda n: None)
    monkeypatch.setattr(hook, "hookgate_path", lambda: None)
    monkeypatch.setenv("AWM_SCOPE", "a:b:c")
    rc = hook.main([mode], stdin_text=json.dumps({"prompt": "hi", "session_id": "s"}))
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_awm_recall_hook_passes_the_prompt_as_the_query(monkeypatch, capsys):
    hook = _load(HOOK, "portable_hook")
    seen = []
    monkeypatch.setattr(hook.shutil, "which", lambda n: "/bin/awm")
    monkeypatch.setattr(hook, "_run", lambda cmd, stdin, timeout=0: seen.append(cmd) or "fact\n")
    monkeypatch.setenv("AWM_SCOPE", "a:b:c")
    assert hook.main(["awm-recall"], stdin_text=json.dumps({"prompt": "why  is\nX"})) == 0
    assert seen[0][:4] == ["/bin/awm", "recall", "--scope", "a:b:c"]
    assert seen[0][5] == "why is X"
    assert capsys.readouterr().out == "fact\n"
