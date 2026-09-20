"""End-to-end smoke for the Claude Code mod. Spends real model tokens; run by hand.

Two headless Claude Code runs against the plugin directory, no install needed:

* **control** -- function hooks OFF. The ``aw`` agent must refuse and no answer may
  carry the awsh signature. This is the half that proves the other half can fail:
  if a Claude model could produce the signature by itself, a green run means nothing.
* **live** -- hooks ON. The answer must carry ``— answered by awsh, <harness>/``.

Exit 0 both held · 1 one did not · 2 could not judge (no claude, no daemon).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

from adk.harnesses import mod
from adk.harnesses.session import scrub_nested_claude_markers

SIGNATURE = "— answered by awsh, "
REFUSAL = "awsh for Claude Code is not active"
TOKEN = "AW-SMOKE-7319"


def _run(harness: str, hooks: bool, timeout: float) -> tuple[int, str]:
    binary = shutil.which("claude")
    if binary is None:
        return 127, "claude is not on PATH"
    env = scrub_nested_claude_markers(dict(os.environ))
    if hooks:
        env[mod.HOOKS_ENV] = "1"
    # ONE line, on purpose. On Windows `claude` resolves to a .cmd shim, and cmd.exe
    # ends an argument at a newline: the model received half a prompt and asked
    # what the task was, which read as the mod failing.
    prompt = (
        f'Use the Agent tool with subagent_type "{mod.PLUGIN_NAME}:aw". Its prompt must '
        f'be exactly two lines: the first line is "aw-harness: {harness}" and the second '
        f'line is "Reply with exactly: {TOKEN}". '
        "Then report its answer verbatim, including any signature line."
    )
    with tempfile.TemporaryDirectory(prefix="aw-smoke-") as cwd:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [binary, "--plugin-dir", str(mod.mod_dir()), "--model", "haiku",
             "--output-format", "stream-json", "--verbose", "-p", prompt],
            cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    return done.returncode, _stream_text(done.stdout) + os.linesep + done.stderr.strip()


def _stream_text(stdout: str) -> str:
    """Every event of the run, decoded. Judged whole, never by the parent's last word.

    The parent is a small model asked to relay an answer, and it paraphrases: it
    reported the bare token for a subagent that had in fact refused. What the
    SUBAGENT said is in the stream either way.
    """
    lines = []
    for raw in stdout.splitlines():
        try:
            lines.append(json.dumps(json.loads(raw), ensure_ascii=False))
        except ValueError:
            lines.append(raw)
    return os.linesep.join(lines)


def smoke(harness: str = "opencode", timeout: float = 300.0) -> int:
    if not mod.mod_present():
        print(f"COULD NOT JUDGE: plugin files missing from {mod.mod_dir()}")
        return 2
    verdict = 0
    for name, hooks in (("control", False), ("live", True)):
        try:
            code, out = _run(harness, hooks, timeout)
        except subprocess.TimeoutExpired:
            print(f"{name}: COULD NOT JUDGE: no answer in {timeout:.0f}s")
            return 2
        if code == 127:
            print(f"{name}: COULD NOT JUDGE: {out}")
            return 2
        signed = SIGNATURE + harness + "/" in out
        if hooks:
            held = signed and TOKEN in out
        else:
            held = not signed and REFUSAL in out
        print(f"{name}: {'ok' if held else 'FAILED'}  (exit {code}, signed={signed})")
        if not held:
            print(out[-1200:])
            verdict = 1
    return verdict


if __name__ == "__main__":
    sys.exit(smoke(*(sys.argv[1:2] or ["opencode"])))
