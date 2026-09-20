"""awsh mod: compact a long tool result before it reaches the model.

The same job as `.claude/hooks/tool-output-compact.ps1` and the Claude Code
function-hooks mod at `awdk/adk/mods/compact/`, on awsh's own side: when a tool
call comes back with 1,000 lines of pytest, the model should see the handful
that decide what happens next, chosen per LINE SHAPE by the decision door.

    from adk.shell.mods.compact_tool_output import register, on_tool_result
    register(on)                       # a registry that speaks on(event, hook)
    text = on_tool_result("pytest", raw)   # or call it directly

🚨 **WIRING, stated plainly because a mod nobody calls reads as shipped.**
awsh's Python REPL streams `tool_call` / `tool_result` events
(`adk/shell/repl.py`, the `elif event_type == "tool_result":` branch) and has
NO hook registry for them today -- there is no `mods/` loader in `adk/shell`,
this is the first file in it. So `register()` binds to any registry it is
handed (the shape the Claude Code engine uses: `on(event, hook)`), and
`on_tool_result` is callable by hand right now. The one line that would make it
automatic is in `repl.py`'s `tool_result` branch, handing `data.get("output")`
through `on_tool_result(data.get("tool"), ...)` before it is printed; that file
belongs to the REPL, not to this mod, so the line is named here rather than
written. Until it exists, the LIVE surfaces are the `/compact` slash command,
`python -m adk.compact`, the MCP tools and the Claude Code mod.

Everything here delegates to `adk.compact`, which delegates to the world-model
tree's `compact.py`. One implementation, three front doors: three copies of a
shape grammar keyed on ONE fork name would each teach the door a different
descriptor for the same line, and the engine would answer confidently from a
table built for a different question.

`python -m adk.shell.mods.compact_tool_output --self-test` proves it can fail.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable, Dict, Optional

#: Below this many lines a result is left alone -- the compactor costs more
#: than it saves, and a short output has no filler to lose.
MIN_LINES = int(os.environ.get("AITHER_COMPACT_MIN_LINES", "60") or 60)

#: The events this mod binds to when handed a registry. `tool.call` is the
#: Claude Code engine's name, `tool_result` is awsh's own stream event.
EVENTS = ("tool.call", "tool_result")

#: Commands whose fork the output learns on. `pytest` filler looks nothing like
#: build filler, so they must not share one table.
_FORKS = (
    "pytest", "ruff", "mypy", "terraform", "ansible", "podman", "docker",
    "cargo", "yarn", "npm", "curl", "make", "pip", "git", "go",
)


def fork_of(command: str) -> str:
    """Which `decide.compact.<fork>` this output learns on."""
    import re

    low = str(command or "").lower()
    for name in _FORKS:
        if re.search(rf"(^|[\s;&|/\\\"']){name}(\s|$)", low):
            return name
    return "bash"


def text_of(payload: Any) -> str:
    """The result's text, whichever shape the event carries. '' = leave alone."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("output", "stdout", "result", "content", "text"):
            val = payload.get(key)
            if isinstance(val, str) and val:
                extra = payload.get("stderr")
                return f"{val}\n{extra}" if isinstance(extra, str) and extra else val
        err = payload.get("stderr")
        if isinstance(err, str) and err:
            return err
    return ""


def on_tool_result(
    tool: str,
    payload: Any,
    *,
    min_lines: int = MIN_LINES,
    header: bool = True,
    url: Optional[str] = None,
    mode: str = "auto",
) -> str:
    """The mod's whole job: raw result in, what the model should read out.

    Returns the ORIGINAL text on every failure path -- a door that is away, a
    result too short to bother with, a compactor that raised. A mod that eats a
    tool result when something is missing is worse than no mod at all."""
    text = text_of(payload)
    if not text or text.count("\n") + 1 < min_lines:
        return text
    try:
        from adk import compact as api

        r = api.compact(text, fork_of(tool), url=url, mode=mode, min_lines=min_lines)
    except Exception as exc:  # noqa: BLE001 -- never raise into a tool stream
        sys.stderr.write(f"[compact-mod] left the output alone: {str(exc)[:160]}\n")
        return text
    if r.kept_lines >= r.total_lines:
        return text
    return f"{r.header()}\n{r.kept}" if header else r.kept


def register(on: Callable[..., Any], options: Optional[Dict[str, Any]] = None) -> int:
    """Bind to a registry that speaks `on(event, hook)`. Returns how many
    bindings were made, so a caller can tell 'registered' from 'silently not'."""
    options = options or {}
    min_lines = int(options.get("min_lines", MIN_LINES))
    mode = str(options.get("mode", "auto"))
    url = options.get("url")
    bound = 0

    def hook(event: Any = None, data: Any = None, **_: Any) -> Any:
        payload = data if data is not None else event
        tool = ""
        if isinstance(payload, dict):
            tool = str(payload.get("tool") or payload.get("name") or "")
            command = ""
            for key in ("command", "cmd", "input", "arguments"):
                val = payload.get(key)
                if isinstance(val, str):
                    command = val
                    break
                if isinstance(val, dict) and isinstance(val.get("command"), str):
                    command = val["command"]
                    break
            if command:
                tool = command
        return on_tool_result(tool, payload, min_lines=min_lines, mode=mode, url=url)

    for event in EVENTS:
        try:
            on(event, hook)
            bound += 1
        except Exception as exc:  # noqa: BLE001 -- a registry without this event
            sys.stderr.write(f"[compact-mod] could not bind {event}: {str(exc)[:120]}\n")
    return bound


# ------------------------------------------------------------------ self-test
def _self_test() -> int:
    fails = []

    def check(name: str, ok: bool) -> None:
        print(("ok   " if ok else "FAIL ") + name)
        if not ok:
            fails.append(name)

    check("pytest command picks the pytest fork", fork_of("python -m pytest -q") == "pytest")
    check("podman build picks the podman fork", fork_of("wsl podman build .") == "podman")
    check("an unknown command falls back to bash", fork_of("sort -u x") == "bash")
    check("no command is still a fork", fork_of("") == "bash")

    check("string payload", text_of("a\nb") == "a\nb")
    check("dict payload joins stdout and stderr",
          text_of({"stdout": "a", "stderr": "b"}) == "a\nb")
    check("stderr-only payload", text_of({"stderr": "boom"}) == "boom")
    check("unknown payload is empty, not a crash", text_of({"nope": 1}) == "")
    check("None payload is empty", text_of(None) == "")

    short = "one\ntwo"
    check("short output is returned untouched", on_tool_result("pytest", short) == short)

    long_out = "\n".join(
        ["==== test session starts ====", "collected 200 items", ""]
        + [f"tests/t.py::test_{i} PASSED    [{i // 2:3d}%]" for i in range(200)]
        + ["tests/t.py::test_boom FAILED   [100%]", "E   assert 1 == 2",
           "FAILED tests/t.py::test_boom - assert 1 == 2",
           "==== 1 failed, 200 passed in 3.2s ===="]
    )
    out = on_tool_result("python -m pytest -q", {"stdout": long_out}, mode="rules")
    check("a long output is compacted", out.count("\n") < long_out.count("\n"))
    check("the failure line survives", "test_boom" in out)
    check("the summary line survives", "1 failed, 200 passed" in out)
    check("the header says what was kept", out.startswith("[compacted by the decision door:"))
    check("header=False omits it",
          not on_tool_result("pytest", {"stdout": long_out}, mode="rules",
                             header=False).startswith("["))

    # a compactor that raises must return the ORIGINAL text
    import adk.compact as api

    real = api.compact
    try:
        api.compact = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("door gone"))
        kept = on_tool_result("pytest", {"stdout": long_out})
        check("a raising compactor returns the original text", kept == long_out)
    finally:
        api.compact = real

    # register binds what it can and reports the count honestly
    seen = []
    check("register binds every event a registry accepts",
          register(lambda ev, hk: seen.append(ev)) == len(EVENTS) == len(seen))

    def only_one(ev, hk):
        if ev != "tool.call":
            raise KeyError(ev)

    check("register reports a partial binding rather than claiming success",
          register(only_one) == 1)

    print(f"\n{'PASS' if not fails else 'FAIL'}: {len(fails)} failing check(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
    sys.exit(0)
