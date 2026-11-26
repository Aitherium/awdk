"""Oneshot harness prompts must stay off argv (stdin or 0600 temp file).

Closes the local process-table snooping gap (`ps`, `/proc/<pid>/cmdline`):
- `codex` (`codex exec --json -`) and `gemini` (`-p ""`) read prompts from stdin.
- `aider` (`--message-file <0600>`) and `opencode` (`--file <0600>`) stage prompts
  in a private per-turn file that is unlinked immediately in `finally`.
- `AITHER_HARNESS_PROMPT_ON_ARGV=1` preserves a legacy opt-out for old binaries.
"""
from __future__ import annotations

import os
import stat
import sys
import time

from adk.harnesses import registry
from adk.harnesses.registry import HarnessSpec, LaunchSpec, Transport
from adk.harnesses.session import HarnessSession, SessionConfig


def _wait_turn_done(session: HarnessSession, timeout_s: float = 15.0) -> list[dict]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        events = session.events_since(0)
        if any(e["kind"] == "turn.completed" for e in events):
            return events
        time.sleep(0.05)
    raise AssertionError("oneshot turn did not complete within timeout")


def test_default_specs_keep_prompts_off_argv(monkeypatch):
    monkeypatch.delenv("AITHER_HARNESS_PROMPT_ON_ARGV", raising=False)
    secret = "SUPER_SECRET_TOKEN_XYZ_999"

    cx = registry.get("codex")
    assert cx.prompt_on_stdin is True
    cx_argv = cx.argv(LaunchSpec(prompt=secret))
    assert cx_argv == ["codex", "exec", "--json", "-"]
    assert secret not in " ".join(cx_argv)

    gm = registry.get("gemini")
    assert gm.prompt_on_stdin is True
    gm_argv = gm.argv(LaunchSpec(prompt=secret))
    assert "--skip-trust" in gm_argv and "-p" in gm_argv
    assert secret not in " ".join(gm_argv)

    ai = registry.get("aider")
    assert ai.prompt_via_file is True
    ai_argv = ai.argv(LaunchSpec(prompt="", prompt_file="/tmp/p.txt"))
    assert ai_argv == ["aider", "--no-pretty", "--yes", "--message-file", "/tmp/p.txt"]
    assert secret not in " ".join(ai_argv)

    oc = registry.get("opencode")
    assert oc.prompt_via_file is True
    oc_argv = oc.argv(LaunchSpec(prompt="", prompt_file="/tmp/p.txt"))
    assert "--file" in oc_argv and "/tmp/p.txt" in oc_argv
    assert secret not in " ".join(oc_argv)

    hm = registry.get("hermes")
    assert hm.prompt_on_stdin is True
    hm_argv = hm.argv(LaunchSpec(prompt=secret))
    assert hm_argv == ["hermes", "chat", "--query-file", "-"]
    assert secret not in " ".join(hm_argv)

    # OpenClaw upstream CLI (`openclaw agent --help`) has `--message`/`-m` only.
    cl = registry.get("openclaw")
    assert cl.prompt_on_stdin is False and cl.prompt_via_file is False


def test_legacy_env_opt_out_restores_argv(monkeypatch):
    monkeypatch.setenv("AITHER_HARNESS_PROMPT_ON_ARGV", "1")
    secret = "LEGACY_ARGV_PROMPT"
    assert registry.get("codex").argv(LaunchSpec(prompt=secret))[-1] == secret
    assert secret in registry.get("gemini").argv(LaunchSpec(prompt=secret))
    assert registry.get("aider").argv(LaunchSpec(prompt=secret, prompt_file="/tmp/x"))[-2:] == [
        "--message",
        secret,
    ]
    assert registry.get("opencode").argv(LaunchSpec(prompt=secret, prompt_file="/tmp/x"))[-1] == secret
    assert registry.get("hermes").argv(LaunchSpec(prompt=secret))[-2:] == ["-q", secret]


def test_oneshot_stdin_subprocess_never_sees_secret_in_argv(tmp_path, monkeypatch):
    monkeypatch.delenv("AITHER_HARNESS_PROMPT_ON_ARGV", raising=False)
    secret = "STDIN_SECRET_CANARY_42"
    script = (
        "import json, sys; "
        "data = sys.stdin.read(); "
        "print(json.dumps({'argv': sys.argv[1:], 'stdin': data}))"
    )
    spec = HarnessSpec(
        id="probe-stdin",
        label="Probe Stdin",
        description="verifies prompt rides stdin, never argv",
        transport=Transport.ONESHOT_PER_TURN,
        binary=sys.executable,
        adapter="text",
        prompt_carries_context=True,
        prompt_on_stdin=True,
        build_argv=lambda s, launch: [s.binary, "-c", script, "-"],
    )
    sess = HarnessSession(
        spec,
        SessionConfig(harness="probe-stdin", cwd=str(tmp_path), system_prompt_append="CTX_SECRET"),
        None,
        root=tmp_path,
    )
    sess.start()
    assert sess.send(secret) is True
    events = _wait_turn_done(sess)
    text = "".join(e["text"] for e in events if e["kind"] == "text.delta").strip()
    import json

    parsed = json.loads(text)
    assert secret not in " ".join(parsed["argv"])
    assert "CTX_SECRET" not in " ".join(parsed["argv"])
    assert secret in parsed["stdin"]
    assert "CTX_SECRET" in parsed["stdin"]


def test_oneshot_prompt_file_is_0600_and_unlinked_after_turn(tmp_path, monkeypatch):
    monkeypatch.delenv("AITHER_HARNESS_PROMPT_ON_ARGV", raising=False)
    secret = "FILE_SECRET_CANARY_77"
    script = (
        "import json, os, stat, sys; "
        "p = sys.argv[1]; "
        "mode = stat.S_IMODE(os.stat(p).st_mode); "
        "body = open(p, encoding='utf-8').read(); "
        "print(json.dumps({'path': p, 'mode': mode, 'body': body, 'argv': sys.argv[1:]}))"
    )
    spec = HarnessSpec(
        id="probe-file",
        label="Probe File",
        description="verifies prompt rides 0600 temp file and is cleaned up",
        transport=Transport.ONESHOT_PER_TURN,
        binary=sys.executable,
        adapter="text",
        prompt_carries_context=True,
        prompt_via_file=True,
        build_argv=lambda s, launch: [s.binary, "-c", script, launch.prompt_file],
    )
    sess = HarnessSession(
        spec,
        SessionConfig(harness="probe-file", cwd=str(tmp_path)),
        None,
        root=tmp_path,
    )
    sess.start()
    assert sess.send(secret) is True
    events = _wait_turn_done(sess)
    import json

    text = "".join(e["text"] for e in events if e["kind"] == "text.delta").strip()
    parsed = json.loads(text)
    assert secret not in " ".join(parsed["argv"])
    assert parsed["body"] == secret
    if os.name != "nt":
        assert parsed["mode"] == 0o600
    # Temp prompt file must be unlinked in finally once the turn finishes.
    assert not os.path.exists(parsed["path"])
    assert not list(sess.dir.glob(".prompt-turn-*"))


def test_oneshot_prompt_file_unlinked_even_when_spawn_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("AITHER_HARNESS_PROMPT_ON_ARGV", raising=False)
    fake_bin = tmp_path / "not-executable-bin"
    fake_bin.write_text("not an elf", encoding="utf-8")
    spec = HarnessSpec(
        id="probe-bad-spawn",
        label="Bad Spawn",
        description="fails Popen",
        transport=Transport.ONESHOT_PER_TURN,
        binary=str(fake_bin),
        adapter="text",
        prompt_via_file=True,
        build_argv=lambda s, launch: [s.binary, "--file", launch.prompt_file],
    )
    monkeypatch.setattr("adk.harnesses.session.resolve_binary", lambda s: str(fake_bin))
    sess = HarnessSession(spec, SessionConfig(harness="probe-bad-spawn", cwd=str(tmp_path)), None, root=tmp_path)
    sess.start()
    assert sess.send("secret") is True
    # Wait for background turn thread to finish cleanup.
    for t in sess._threads:
        t.join(timeout=5)
    assert not list(sess.dir.glob(".prompt-turn-*"))
