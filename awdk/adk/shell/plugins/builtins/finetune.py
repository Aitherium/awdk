"""Fine-tune a model without making it worse — AitherShell plugin.

    /finetune plan [base] [engine]     what does the base ALREADY score?
    /finetune gate                     would this corpus damage it? (free)
    /finetune run <tag> [engine]       anchors -> corpus -> gate -> train -> bench
    /finetune soup <config.yaml>       soup-cli, with the corpus gate in front
    /finetune snapshot <dir> <store> <label>
    /finetune restore  <store> <label> <dest>
    /finetune seal     <dir> [subject]
    /finetune publish  <dir> <out> [name]

Aliases: /ft, /train

WHY THE ORDER OF THESE COMMANDS IS THE POINT

The expensive mistake in fine-tuning is not a crashed run — it is a run that
SUCCEEDS and produces a worse model. Measured across seven fine-tunes of one
orchestrator, every one lost to its own base (0.8540), and the two failure modes
score IDENTICALLY: a model that collapsed into a single register benches like
one that is merely weak. Nothing downstream can tell them apart, so the checks
have to happen before the money is spent.

`gate` is the one people skip and the one that pays. Damage is predictable from
the corpus alone: TCF009 named three of the four dimensions a previous run
destroyed, from the corpus, before a box was rented.

🚨 THIS FILE'S OWN SHAPE IS A FINDING. It was first written as a class with a
`register_commands(cli_group)` static method, copying `decisions.py`. Measured:
the shell's plugin loader registers `SlashCommand` SUBCLASSES only
(`plugins/__init__.py::_load_python_plugin`), so `register_commands` is consumed
by nothing — `cli.py::_register_commands` is an unrelated function taking a
subparser. Two builtins still use that dead shape and 36 use this one. A plugin
in the wrong shape does not error; it simply never appears, which is the same
silence this plugin's own doctrine is about — and finding it that way is why
`check_shell_plugins_register.py` now exists.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand

#: Resolved from THIS file, never from the working directory. A repo-relative
#: path under a changed cwd made every bench subprocess die instantly on
#: "can't open file" — the tool was correct and simply unreachable.
_ADK_ROOT = Path(__file__).resolve().parents[4]
_REPO = _ADK_ROOT.parent
_TOOLS = _REPO / "AitherOS" / "dev" / "tools"


def _tool(name: str) -> Optional[List[str]]:
    path = _TOOLS / name
    if not path.is_file():
        return None
    return [sys.executable, str(path)]


async def _capture(cmd: List[str]) -> str:
    """Run and report the REAL outcome, without parking the event loop.

    ASYNC on purpose. The obvious `subprocess.run` here is a blocking call
    inside an `async def`, so it runs ON the loop and stalls every other
    concurrent request for its whole duration — and the durations in this
    plugin are training runs, i.e. hours. `create_subprocess_exec` is the
    native fix and needs no monorepo import, which matters because this package
    ships to PyPI and cannot reach `lib.core.EventLoopMonitor`.

    No `timeout` wrapper: a SIGTERM at ten minutes reads as a training failure,
    and that has killed a two-hour run here. No success-word filter either — a
    filter that only knows success turns every failure into silence, so a
    non-zero exit prints rc, stdout AND stderr.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await proc.communicate()
    except OSError as exc:
        return f"could not run {cmd[0]}: {exc}"
    out = (out_b or b"").decode("utf-8", errors="replace")
    err = (err_b or b"").decode("utf-8", errors="replace")
    if proc.returncode == 0:
        return out.strip() or "ok"
    return (f"exit {proc.returncode}"
            + "\n--- stdout ---\n" + out.strip()[-2000:]
            + "\n--- stderr ---\n" + err.strip()[-2000:])


async def _aw(binary: str, args: List[str]) -> str:
    exe = shutil.which(binary)
    if not exe:
        return (f"`{binary}` is not on PATH (pip install {binary}). Refusing "
                f"rather than skipping the step: an unsealed adapter that "
                f"reports success is indistinguishable from a sealed one until "
                f"someone tries to verify it.")
    return await _capture([exe, *args])


class FinetunePlugin(SlashCommand):
    """Train a model, and know whether it actually got better."""

    name = "finetune"
    description = "Fine-tune a model safely: plan, gate, run, seal, publish"
    aliases = ["ft", "train"]

    def __init__(self) -> None:
        super().__init__(
            name="finetune",
            description="Fine-tune a model safely: plan, gate, run, seal, publish",
            aliases=["ft", "train"],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        sub = args[0].lower() if args else "help"
        rest = args[1:]

        if sub in ("help", "-h", "--help"):
            return __doc__

        if sub == "plan":
            base = rest[0] if rest else "Nemotron-Orchestrator-8B"
            engine = rest[1] if len(rest) > 1 else "native"
            cmd = _tool("finetune_run.py")
            if not cmd:
                return self._missing("finetune_run.py")
            # --dry-run runs on a laptop with no cloud credential on purpose: a
            # dry run exists to check the recipe BEFORE anything is spent, and
            # requiring a live training host to perform one inverts that.
            return await _capture([*cmd, "--base", base, "--engine", engine,
                             "--tag", "plan", "--dry-run"])

        if sub == "gate":
            cmd = _tool("check_training_corpus_fitness.py")
            if not cmd:
                return self._missing("check_training_corpus_fitness.py")
            return await _capture([*cmd, "--all"])

        if sub == "run":
            if not rest:
                return "usage: /finetune run <tag> [engine] [base]"
            tag = rest[0]
            engine = rest[1] if len(rest) > 1 else "native"
            base = rest[2] if len(rest) > 2 else "Nemotron-Orchestrator-8B"
            cmd = _tool("finetune_run.py")
            if not cmd:
                return self._missing("finetune_run.py")
            return await _capture([*cmd, "--tag", tag, "--engine", engine,
                             "--base", base])

        if sub == "soup":
            if not rest:
                return "usage: /finetune soup <config.yaml>"
            cmd = _tool("aither_soup.py")
            if not cmd:
                return self._missing("aither_soup.py")
            # Never bare `soup train`: that is a complete, working command that
            # runs no gate at all.
            return await _capture([*cmd, "train", "--config", rest[0]])

        if sub == "snapshot":
            if len(rest) < 3:
                return "usage: /finetune snapshot <dir> <store> <label>"
            return await _aw("awrecover", ["snapshot", rest[0], "--store", rest[1],
                                     "--label", rest[2]])

        if sub == "restore":
            if len(rest) < 3:
                return "usage: /finetune restore <store> <label> <dest>"
            return await _aw("awrecover", ["restore", "--store", rest[0],
                                     "--label", rest[1], "--dest", rest[2]])

        if sub == "seal":
            if not rest:
                return "usage: /finetune seal <dir> [subject]"
            a = ["sign", rest[0]]
            if len(rest) > 1:
                a += ["--subject", rest[1]]
            return await _aw("awseal", a)

        if sub == "publish":
            if len(rest) < 2:
                return "usage: /finetune publish <dir> <out> [name]"
            a = ["publish", rest[0], "--out", rest[1], "--seal"]
            if len(rest) > 2:
                a += ["--name", rest[2]]
            return await _aw("awshare", a)

        return f"unknown subcommand {sub!r}\n\n{__doc__}"

    @staticmethod
    def _missing(tool: str) -> str:
        return (f"{tool} not found under {_TOOLS}. Refusing to report success "
                f"for a step that never ran — that is how a pipeline produces "
                f"an adapter nobody gated.")
