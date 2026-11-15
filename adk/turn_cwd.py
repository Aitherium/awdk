"""The working directory of the turn that is running, when its caller named one.

A session spawned through awsh carries a ``cwd``. The awdk harness runs its turns
inside the kit server, whose process cwd is wherever ``adk run`` was started, so
that cwd was accepted and then ignored (measured 2026-11-12). The server binds it
here for the length of the turn; the built-in shell, file, git and search tools
resolve relative paths against it. It widens nothing: the file tools' allowed
roots still decide what may be read or written.
"""
from __future__ import annotations

import contextvars
import functools
import inspect
import os

TURN_CWD: contextvars.ContextVar[str] = contextvars.ContextVar("adk_turn_cwd", default="")


def current() -> str:
    """The turn's cwd, or the process cwd when no caller named one."""
    return TURN_CWD.get() or os.getcwd()


def usable(cwd: object) -> str:
    """``cwd`` as an absolute path when it names an existing directory, else ""."""
    if not isinstance(cwd, str) or not cwd.strip():
        return ""
    path = os.path.abspath(os.path.expanduser(cwd.strip()))
    return path if os.path.isdir(path) else ""


def resolve(path: str, cwd: str = "") -> str:
    base = cwd or TURN_CWD.get()
    if not base or not isinstance(path, str) or not path:
        return path
    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded):
        return path
    return os.path.normpath(os.path.join(base, expanded))


def in_turn_cwd(fn):
    """Resolve a tool's ``path`` argument against the turn's cwd. A no-op (the
    original call, untouched) when no turn cwd is bound."""
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        base = TURN_CWD.get()
        if not base or "path" not in signature.parameters:
            return fn(*args, **kwargs)
        try:
            bound = signature.bind_partial(*args, **kwargs)
        except TypeError:
            return fn(*args, **kwargs)
        bound.apply_defaults()
        bound.arguments["path"] = resolve(bound.arguments.get("path") or ".", base)
        return fn(*bound.args, **bound.kwargs)

    return wrapper
