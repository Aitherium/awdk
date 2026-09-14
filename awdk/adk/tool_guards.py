"""Cross-cutting invariants applied to TOOL CALLS, not to agents' memories.

WHY THIS MODULE EXISTS

Most recurring defect classes in an agent codebase are rules an agent was supposed
to REMEMBER, with a linter added later to catch the forgetting: always route calls
through the scheduler, never disable TLS verification, always attach the service
credential, always scope a search by tenant, never block the event loop. Each is a
"remember to", and each eventually needed a checker because remembering does not
scale across sessions.

The sharpest example is a file lease. A lease gate that runs at COMMIT time can be
satisfied on every single commit and still be useless, because the tool that
separates your edits from a concurrent author needs a baseline captured at lease
time — and by commit time the baseline already contains everyone's edits. The
discipline is followed; the outcome still fails.

The pattern that works:

    make the invariant a property of the EXECUTION PATH,
    not a step in the agent's plan.

An agent cannot forget a guard it never has to invoke.

WHY AT REGISTRATION, NOT AT DISPATCH

Tools are dispatched from more than one place, and the next consumer will be
another. Guarding a dispatcher guards one caller; wrapping the tool where it is
registered guards all of them, including callers not written yet.

SOFT BY CONSTRUCTION

This package runs on machines with no repository and none of the optional
integrations. A guard that raised there would break an agent to enforce something
that does not apply to it. So a guard that ERRORS is skipped and the tool proceeds;
only a guard that deliberately REFUSES stops the call. "I could not check" and
"this is forbidden" are different verdicts and must never be conflated.
"""

from __future__ import annotations

import functools
import logging
import os
import sys
from typing import Any, Callable, List, Tuple

logger = logging.getLogger(__name__)

#: (name, predicate, guard). `predicate(tool_name) -> bool` selects which tools a
#: guard applies to; `guard(tool_name, kwargs)` runs before the tool.
_GUARDS: List[Tuple[str, Callable[[str], bool], Callable[[str, dict], None]]] = []


class ToolRefusalError(Exception):
    """A guard deliberately REFUSED the call.

    Distinct from a guard that merely failed: this one is a verdict and is
    surfaced to the agent as the tool's error. Raise it only when proceeding
    would corrupt something — not when the guard could not do its job.
    """


def register_guard(name: str, predicate: Callable[[str], bool],
                   guard: Callable[[str, dict], None]) -> None:
    """Install a guard. Idempotent by name, so a re-import cannot double-install."""
    global _GUARDS
    _GUARDS = [g for g in _GUARDS if g[0] != name]
    _GUARDS.append((name, predicate, guard))


def registered_guards() -> List[str]:
    return [g[0] for g in _GUARDS]


def clear_guards() -> None:
    """Test seam only."""
    _GUARDS.clear()


def apply_guards(tool_name: str, fn: Callable) -> Callable:
    """Wrap `fn` so every matching guard runs before it.

    Returns `fn` unchanged when nothing matches, so an unguarded tool pays no
    call-time cost and keeps its identity for anything comparing functions.
    """
    matching = [g for g in _GUARDS if g[1](tool_name)]
    if not matching:
        return fn

    @functools.wraps(fn)
    def guarded(*args, **kwargs):
        for gname, _pred, guard in matching:
            try:
                guard(tool_name, kwargs)
            except ToolRefusalError as refusal:
                # A verdict. Surface it as the tool's own error string so the
                # agent sees WHY, in the channel it already reads.
                return f"REFUSED by {gname}: {refusal}"
            except Exception as exc:  # noqa: BLE001
                # The guard broke. That is OUR failure, not the caller's — never
                # block a customer's agent because a platform helper is absent.
                logger.debug("tool guard %s skipped for %s: %s", gname, tool_name, exc)
        return fn(*args, **kwargs)

    return guarded


def guard_registry(tools: list) -> list:
    """Apply guards to a LOCAL_TOOLS-shaped registry, in place, and return it."""
    for entry in tools:
        if isinstance(entry, dict) and callable(entry.get("fn")):
            entry["fn"] = apply_guards(entry.get("name", ""), entry["fn"])
    return tools


# ─────────────────────────────────────────────────────────────────────────────
# Guard: take the awgit lease BEFORE the write, not at commit time.
# ─────────────────────────────────────────────────────────────────────────────

_WARNED: set[str] = set()


def _warn_once(message: str) -> None:
    """Say a thing once per process.

    Once, not every call: a write-tool guard fires on every edit, and a warning that
    repeats a hundred times is scrolled past exactly like one that never prints.
    """
    if message in _WARNED:
        return
    _WARNED.add(message)
    print(f"[adk] WARNING: {message}", file=sys.stderr, flush=True)


WRITE_TOOLS = {"write_file", "edit_file", "patch_file", "apply_patch", "create_file"}


def _is_write_tool(tool_name: str) -> bool:
    return tool_name in WRITE_TOOLS


def _awgit_lease_guard(tool_name: str, kwargs: dict) -> None:
    """Acquire a lease on the target path, capturing the baseline stage-mine needs.

    ACQUIRES rather than refuses, deliberately. A guard that blocks to demand a
    manual command teaches agents to route around the tool; one that quietly does
    the right thing is invisible until it matters. The single case it DOES refuse
    is a real conflict — another actor holds the path — because that is when
    proceeding silently destroys someone's work.
    """
    path = kwargs.get("path") or kwargs.get("file_path")
    if not path:
        return
    if os.environ.get("AITHER_ADK_NO_LEASE"):
        return

    try:
        from awgit.leases import LeaseConflictError, LeaseRegistry, is_guarded
    except Exception:
        # awgit is a DECLARED dependency now, so absence means something is wrong with
        # the install rather than "this is a customer machine". Still degrade rather
        # than refuse — a guard that blocks the agent teaches it to route around the
        # tool — but SAY SO, once, because the silent version is the dangerous one:
        # with no lease registry this guard becomes a no-op and concurrent edits get
        # destroyed with no error, no log line, and nothing to notice.
        _warn_once(
            "awgit is not importable, so the write-lease guard is INERT. Concurrent "
            "edits to the same file will overwrite each other silently. "
            "Fix with: pip install awgit"
        )
        return

    from pathlib import Path

    repo = os.environ.get("VCS_REPO_ROOT") or os.getcwd()
    try:
        rel = Path(path).resolve().relative_to(Path(repo).resolve()).as_posix()
    except (ValueError, OSError):
        return  # outside the repo: not ours to guard
    if not is_guarded(rel):
        return

    actor = (os.environ.get("AITHER_ACTOR")
             or f"adk:{os.environ.get('AITHER_AGENT_NAME', 'agent')}")
    try:
        LeaseRegistry().acquire(actor, [rel], reason="auto: adk tool write")
    except LeaseConflictError as conflict:
        raise ToolRefusalError(
            f"{rel} is leased by another actor ({conflict}). Editing it anyway is how "
            f"a concurrent change gets swept or reverted. Coordinate or wait — do not "
            f"force past it."
        ) from conflict


register_guard("awgit-lease", _is_write_tool, _awgit_lease_guard)


def _self_test() -> int:
    ok = True

    def check(label: str, got: Any, want: Any) -> None:
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'PASS' if good else 'FAIL'}  {label} -> {got!r} (want {want!r})")

    saved = list(_GUARDS)
    try:
        clear_guards()

        calls = []
        register_guard("rec", lambda n: n == "t",
                       lambda n, kw: calls.append(kw.get("path")))
        wrapped = apply_guards("t", lambda path, content: f"wrote {path}")
        check("guard runs before the tool", wrapped(path="a.py", content="x"), "wrote a.py")
        check("guard saw the argument", calls, ["a.py"])

        # An unmatched tool is returned untouched — no wrapper, no cost.
        raw = lambda path: path  # noqa: E731
        check("unmatched tool is not wrapped", apply_guards("other", raw) is raw, True)

        # A BROKEN guard must not break the tool.
        clear_guards()
        def _boom(n, kw):
            raise RuntimeError("x")

        register_guard("boom", lambda n: True, _boom)
        check("broken guard is skipped, tool still runs",
              apply_guards("t", lambda: "ran")(), "ran")

        # A REFUSING guard must stop the tool and say why.
        clear_guards()

        def _refuse(n, kw):
            raise ToolRefusalError("held by someone else")

        register_guard("refuser", lambda n: True, _refuse)
        out = apply_guards("t", lambda: "ran")()
        check("refusal blocks the tool", out.startswith("REFUSED by refuser"), True)
        check("refusal explains why", "held by someone else" in out, True)

        # Registration is idempotent — a re-import cannot double-install.
        clear_guards()
        register_guard("dup", lambda n: True, lambda n, kw: None)
        register_guard("dup", lambda n: True, lambda n, kw: None)
        check("register_guard is idempotent", registered_guards(), ["dup"])

        # guard_registry rewires a LOCAL_TOOLS-shaped list.
        clear_guards()
        seen = []
        register_guard("r2", lambda n: n == "write_file", lambda n, kw: seen.append(n))
        reg = [{"name": "write_file", "fn": lambda path: "w"},
               {"name": "read_file", "fn": lambda path: "r"}]
        guard_registry(reg)
        reg[0]["fn"](path="x")
        reg[1]["fn"](path="x")
        check("only the matching tool is guarded", seen, ["write_file"])
    finally:
        clear_guards()
        for g in saved:
            _GUARDS.append(g)

    print("\nself-test:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
