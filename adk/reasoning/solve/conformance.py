"""Conformance checker for :class:`adk.reasoning.solve.Environment`.

``isinstance(env, Environment)`` only proves the four method NAMES exist
(``runtime_checkable`` checks nothing else). ``check_environment`` goes further:

* each required member is callable, and ``act`` accepts ``(action, source=...)``;
* every optional hook that is present is callable with the arguments the loop
  passes it (``_types.HOOK_ARGS``: ``tools(loop)``, ``render(obs, last)`` ...);
* with ``probe=True`` it calls the read-only methods (``observe``,
  ``available_actions``, ``done``) and checks the shapes they return.

``act`` is never called: it mutates the world, and a checker that plays a move
would charge an action to a scored run.
"""

from __future__ import annotations

import inspect
from typing import Any, List

from ._types import HOOK_ARGS, OPTIONAL_HOOKS, Environment

REQUIRED = ("observe", "act", "available_actions", "done")
OBS_FIELDS = ("state", "level", "level_up", "died", "done", "info")

__all__ = ["REQUIRED", "OBS_FIELDS", "check_environment", "assert_environment", "check_obs"]


def check_obs(obs: Any, where: str = "observe()") -> List[str]:
    """Problems with one observation (duck-typed: any object with the Obs fields)."""
    problems: List[str] = []
    missing = [f for f in OBS_FIELDS if not hasattr(obs, f)]
    if missing:
        return ["%s returned %s without field(s) %s" % (where, type(obs).__name__, missing)]
    if not isinstance(obs.level, int) or isinstance(obs.level, bool):
        problems.append("%s.level is %s, not int" % (where, type(obs.level).__name__))
    for flag in ("level_up", "died", "done"):
        if not isinstance(getattr(obs, flag), bool):
            problems.append("%s.%s is not a bool" % (where, flag))
    if not isinstance(obs.info, dict):
        problems.append("%s.info is not a dict" % where)
    return problems


def _accepts_source(fn: Any) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True  # builtins / C callables: cannot judge, do not fail on it
    params = sig.parameters
    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        return True
    src = params.get("source")
    return src is not None and src.kind is not inspect.Parameter.POSITIONAL_ONLY


def _accepts_positional(fn: Any, n: int) -> bool:
    """``fn(*[None] * n)`` would bind (bound methods already exclude ``self``)."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True  # builtins / C callables: cannot judge, do not fail on it
    try:
        sig.bind(*([None] * n))
    except TypeError:
        return False
    return True


def check_environment(env: Any, *, probe: bool = False) -> List[str]:
    """Return a list of conformance problems; an empty list means conformant."""
    problems: List[str] = []
    for name in REQUIRED:
        member = getattr(env, name, None)
        if member is None:
            problems.append("missing required method %s()" % name)
        elif not callable(member):
            problems.append("%s is not callable" % name)
    if problems:
        return problems
    if not isinstance(env, Environment):
        problems.append("isinstance(env, Environment) is False")
    if not _accepts_source(env.act):
        problems.append("act() must accept act(action, source='model')")
    for hook in OPTIONAL_HOOKS:
        member = getattr(env, hook, None)
        if member is None:
            continue
        if not callable(member):
            problems.append("optional hook %s is present but not callable" % hook)
        elif not _accepts_positional(member, len(HOOK_ARGS[hook])):
            problems.append(
                "optional hook %s must accept %s(%s): the loop passes %d positional arg(s)"
                % (hook, hook, ", ".join(HOOK_ARGS[hook]), len(HOOK_ARGS[hook]))
            )
    if not probe:
        return problems

    try:
        obs = env.observe()
    except Exception as exc:  # noqa: BLE001 - a raising observe() is a finding
        problems.append("observe() raised %s: %s" % (type(exc).__name__, exc))
    else:
        problems.extend(check_obs(obs))
    try:
        acts = env.available_actions()
    except Exception as exc:  # noqa: BLE001
        problems.append("available_actions() raised %s: %s" % (type(exc).__name__, exc))
    else:
        if not isinstance(acts, list) or not all(
            isinstance(a, int) and not isinstance(a, bool) for a in acts
        ):
            problems.append("available_actions() must return list[int], got %r" % (acts,))
    try:
        d = env.done()
    except Exception as exc:  # noqa: BLE001
        problems.append("done() raised %s: %s" % (type(exc).__name__, exc))
    else:
        if not isinstance(d, bool):
            problems.append("done() must return bool, got %s" % type(d).__name__)
    return problems


def assert_environment(env: Any, *, probe: bool = False) -> None:
    """Raise ``TypeError`` naming every problem when ``env`` does not conform."""
    problems = check_environment(env, probe=probe)
    if problems:
        raise TypeError(
            "%s is not a conformant Environment:\n  - %s"
            % (type(env).__name__, "\n  - ".join(problems))
        )
