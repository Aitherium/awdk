"""ModelBackend protocol conformance suite.

The objective: every concrete backend fulfils the SAME ``ModelBackend``
contract, and the contract *as declared* matches the contract *as
implemented*. A deviation -- a backend missing a member, a member added
to one backend but not the others, or a Protocol stub whose async kind
disagrees with every implementation -- is a bug, and shows up here as a
failure rather than as a ``TypeError`` at the first real call.

Checks are introspective: no credentials, no network, no subprocess.
Every backend below constructs from dummy kwargs (constructors only
store config; clients are built lazily per call), so this suite runs
anywhere.

Why this exists
---------------
``adk`` carries two parallel LLM abstraction layers -- this one
(``adk.core.model.ModelBackend``, used by ``adk.core.*`` and
``adk.reasoning.*``) and ``adk.llm.base.LLMProvider`` (used by the CLI
and most of the app surface). Neither had a conformance test, and the
drift that produces is silent: it only surfaces when a caller trusts a
declaration that no implementation honours.

Adapted from the conformance-suite pattern in rekursiv-ai/sagent
(Apache-2.0); reimplemented against our own protocol, no code copied.
See ``.RESEARCH/INTAKE/sagent/DOSSIER.md``.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest

from adk.core.backends.anthropic import AnthropicBackend
from adk.core.backends.ollama import OllamaBackend
from adk.core.backends.openai_compat import (
    DeepSeekBackend,
    MoonshotBackend,
    OpenAIBackend,
    VLLMBackend,
    _OpenAICompatBackend,
)
from adk.core.model import ModelBackend
from adk.reasoning.speculative import SpeculativeBackend


# Every concrete class that claims to fulfil ``ModelBackend``, paired with
# kwargs that construct it. Add a new backend here -- the suite then holds
# it to the identical surface as the rest.
#
# ``SpeculativeBackend`` is a composing façade ("ModelBackend façade that
# drafts with FAST and verifies with REASONING") and belongs here for the
# same reason as the leaf backends: it is passed wherever a ModelBackend is
# expected. It was missed on the first pass of this suite -- a reminder that
# a conformance matrix silently degenerates unless it is derived from, or
# checked against, the real set of implementations.
_BACKENDS: list[tuple[type, dict[str, Any]]] = [
    (AnthropicBackend, {"api_key": "test-key"}),
    (OllamaBackend, {}),
    (_OpenAICompatBackend, {"base_url": "http://x", "model": "m", "api_key": "k"}),
    (OpenAIBackend, {"api_key": "test-key"}),
    (VLLMBackend, {"base_url": "http://x"}),
    (DeepSeekBackend, {"api_key": "test-key"}),
    (MoonshotBackend, {"api_key": "test-key"}),
    (
        SpeculativeBackend,
        {
            "draft": OllamaBackend(model="draft-m"),
            "verifier": OllamaBackend(model="verify-m"),
        },
    ),
]

_BACKEND_CLASSES = [cls for cls, _ in _BACKENDS]

# Callable members the protocol declares.
_PROTOCOL_METHODS = frozenset({"generate", "stream"})

# Data members the protocol declares as annotations. ``dir()`` cannot see
# these -- a bare ``name: str`` on a Protocol is an annotation, not an
# attribute -- so they are read from ``__annotations__`` and asserted
# against constructed instances instead.
_PROTOCOL_DATA_MEMBERS = frozenset({"name", "model"})


def _ids() -> list[str]:
    return [cls.__name__ for cls in _BACKEND_CLASSES]


@pytest.mark.parametrize("cls", _BACKEND_CLASSES, ids=_ids())
def test_backend_implements_every_protocol_method(cls: type) -> None:
    """Each backend supplies every callable member of the contract."""
    missing = sorted(m for m in _PROTOCOL_METHODS if not callable(getattr(cls, m, None)))
    assert not missing, (
        f"{cls.__name__} is missing ModelBackend method(s): {missing}"
    )


@pytest.mark.parametrize("cls", _BACKEND_CLASSES, ids=_ids())
def test_generate_is_a_coroutine_function(cls: type) -> None:
    """``generate`` returns a value: it must be awaitable, not a generator."""
    fn = cls.generate
    assert inspect.iscoroutinefunction(fn), (
        f"{cls.__name__}.generate must be `async def` returning ModelResponse; "
        f"got {'async generator' if inspect.isasyncgenfunction(fn) else type(fn)}"
    )


@pytest.mark.parametrize("cls", _BACKEND_CLASSES, ids=_ids())
def test_stream_is_an_async_generator_function(cls: type) -> None:
    """``stream`` yields chunks: callers use ``async for``, never ``await``.

    An implementation that became a plain coroutine would break every
    ``async for chunk in backend.stream(...)`` call site (e.g.
    ``adk/reasoning/speculative.py``) with a TypeError at runtime.
    """
    fn = cls.stream
    assert inspect.isasyncgenfunction(fn), (
        f"{cls.__name__}.stream must be an async generator (`async def` + `yield`); "
        f"callers do `async for ... in backend.stream(...)`"
    )


def test_protocol_stream_declaration_matches_the_implementations() -> None:
    """The declared ``stream`` kind must match what every backend does.

    Regression guard for a contract-truth defect found 2026-07-26: the
    Protocol declared ``async def stream(...) -> AsyncIterator[str]`` --
    a *coroutine* returning an iterator, i.e. ``it = await b.stream(...)``
    -- while all 7 implementations are async *generators*, i.e.
    ``async for c in b.stream(...)``. A caller following the annotation
    got ``TypeError: object async_generator can't be used in 'await'
    expression``. The correct Protocol form for an async generator is a
    plain ``def`` returning ``AsyncIterator``.

    This asserts the declaration against the implementations rather than
    hardcoding either, so it stays true if the contract legitimately
    changes -- but never lets the two drift apart silently again.
    """
    declared_is_asyncgen = inspect.isasyncgenfunction(ModelBackend.stream)
    declared_is_coroutine = inspect.iscoroutinefunction(ModelBackend.stream)

    impls_are_asyncgen = {
        cls.__name__: inspect.isasyncgenfunction(cls.stream)
        for cls in _BACKEND_CLASSES
    }
    assert all(impls_are_asyncgen.values()), (
        f"precondition: not every backend streams as an async generator: "
        f"{sorted(k for k, v in impls_are_asyncgen.items() if not v)}"
    )

    assert not declared_is_coroutine, (
        "ModelBackend.stream is declared `async def` (a coroutine returning an "
        "AsyncIterator), but every implementation is an async generator. A caller "
        "trusting the annotation and writing `await backend.stream(...)` gets a "
        "TypeError. Declare it as `def stream(...) -> AsyncIterator[str]: ...`"
    )
    # A Protocol stub with no `yield` is never itself an async generator;
    # the plain-`def` form is what correctly types one.
    assert not declared_is_asyncgen, (
        "unexpected: Protocol stub reports as an async generator function"
    )


@pytest.mark.parametrize(
    ("cls", "kwargs"), _BACKENDS, ids=_ids()
)
def test_protocol_data_members_are_populated_on_instances(
    cls: type, kwargs: dict[str, Any]
) -> None:
    """``name`` and ``model`` are declared by the contract, so they must exist.

    Asserted on a constructed instance, not on the class: ``model`` is set
    in ``__init__`` and is invisible to any class-level check. Both must
    be non-empty strings -- a backend whose ``model`` is ``None`` routes
    a request to nothing and fails at the provider, not here.
    """
    instance = cls(**kwargs)
    for member in sorted(_PROTOCOL_DATA_MEMBERS):
        value = getattr(instance, member, None)
        assert isinstance(value, str) and value, (
            f"{cls.__name__}().{member} must be a non-empty str; got {value!r}"
        )


def test_backend_names_are_unique() -> None:
    """``name`` identifies a backend in config and CLI output.

    Two backends sharing a name makes ``adk backend switch`` ambiguous and
    silently routes to whichever the resolver happens to hit first.
    ``_OpenAICompatBackend`` is the shared base, so its name is expected
    to differ from every concrete subclass's.
    """
    names = [cls.name for cls in _BACKEND_CLASSES]  # type: ignore[attr-defined]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert not duplicates, f"backend name collision: {duplicates}"


@pytest.mark.parametrize("cls", _BACKEND_CLASSES, ids=_ids())
def test_backend_exposes_no_public_surface_beyond_the_contract(cls: type) -> None:
    """Extra public *instance* members are a deviation callers ``getattr``-probe.

    The contract is the ceiling for the instance surface: promote a
    genuinely shared member to the protocol so all backends implement it,
    or make it private. ``name`` and ``model`` are contract data members
    and so are allowed.

    Classmethod/staticmethod factories (``SpeculativeBackend.from_router``)
    are deliberately exempt: they are construction helpers on the class, not
    part of the surface a holder of a ``ModelBackend`` calls, so they create
    no probe hazard.
    """
    allowed = _PROTOCOL_METHODS | _PROTOCOL_DATA_MEMBERS
    own_public = {
        n
        for n, v in vars(cls).items()
        if not n.startswith("_")
        and n not in allowed
        and not isinstance(v, (classmethod, staticmethod))
    }
    assert not own_public, (
        f"{cls.__name__} exposes public members outside the ModelBackend "
        f"contract: {sorted(own_public)} -- promote to the protocol or make private"
    )


# ----------------------------------------------------------------------
# getattr-smell guard: consumers must not probe a total contract.
# ----------------------------------------------------------------------

_GUARDED_FILES = [
    "adk/core/model.py",
    "adk/core/agent.py",
    "adk/core/codeact.py",
    "adk/reasoning/speculative.py",
]


def _getattr_string_literals(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, name)`` for every ``getattr(obj, "<name>", ...)``."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            continue
        out.append((node.lineno, node.args[1].value))
    return out


@pytest.mark.parametrize("rel", _GUARDED_FILES)
def test_no_getattr_probe_of_backend_contract_members(rel: str) -> None:
    """Consumers never ``getattr``-probe a ``ModelBackend`` contract member.

    The contract is total, so probing it is the smell: ``getattr(backend,
    "stream", None)`` silently degrades to a no-op when a backend is
    misconfigured, instead of failing loudly. ``getattr`` of NON-contract
    attributes (SDK exception fields, private names) is fine and not
    flagged.
    """
    root = Path(__file__).resolve().parent.parent
    path = root / rel
    if not path.exists():  # guarded file moved -- surface it, don't skip silently
        pytest.fail(f"guarded file {rel} does not exist; update _GUARDED_FILES")

    tree = ast.parse(path.read_text(encoding="utf-8"))
    contract = _PROTOCOL_METHODS | _PROTOCOL_DATA_MEMBERS
    offenders = [
        (lineno, name)
        for lineno, name in _getattr_string_literals(tree)
        if name in contract
    ]
    assert not offenders, (
        f"{rel}: getattr-probe of ModelBackend contract member(s) {offenders}; "
        "call the member directly -- the contract is total"
    )
