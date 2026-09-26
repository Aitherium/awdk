# vendored from h30-repl-agent@f27271775d6786b1df5dd40234005436af8081c8:agent/repl/core/sandbox.py -- edit only by re-vendoring (see adk/reasoning/solve/_provenance.py)
"""h30 sandbox: run model-written Python with a time cap and a bounded print.

This is a GUARD, not a security boundary: the code comes from our own local
model, and the goal is that a bad turn (an infinite loop, a huge print, a
swallowed exception) costs one turn and never the game.

Mechanics
---------
* Every piece of model code is compiled with a filename that starts with
  ``<h30`` (turn code, skills, hypotheses).  A thread-local ``sys.settrace``
  tracer only traces those frames and raises ``SandboxTimeout`` on the first
  line event past the deadline, so an infinite loop in model code stops within
  one line of the cap, while our own helpers (explorer, numpy) run untraced.
* Time spent waiting on the game engine is excluded: ``act()`` wraps its wait
  in ``Sandbox.paused()``.
* ``SandboxTimeout``, ``ActionCap``, ``TurnEnd`` and ``GameStopped`` derive
  from ``BaseException`` and the code is rewritten so ``except:`` and
  ``except BaseException`` become ``except Exception`` -- model code cannot
  swallow the stop signals.
* ``print`` writes to a buffer capped at creation time; nothing is truncated
  afterwards.
* Imports are limited to a small allow-list; ``open``/``eval``/``exec`` and
  friends are absent from the builtins.

Pure stdlib + numpy.  3.10-compatible.
"""
from __future__ import annotations

import ast
import builtins
import contextlib
import sys
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, Optional

FILENAME_PREFIX = "<h30"

ALLOWED_IMPORTS = frozenset({
    "math", "itertools", "collections", "functools", "heapq", "numpy", "re",
    "random", "statistics", "operator", "copy", "dataclasses", "typing", "bisect",
})

_SAFE_BUILTIN_NAMES = (
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter", "float",
    "frozenset", "getattr", "hasattr", "hash", "int", "isinstance", "issubclass",
    "iter", "len", "list", "map", "max", "min", "next", "object", "pow", "range",
    "repr", "reversed", "round", "set", "slice", "sorted", "str", "sum", "tuple",
    "type", "zip", "callable", "chr", "ord", "format", "id", "setattr", "property",
    "staticmethod", "classmethod", "super", "True", "False", "None",
    "Exception", "ValueError", "TypeError", "KeyError", "IndexError", "StopIteration",
    "RuntimeError", "AssertionError", "ZeroDivisionError", "AttributeError",
    "NotImplementedError", "ArithmeticError", "LookupError",
)


class SandboxTimeout(BaseException):
    """The turn's compute cap was reached."""


class ActionCap(BaseException):
    """The turn's action cap was reached."""


class TurnEnd(BaseException):
    """The turn ended on a game event (level up, GAME OVER)."""


class GameStopped(BaseException):
    """The game is over for this policy (won, or the harness stopped it)."""


STOP_SIGNALS = (SandboxTimeout, ActionCap, TurnEnd, GameStopped)


class _NoBaseCatch(ast.NodeTransformer):
    """``except:`` / ``except BaseException`` -> ``except Exception``."""

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> Any:
        self.generic_visit(node)
        if node.type is None or _names_base(node.type):
            node.type = ast.copy_location(ast.Name(id="Exception", ctx=ast.Load()), node)
        return node


def _names_base(t: ast.AST) -> bool:
    if isinstance(t, ast.Name):
        return t.id in ("BaseException", "SandboxTimeout", "ActionCap", "TurnEnd", "GameStopped")
    if isinstance(t, ast.Attribute):
        return t.attr == "BaseException"
    if isinstance(t, ast.Tuple):
        return any(_names_base(e) for e in t.elts)
    return False


def compile_guarded(code: str, filename: str) -> Any:
    """Parse, rewrite the broad except handlers, compile."""
    if not filename.startswith(FILENAME_PREFIX):
        filename = FILENAME_PREFIX + ":" + filename
    tree = ast.parse(code, filename=filename)
    tree = _NoBaseCatch().visit(tree)
    ast.fix_missing_locations(tree)
    return compile(tree, filename, "exec")


@dataclass
class RunResult:
    ok: bool
    stdout: str
    error: str = ""
    stopped_by: str = ""  # "", "timeout", "action_cap", "turn_end", "game_stopped"
    elapsed_s: float = 0.0


class _CappedOut:
    def __init__(self, cap: int) -> None:
        self.cap = int(cap)
        self.parts: list = []
        self.n = 0
        self.capped = False

    def write(self, s: str) -> None:
        if self.capped:
            return
        room = self.cap - self.n
        if len(s) > room:
            # cut at creation: the message is born bounded, never trimmed later
            s = s[:max(0, room)]
            self.capped = True
        self.parts.append(s)
        self.n += len(s)

    def text(self) -> str:
        out = "".join(self.parts)
        if self.capped:
            out += "\n[output capped at %d chars]" % self.cap
        return out


class Sandbox:
    """One persistent namespace per game."""

    def __init__(self, time_cap_s: float = 20.0, print_cap: int = 1500, *,
                 cancel_check: Optional[Callable[[], None]] = None) -> None:
        self.time_cap_s = float(time_cap_s)
        self.print_cap = int(print_cap)
        # SEAM(adk): called on every traced line of model code; it raises a
        # BaseException to stop the run (budget / cancel).  None = h30 behaviour.
        self.cancel_check = cancel_check
        self._out: Optional[_CappedOut] = None
        self._active = False
        self._t0 = 0.0
        self._paused_s = 0.0
        self._cap = self.time_cap_s
        self._pause_depth = 0
        self._pause_t0 = 0.0
        self.ns: Dict[str, Any] = {"__builtins__": self._builtins(), "__name__": "h30"}

    # ------------------------------------------------------------ namespace
    def _builtins(self) -> Dict[str, Any]:
        b: Dict[str, Any] = {}
        for name in _SAFE_BUILTIN_NAMES:
            if hasattr(builtins, name):
                b[name] = getattr(builtins, name)
        b["print"] = self._print
        b["__import__"] = self._import
        b["__build_class__"] = builtins.__build_class__
        return b

    @staticmethod
    def _import(name: str, globals: Any = None, locals: Any = None,
                fromlist: Any = (), level: int = 0) -> Any:
        root = (name or "").split(".")[0]
        if level != 0 or root not in ALLOWED_IMPORTS:
            raise ImportError("import of %r is not allowed in the sandbox" % name)
        return __import__(name, globals, locals, fromlist, level)

    def _print(self, *args: Any, sep: str = " ", end: str = "\n", **_kw: Any) -> None:
        text = sep.join(str(a) for a in args) + end
        if self._out is not None:
            self._out.write(text)

    # ------------------------------------------------------------ the clock
    def elapsed(self) -> float:
        paused = self._paused_s
        if self._pause_depth:
            paused += time.perf_counter() - self._pause_t0
        return time.perf_counter() - self._t0 - paused

    def _check(self) -> None:
        if self.cancel_check is not None and self._active and self._pause_depth == 0:
            self.cancel_check()  # SEAM(adk)
        if self._active and self._pause_depth == 0 and self.elapsed() > self._cap:
            self._active = False
            raise SandboxTimeout("turn compute cap %.1fs reached" % self._cap)

    def _local(self, frame: Any, event: str, arg: Any) -> Any:
        if event == "line":
            self._check()
        return self._local

    def _global(self, frame: Any, event: str, arg: Any) -> Any:
        if frame.f_code.co_filename.startswith(FILENAME_PREFIX):
            self._check()
            return self._local
        return None

    @contextlib.contextmanager
    def paused(self) -> Iterator[None]:
        """Stop the clock (the engine wait inside ``act()``)."""
        if self._pause_depth == 0:
            self._pause_t0 = time.perf_counter()
        self._pause_depth += 1
        try:
            yield
        finally:
            self._pause_depth -= 1
            if self._pause_depth == 0:
                self._paused_s += time.perf_counter() - self._pause_t0

    @contextlib.contextmanager
    def _clock(self, cap_s: Optional[float]) -> Iterator[None]:
        prev_trace = sys.gettrace()
        saved = (self._active, self._t0, self._paused_s, self._cap, self._pause_depth)
        self._active = True
        self._t0 = time.perf_counter()
        self._paused_s = 0.0
        self._pause_depth = 0
        self._cap = float(cap_s if cap_s is not None else self.time_cap_s)
        sys.settrace(self._global)
        try:
            yield
        finally:
            sys.settrace(prev_trace)
            (self._active, self._t0, self._paused_s, self._cap, self._pause_depth) = saved

    # ------------------------------------------------------------ running
    def run(self, code: str, filename: str = "<h30-turn>", cap_s: Optional[float] = None) -> RunResult:
        """Execute ``code`` in the persistent namespace. Never raises except
        for ``GameStopped`` (the harness ended the game)."""
        out = _CappedOut(self.print_cap)
        prev_out = self._out
        self._out = out
        t0 = time.perf_counter()
        try:
            try:
                compiled = compile_guarded(code, filename)
            except SyntaxError as exc:
                return RunResult(False, "", "SyntaxError: %s (line %s)" % (exc.msg, exc.lineno),
                                 elapsed_s=0.0)
            try:
                with self._clock(cap_s):
                    exec(compiled, self.ns)  # noqa: S102 - the sandbox's purpose
                return RunResult(True, out.text(), elapsed_s=time.perf_counter() - t0)
            except SandboxTimeout as exc:
                return RunResult(False, out.text(), str(exc), "timeout", time.perf_counter() - t0)
            except ActionCap as exc:
                return RunResult(True, out.text(), str(exc), "action_cap", time.perf_counter() - t0)
            except TurnEnd as exc:
                return RunResult(True, out.text(), str(exc), "turn_end", time.perf_counter() - t0)
            except GameStopped:
                raise
            except Exception as exc:  # noqa: BLE001 - model code errors are data
                return RunResult(False, out.text(), _short_tb(exc), elapsed_s=time.perf_counter() - t0)
        finally:
            self._out = prev_out

    def call(self, fn: Callable[..., Any], *args: Any, cap_s: Optional[float] = None) -> Any:
        """Call a model-written function under the clock (hypothesis replay).
        Raises ``SandboxTimeout`` past the cap; other errors propagate."""
        if self._active:  # already inside a turn: the turn's clock governs
            return fn(*args)
        out = _CappedOut(self.print_cap)
        prev_out = self._out
        self._out = out
        try:
            with self._clock(cap_s):
                return fn(*args)
        finally:
            self._out = prev_out

    def define(self, source: str, filename: str) -> Optional[str]:
        """Exec a skill/hypothesis definition; returns an error string or None."""
        res = self.run(source, filename=filename, cap_s=2.0)
        return None if res.ok else (res.error or "failed")


def _short_tb(exc: BaseException) -> str:
    frames = [f for f in traceback.extract_tb(exc.__traceback__)
              if f.filename.startswith(FILENAME_PREFIX)]
    where = ""
    if frames:
        f = frames[-1]
        where = " (%s line %s: %s)" % (f.filename, f.lineno, (f.line or "").strip()[:80])
    return "%s: %s%s" % (type(exc).__name__, str(exc)[:300], where)


__all__ = ["Sandbox", "RunResult", "SandboxTimeout", "ActionCap", "TurnEnd", "GameStopped",
           "STOP_SIGNALS", "compile_guarded", "FILENAME_PREFIX", "ALLOWED_IMPORTS"]
