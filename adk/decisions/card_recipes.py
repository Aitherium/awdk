"""Card recipes: an OPTIONS card raised from a template, whose answer becomes an action.

A credential recipe (``recipes.py``) is a different animal: it forces
``kind="credential"``, which the store then refuses to give options or a
default to, because a credential's value travels to the vault and never through
the card. So a template for an ordinary *decidable* card cannot live there, and
it does not: this registry is code, one dict per recipe, no YAML.

Two properties are the whole point, and each exists because of a failure mode
that has a name:

1. **One failing streak raises exactly ONE card.** The producer calls the CLI on
   every failing pass, so without a dedupe key a job failing every minute would
   raise a card every minute — and, worse, would raise a FRESH one the moment
   the owner answered the last. The recipe stamps a streak-scoped
   ``dedupe_key`` and the store refuses a second card for it whatever its
   status, so answering ends the ask instead of restarting it.

2. **The answer is applied, not merely recorded.** These cards have no session:
   a scheduler raised them, and the text-steering path returns early for a card
   with no session id. So the action hook lives in the store's state
   TRANSITION — every answer path (terminal reply, chat DM, the window, a
   deadline expiring) already goes through it — and the mapping from an option
   key to an argv LIST lives here beside the options it answers.

The argv is a list and never a shell string, ``argv[0]`` is resolved from the
environment to an absolute file that must exist, and the only variable
interpolated into it is re-validated against a strict name pattern AT APPLY
TIME — because the input at that point is a JSON file on disk, not the
command-line argument that was checked when the card was raised.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Optional

from adk.decisions.store import DecisionCard, DecisionOption, DecisionSource

#: Job names reach an argv element, so they are checked before anything is built.
#: The first character must be alphanumeric, which is what stops a name ever
#: becoming an OPTION to the command it is passed to (``-name``, ``--help``).
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: How long a synchronous action may take before it is abandoned. A detached
#: action is never waited on at all — see ``apply_answer``.
SYNC_TIMEOUT_SECONDS = 30.0

#: Where ``argv[0]`` comes from. The environment of the process APPLYING the
#: answer, never the card: a payload that could name its own binary would make
#: every card a code-execution primitive.
BIN_ENV = "AWRISE_BIN"
BIN_NAME = "awrise"

#: Windows allocates a console for a console program that has none, and a card is
#: answered from a DETACHED process — which has none. Without this flag a window
#: flashes on the owner's desktop, takes focus, and eats whatever they were
#: typing. Zero on POSIX, where the argument is accepted and means nothing.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class CardRecipeError(Exception):
    """A recipe or variable problem the caller should print and exit 2 on."""


CARD_RECIPES: dict[str, dict[str, Any]] = {
    "wake-failed": {
        "producer": "awrise",
        #: THE ENTITLEMENT THIS RECIPE'S ANSWER SPENDS.
        #:
        #: Answering this card spawns `awrise disable|run --name <job>` on the
        #: owner's host — exactly what POST /wakes/{name}/{disable,run} does, and
        #: those routes are gated on `wakes:mutate`. Measured 2026-09-18: the card
        #: door was gated on the daemon bearer ALONE, so a token refused at
        #: /wakes/x/run reached the identical spawn by raising a `wake-failed`
        #: card and answering it. A recipe that can spawn therefore NAMES the
        #: entitlement its answer spends, and `_invariants` refuses to build a
        #: card for a spawning recipe that names none (fail-closed: a new recipe
        #: is ungatable only by deliberately declaring "" and failing the check).
        "entitlement": "wakes:mutate",
        "required": ("job", "first_failure_ts", "n"),
        "optional": ("last_state", "last_reason", "last_wake_id", "every", "run",
                     "output_tail", "home"),
        #: Variables that reach an argv element. Validated at RAISE time and
        #: again at APPLY time, against ``_NAME_RE``.
        "name_vars": ("job",),
        "defaults": {
            "last_state": "failure",
            "last_reason": "unknown",
            "last_wake_id": "unknown",
            "every": "its schedule",
            "run": "(not recorded)",
            "output_tail": "(no output captured)",
            "home": "~/.aither/awrise",
        },
        #: A card is read on a phone. An unbounded command line or output tail
        #: pushes the options off the screen, which is the one thing that must
        #: never happen to a decidable card.
        "truncate": {"run": 120, "output_tail": 600},
        "kind": "decision",
        #: high, not normal: the chat lane filters below high, so a normal card
        #: would be raised, stored, and reach nobody.
        "urgency": "high",
        "deadline_seconds": 24 * 3600,
        #: Streak-scoped: a new first failure (i.e. a success reset the streak)
        #: is a new question and gets a new card.
        "dedupe_key": "awrise:wake-failed:{job}:{first_failure_ts}",
        #: Every card for this job, whatever its timestamp. The guard for the
        #: case the key cannot cover: the producer recomputing the first-failure
        #: timestamp mid-streak.
        "dedupe_prefix": "awrise:wake-failed:{job}:",
        "title": "Wake '{job}' has failed {n}x in a row",
        "summary": ("awrise job {job} ({every}) has failed {n} consecutive wakes since "
                    "{first_failure_ts}. Last: {last_state} - {last_reason}."),
        "detail": "run: {run}\n\noutput tail:\n{output_tail}\n\nledger: {home}/ledger/",
        "facts": ("job: {job}", "first failure: {first_failure_ts}",
                  "last wake: {last_wake_id}", "streak: {n}"),
        "options": (
            {"key": "disable", "label": "Disable this wake",
             "consequence": "awrise disable --name {job}; nothing runs until you re-enable"},
            {"key": "keep", "label": "Keep it scheduled",
             "consequence": "no change; this streak is not re-raised - a new streak "
                            "(new first failure) raises a new card",
             "recommended": True},
            {"key": "run_now", "label": "Run it once now",
             "consequence": "awrise run --name {job} is started detached (pid recorded on "
                            "the card); read the outcome in the wake list; the schedule and "
                            "streak are unchanged unless it succeeds"},
        ),
        #: The default is what a free-text reply and a deadline both apply, so it
        #: is the ONLY branch that may be reached by accident — and its argv is
        #: None, so neither path can ever spawn a process.
        "default_key": "keep",
        "steerback": {
            "disable": (BIN_NAME, "disable", "--name", "{job}"),
            "keep": None,
            "run_now": (BIN_NAME, "run", "--name", "{job}"),
        },
        #: Started and NOT waited on. The process applying the answer is the
        #: owner's reply channel; blocking it for the length of a scheduled job
        #: stalls the channel, and a timeout kill would abort the very run the
        #: owner just asked for.
        "detach": ("run_now",),
    },
}


# ── registry ───────────────────────────────────────────────────────────────────


def recipe_ids() -> list[str]:
    return sorted(CARD_RECIPES)


def get_recipe(recipe_id: str) -> dict[str, Any]:
    want = (recipe_id or "").strip()
    if want not in CARD_RECIPES:
        raise CardRecipeError(
            f"unknown card recipe {want!r}; known: " + ", ".join(recipe_ids()))
    return CARD_RECIPES[want]


#: What a card whose recipe THIS BUILD DOES NOT KNOW demands. Nobody's token
#: carries it, so a scoped principal is refused and the owner ("*") is not — the
#: fail-closed reading of "a newer producer raised a card I cannot classify".
UNKNOWN_RECIPE_ENTITLEMENT = "decisions:recipe:unknown"


def recipe_entitlement(recipe_id: str) -> str:
    """The entitlement a caller must hold to raise or answer this recipe's cards.

    "" only for a recipe that can spawn NOTHING (every steerback argv is None);
    such a card is a note, and answering it is not a capability. An unknown
    recipe id answers ``UNKNOWN_RECIPE_ENTITLEMENT`` rather than "" — an
    unrecognised recipe must never read as "needs no permission".
    """
    recipe = CARD_RECIPES.get((recipe_id or "").strip())
    if recipe is None:
        return UNKNOWN_RECIPE_ENTITLEMENT
    declared = str(recipe.get("entitlement") or "").strip()
    if declared:
        return declared
    if _can_spawn(recipe):
        # Declared nothing but CAN spawn: _invariants already refuses to build
        # such a card, and this is the second half of the same fail-closed
        # decision for a card already on disk from an older build.
        return UNKNOWN_RECIPE_ENTITLEMENT
    return ""


def _can_spawn(recipe: dict[str, Any]) -> bool:
    """Does ANY answer to this recipe start a process?"""
    steerback = recipe.get("steerback")
    if not isinstance(steerback, dict):
        return False
    return any(template is not None for template in steerback.values())


def spawns_on(recipe_id: str, choice: str) -> bool:
    """Would answering ``choice`` on this recipe start a process?"""
    recipe = CARD_RECIPES.get((recipe_id or "").strip())
    if recipe is None:
        return False
    steerback = recipe.get("steerback")
    if not isinstance(steerback, dict):
        return False
    return steerback.get((choice or "").strip()) is not None


def _invariants(recipe_id: str, recipe: dict[str, Any]) -> list[str]:
    """Structural problems in a recipe. Empty means the recipe is answerable.

    These are the properties every surface relies on and none of them can check:
    an option with no argv entry silently does nothing when answered, and a
    default whose argv is not None turns "the owner ignored it" into a spawn.
    """
    problems: list[str] = []
    options = recipe.get("options") or ()
    keys = [str(o.get("key", "")) for o in options]
    steerback = recipe.get("steerback")
    if not isinstance(steerback, dict):
        return [f"{recipe_id}: no steerback map"]
    if sorted(keys) != sorted(steerback):
        problems.append(
            f"{recipe_id}: option keys {sorted(keys)} != steerback keys {sorted(steerback)}")
    default = recipe.get("default_key", "")
    if default not in keys:
        problems.append(f"{recipe_id}: default_key {default!r} is not an option")
    elif steerback.get(default) is not None:
        problems.append(
            f"{recipe_id}: steerback[{default!r}] must be None - the default is applied by a "
            "deadline and by a free-text reply, so it must never spawn anything")
    if len(options) < 2:
        problems.append(f"{recipe_id}: fewer than two options is not a decision")
    for key in recipe.get("detach", ()):
        if key not in steerback:
            problems.append(f"{recipe_id}: detach names {key!r}, which is not an option")
    # A recipe whose answer spawns is a CAPABILITY, and a capability with no name
    # cannot be gated: the daemon would have nothing to check the caller against,
    # and the card door would be a way around whatever route offers the same verb.
    if _can_spawn(recipe) and not str(recipe.get("entitlement") or "").strip():
        problems.append(
            f"{recipe_id}: answering it spawns a process, so it must declare "
            "'entitlement' - the permission the answering caller must hold")
    return problems


def check_all() -> list[str]:
    problems: list[str] = []
    for recipe_id, recipe in sorted(CARD_RECIPES.items()):
        problems.extend(_invariants(recipe_id, recipe))
    return problems


# ── raising ────────────────────────────────────────────────────────────────────


def _fill(recipe: dict[str, Any], variables: dict[str, str]) -> dict[str, str]:
    """Validated, defaulted, truncated variables — or raise CardRecipeError."""
    required = tuple(recipe.get("required") or ())
    optional = tuple(recipe.get("optional") or ())
    known = set(required) | set(optional)

    supplied = {str(k): ("" if v is None else str(v)) for k, v in (variables or {}).items()}
    unknown = sorted(set(supplied) - known)
    if unknown:
        raise CardRecipeError(
            "unknown var(s): " + ", ".join(unknown) + "; known: " + ", ".join(sorted(known)))

    missing = [name for name in required if not supplied.get(name, "").strip()]
    if missing:
        raise CardRecipeError("needs: " + ", ".join(missing))

    filled: dict[str, str] = {}
    defaults = recipe.get("defaults") or {}
    for name in known:
        value = supplied.get(name, "").strip()
        if not value:
            value = str(defaults.get(name, ""))
        filled[name] = value

    for name in recipe.get("name_vars") or ():
        if not _NAME_RE.match(filled.get(name, "")):
            raise CardRecipeError(
                f"invalid {name} name {filled.get(name, '')!r}: it must start with a letter or "
                "digit and carry only letters, digits, dot, dash or underscore")

    for name, limit in (recipe.get("truncate") or {}).items():
        value = filled.get(name, "")
        if len(value) > limit:
            filled[name] = value[: limit - 1] + "…"
    return filled


def build_card(
    recipe_id: str,
    variables: dict[str, str],
    *,
    now: Optional[float] = None,
    source: Optional[DecisionSource] = None,
) -> DecisionCard:
    """The card a recipe describes, filled from ``variables``. Never writes."""
    recipe = get_recipe(recipe_id)
    try:
        filled = _fill(recipe, variables)
    except CardRecipeError as exc:
        raise CardRecipeError(f"recipe {recipe_id} {exc}") from exc

    problems = _invariants(recipe_id, recipe)
    if problems:
        # A malformed recipe must not become a card whose answer does nothing.
        raise CardRecipeError("; ".join(problems))

    options = [
        DecisionOption(
            key=str(spec["key"]),
            label=str(spec.get("label") or spec["key"]),
            consequence=str(spec.get("consequence") or "").format(**filled),
            recommended=bool(spec.get("recommended")),
        )
        for spec in recipe["options"]
    ]
    stamp = time.time() if now is None else float(now)
    deadline_seconds = recipe.get("deadline_seconds")
    card_source = source or DecisionSource(
        agent=str(recipe.get("producer") or ""),
        session_id="",
        steer_channel="",
    )
    return DecisionCard(
        id="",
        title=str(recipe["title"]).format(**filled),
        summary=str(recipe.get("summary") or "").format(**filled),
        detail=str(recipe.get("detail") or "").format(**filled),
        kind=str(recipe.get("kind") or "decision"),
        urgency=str(recipe.get("urgency") or "normal"),
        options=options,
        default_key=str(recipe.get("default_key") or ""),
        facts=[str(f).format(**filled) for f in (recipe.get("facts") or ())],
        source=card_source,
        created_at=stamp,
        deadline=(stamp + float(deadline_seconds)) if deadline_seconds else None,
        dedupe_key=str(recipe.get("dedupe_key") or "").format(**filled),
        card_recipe=recipe_id,
        recipe_vars=dict(filled),
    )


def dedupe_prefix(recipe_id: str, variables: dict[str, str]) -> str:
    """The "no second OPEN card for this subject" prefix, or "" when the recipe has none."""
    recipe = get_recipe(recipe_id)
    template = str(recipe.get("dedupe_prefix") or "")
    if not template:
        return ""
    filled = _fill(recipe, variables)
    return template.format(**filled)


# ── applying the answer ────────────────────────────────────────────────────────


def resolve_bin() -> str:
    """Absolute path of the producer CLI, or "" when it is not installed here."""
    raw = os.environ.get(BIN_ENV, "").strip() or (shutil.which(BIN_NAME) or "")
    if not raw:
        return ""
    resolved = os.path.abspath(raw)
    return resolved if os.path.isfile(resolved) else ""


def _detached(argv: list[str]) -> "subprocess.Popen[bytes]":
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        # NOT DETACHED_PROCESS: Windows ignores CREATE_NO_WINDOW when the two are
        # combined, so the child has NO console, and the first console program it
        # starts (a .cmd launcher running python) allocates a visible one — a
        # terminal tab that takes focus. A hidden console is inherited instead.
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | _CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs)


def apply_answer(card: DecisionCard) -> tuple[bool, str]:
    """Turn a closed card's answer into the action it promised. ``(applied, why)``.

    NEVER raises: it is called from inside the store's state transition, and a
    failure to act must not undo an answer that is already recorded.
    """
    try:
        recipe_id = (getattr(card, "card_recipe", "") or "").strip()
        if not recipe_id:
            return False, ""
        recipe = CARD_RECIPES.get(recipe_id)
        if recipe is None:
            return False, f"refused: unknown card recipe {recipe_id!r}"
        steerback = recipe.get("steerback") or {}
        key = (card.answer or "").strip()
        if key not in steerback:
            return False, f"refused: {key!r} has no action on recipe {recipe_id}"
        template = steerback[key]
        if template is None:
            return True, "no-op"

        variables = getattr(card, "recipe_vars", None)
        if not isinstance(variables, dict):
            return False, "refused: no recipe vars on the card"
        # The card JSON on disk is writable by anything that can reach the card
        # directory, so the name that reaches an argv element is re-validated
        # HERE and not trusted from raise time.
        safe: dict[str, str] = {}
        for name in recipe.get("name_vars") or ():
            value = variables.get(name)
            if not isinstance(value, str) or not _NAME_RE.match(value):
                return False, f"refused: {name} name invalid at apply time"
            safe[name] = value

        binary = resolve_bin()
        if not binary:
            return False, "refused: awrise binary not found"

        try:
            argv = [binary] + [str(part).format(**safe) for part in template[1:]]
        except (KeyError, IndexError) as exc:
            return False, f"refused: recipe argv names an unvalidated var ({exc})"
        spoken = " ".join([str(template[0])] + argv[1:])

        if key in (recipe.get("detach") or ()):
            proc = _detached(argv)
            return True, f"{spoken} started detached pid {proc.pid}"

        try:
            done = subprocess.run(argv, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  creationflags=_CREATE_NO_WINDOW,
                                  timeout=SYNC_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return False, f"timeout after {int(SYNC_TIMEOUT_SECONDS)}s"
        return done.returncode == 0, f"{spoken} -> exit {done.returncode}"
    except Exception as exc:  # never propagate into a recorded answer
        return False, f"refused: {exc.__class__.__name__}: {exc}"


# ── self-test ──────────────────────────────────────────────────────────────────


def _self_test() -> int:
    """Each arm has a negative twin: the check is watched failing before it is trusted."""
    import json
    import tempfile
    from pathlib import Path

    from adk.decisions.store import DecisionStore

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if condition else 'FAIL'} {name}{'' if condition else ' ' + detail}")
        if not condition:
            failures.append(name)

    def marker_bin(tmp: Path, marker: Path) -> str:
        """A fake producer CLI that records that it RAN. Its absence is the proof."""
        if os.name == "nt":
            script = tmp / "fake_awrise.cmd"
            script.write_text(
                "@echo off\r\n"
                f'echo ran %* > "{marker}"\r\n'
                "exit /b 0\r\n",
                encoding="utf-8")
        else:
            script = tmp / "fake_awrise.sh"
            script.write_text(
                "#!/bin/sh\n"
                f'echo "ran $*" > "{marker}"\n'
                "exit 0\n",
                encoding="utf-8")
            script.chmod(0o755)
        return str(script)

    real = CARD_RECIPES["wake-failed"]

    # (a) every option has an action entry — and a recipe that loses one is caught.
    check("every option key has a steerback entry", not _invariants("wake-failed", real))
    broken = {**real, "steerback": {**real["steerback"], "stray": ("awrise", "x")}}
    check("a steerback key with no option FAILS", bool(_invariants("broken", broken)))

    # (b) the default is one of the options.
    broken = {**real, "default_key": "nope"}
    check("a default that is not an option FAILS", bool(_invariants("broken", broken)))

    # (c) the default never spawns.
    broken = {**real, "steerback": {**real["steerback"], "keep": ("awrise", "disable")}}
    check("a default with an argv FAILS", bool(_invariants("broken", broken)))
    check("the real default maps to no argv", real["steerback"][real["default_key"]] is None)

    # (c2) a spawning recipe NAMES the entitlement its answer spends, and one
    # that forgets is refused — the card door cannot be a way around the route
    # door offering the same verb.
    check("the real recipe names its entitlement",
          recipe_entitlement("wake-failed") == "wakes:mutate",
          recipe_entitlement("wake-failed"))
    broken = {k: v for k, v in real.items() if k != "entitlement"}
    check("a spawning recipe with NO entitlement FAILS",
          any("entitlement" in problem for problem in _invariants("broken", broken)))
    check("an unknown recipe demands the unknown-recipe entitlement",
          recipe_entitlement("no-such-recipe") == UNKNOWN_RECIPE_ENTITLEMENT)
    CARD_RECIPES["_selftest-inert"] = {"steerback": {"a": None, "b": None}}
    try:
        check("a recipe that spawns nothing demands nothing",
              recipe_entitlement("_selftest-inert") == "")
    finally:
        CARD_RECIPES.pop("_selftest-inert", None)
    check("spawns_on is true only for the spawning choices",
          spawns_on("wake-failed", "run_now") and spawns_on("wake-failed", "disable")
          and not spawns_on("wake-failed", "keep")
          and not spawns_on("wake-failed", "not-an-option"))

    base = {"job": "nightly-sync", "first_failure_ts": "2026-09-18T05:00:01+00:00", "n": "3"}

    # (d) raise time refuses a name that could become an argument or a command.
    for bad in ("x; rm -rf /", "", "-name", "../x", "a b", "z" * 65):
        try:
            build_card("wake-failed", {**base, "job": bad})
            check(f"raise-time refuses job {bad!r}", False, "it was accepted")
        except CardRecipeError:
            check(f"raise-time refuses job {bad!r}", True)
    try:
        good = build_card("wake-failed", base)
        check("raise-time accepts a real job name", good.title.startswith("Wake 'nightly-sync'"))
        check("the card carries a streak-scoped dedupe key",
              good.dedupe_key == "awrise:wake-failed:nightly-sync:2026-09-18T05:00:01+00:00",
              good.dedupe_key)
    except CardRecipeError as exc:
        check("raise-time accepts a real job name", False, str(exc))
    try:
        build_card("wake-failed", {"job": "nightly-sync"})
        check("missing required vars are refused", False, "it was accepted")
    except CardRecipeError as exc:
        check("missing required vars are refused", "needs" in str(exc), str(exc))

    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        marker = tmp / "ran.txt"
        os.environ["AWRISE_BIN"] = marker_bin(tmp, marker)

        # (e) apply time refuses an on-disk name the raise-time check never saw.
        for bad in ("x; rm -rf /", "", "-name", 17):
            card = build_card("wake-failed", base)
            card.answer = "disable"
            card.recipe_vars = {**card.recipe_vars, "job": bad}
            applied, why = apply_answer(card)
            check(f"apply-time refuses job {bad!r}",
                  not applied and "invalid at apply time" in why, why)
        check("nothing was spawned by any refusal", not marker.exists())

        # The positive twin: the same path DOES spawn when the name is sound.
        card = build_card("wake-failed", base)
        card.answer = "disable"
        applied, why = apply_answer(card)
        check("a sound name runs the action", applied and "exit 0" in why, why)
        check("the fake producer really ran", marker.exists())
        if marker.exists():
            recorded = marker.read_text(encoding="utf-8", errors="replace")
            check("the action named the job", "nightly-sync" in recorded, recorded.strip())
            marker.unlink()

        # keep is a no-op, and that is asserted rather than assumed.
        card = build_card("wake-failed", base)
        card.answer = "keep"
        applied, why = apply_answer(card)
        check("keep applies nothing", applied and why == "no-op", why)
        check("keep spawned nothing", not marker.exists())

        # (f) a binary that is not a file refuses, and does not spawn.
        os.environ["AWRISE_BIN"] = str(tmp / "there-is-no-such-file")
        card = build_card("wake-failed", base)
        card.answer = "disable"
        applied, why = apply_answer(card)
        check("a missing binary refuses", not applied and "not found" in why, why)
        check("a missing binary spawned nothing", not marker.exists())
        os.environ["AWRISE_BIN"] = marker_bin(tmp, marker)

        # (g)/(h) dedupe: one streak, one card — even after the first is CLOSED.
        os.environ["AITHER_DECISIONS_DIR"] = str(tmp / "cards")
        os.environ["AITHER_STEER_DIR"] = str(tmp / "steer")
        store = DecisionStore(tmp / "cards")
        first = store.create(build_card("wake-failed", base))
        again = store.create(build_card("wake-failed", base))
        check("a repeat raise returns the first card", again.id == first.id,
              f"{first.id} vs {again.id}")
        check("a repeat raise writes no second file",
              len(list((tmp / "cards").glob("d-*.json"))) == 1)
        store.answer(first.id, "keep", via="selftest", deliver=False)
        third = store.create(build_card("wake-failed", base))
        check("an ANSWERED streak is not re-raised", third.id == first.id,
              f"{first.id} vs {third.id}")
        check("an answered repeat writes no second file",
              len(list((tmp / "cards").glob("d-*.json"))) == 1)

        shifted = {**base, "first_failure_ts": "2026-09-18T06:00:01+00:00"}
        fourth = store.create(build_card("wake-failed", shifted))
        check("a new streak after a CLOSED card does raise a new card",
              fourth.id != first.id)
        fifth = store.create(build_card(
            "wake-failed", {**base, "first_failure_ts": "2026-09-18T07:00:01+00:00"}))
        check("a second OPEN card for the same job is refused", fifth.id == fourth.id,
              f"{fourth.id} vs {fifth.id}")

        # The recorded answer really becomes an action, on a card with NO session.
        marker.unlink(missing_ok=True)
        answered = store.answer(fourth.id, "disable", via="selftest", deliver=True)
        check("a sessionless card still runs its action", marker.exists())
        check("the outcome is recorded on the card",
              any("Steerback" in n.text for n in answered.notes),
              json.dumps([n.text for n in answered.notes]))

    print()
    if failures:
        print(f"card-recipes self-test FAILED - {len(failures)}: {', '.join(failures)}")
        return 1
    print("card-recipes self-test passed - one streak one card, and the default never spawns")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        raise SystemExit(_self_test())
    print("card recipes: " + ", ".join(recipe_ids()))
    problems = check_all()
    for problem in problems:
        print(f"  BROKEN {problem}", file=sys.stderr)
    raise SystemExit(1 if problems else 0)
