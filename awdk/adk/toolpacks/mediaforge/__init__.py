"""media-forge tool pack — the creative engine on the operator's OWN machine.

WHAT THIS FIXES. The manifest beside this file declares `mcp_tools:
["mediaforge_*"]` with `tool_modules: []`, which makes the loader file-load THIS
module by path and call `register()`. Until this file existed the pack advertised
24 tools and registered **zero** — a manifest over a hole, and the phantom-
capability shape every gate in this repo exists to catch. A pack that declares a
capability and binds nothing is worse than an absent pack, because it reads as
authoritative.

WHY THE TOOL MODULES ARE COPIES AND NOT IMPORTS. `aw-family.md` says settle
"client, not lift" with numbers, so it was measured: `mcp_character_forge.py` and
`mcp_color_forge.py` import only `os`, `time` and `requests` — **zero** monorepo
imports — and already resolve `AITHER_MEDIAFORGE_URL` themselves. They are
already standalone HTTP clients against the engine, so carrying them here keeps
this pack installable by a stranger. Importing them from
`AitherOS/apps/awnode/tools/mcp/` instead would be a `ModuleNotFoundError` on
every machine that is not this monorepo — gate 1i's UNSHIPPED IMPORT class, which
ships a BROKEN package rather than a disclosure.

    They are copies, so they can drift. The originals live at
    AitherOS/apps/awnode/tools/mcp/mcp_{character,color}_forge.py. If those
    start changing, this needs a parity check of the kind
    check_awgit_mirror.py performs — a comment is not a gate, and this repo has
    already watched two copies of one file drift while a comment inside them
    asked people to keep them in step.

🪤 `_BASE` IS RESOLVED AT IMPORT TIME in both modules, from
`AITHER_MEDIAFORGE_URL`. So pointing the pack at a different engine is **not** a
live config change: a process that imported these keeps the old URL until it is
restarted. That is the bind-mount-staleness class (gate 1p) wearing an env var,
and it is exactly how the whole surface stayed inert — the gateway quadlet never
carried the variable at all, so every tool fell back to a hostname with no
service and failed as "media-forge is down".

THE ENGINE IS THEIRS. These tools reach a process the operator runs
(`availability: local`). An unreachable engine is reported as an error by the
clients themselves; this module does not invent a fallback, because a wrong
answer about whether a render happened is worse than a missing one.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("mediaforge")

PACK_ID = "mediaforge"

#: The vendored client modules, in the order their tools should register.
_TOOL_MODULES = ("mcp_character_forge", "mcp_color_forge", "mcp_mesh_forge")

#: Every tool this pack owns is named for the manifest's `mediaforge_*` glob.
#: Enumerated by PREFIX rather than hand-listed on purpose: a hand-list is a
#: second index over the same truth and it rots, which is the failure that
#: produced the 24-declared/0-registered state this module fixes.
_PREFIX = "mediaforge_"


def _collect() -> list:
    """Every public `mediaforge_*` callable across the vendored client modules.

    A module that will not import is logged and SKIPPED rather than raising, so
    one broken client cannot take the whole pack down — but the count is
    returned to the caller, so a partial load is visible rather than silent.
    """
    import importlib

    found: list = []
    for mod_name in _TOOL_MODULES:
        try:
            mod = importlib.import_module(f".{mod_name}", __name__)
        except Exception as exc:                      # noqa: BLE001 - see docstring
            logger.warning("mediaforge: %s unavailable (%s) - its tools are absent",
                           mod_name, exc)
            continue
        names = sorted(n for n in dir(mod) if n.startswith(_PREFIX))
        if not names:
            logger.warning("mediaforge: %s exported no %s* tools", mod_name, _PREFIX)
        for name in names:
            fn = getattr(mod, name, None)
            if callable(fn):
                found.append(fn)
    return found


def register(registry) -> int:
    """Register every vendored `mediaforge_*` tool. Returns the count.

    `availability: local` means there is nothing to entitle — the compute is the
    operator's — so all tools register unconditionally. Reachability is a
    RUNTIME question each tool answers for itself; refusing to register here
    because the engine happens to be down would make a temporarily-stopped
    engine indistinguishable from an uninstalled pack.
    """
    fns = _collect()
    if not fns:
        logger.warning("mediaforge pack unavailable - 0 tools registered")
        return 0
    n = 0
    for fn in fns:
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:                      # noqa: BLE001
            logger.debug("mediaforge: skip tool %s: %s",
                         getattr(fn, "__name__", "?"), exc)
    logger.info("media-forge registered %d %s* tools", n, _PREFIX)
    return n


def self_test() -> int:
    """Prove this module can still fail. Exit 0 pass, 1 violation, 2 cannot judge.

    Asserts the two things that were actually wrong here, because a self-test
    that only checks the happy path would have passed on the empty pack:
      1. the enumeration finds tools at all (the 24-declared/0-bound state);
      2. a registry that REFUSES everything yields 0, not a false count.
    """
    fns = _collect()
    if not fns:
        print("MF001 cannot judge: no mediaforge_* tools found - are the "
              "vendored client modules present beside this file?")
        return 2

    class _Accept:
        def __init__(self) -> None:
            self.seen: list = []

        def register(self, fn) -> None:
            self.seen.append(getattr(fn, "__name__", "?"))

    class _Refuse:
        def register(self, fn) -> None:
            raise RuntimeError("refused")

    ok = _Accept()
    good = register(ok)
    bad = register(_Refuse())

    fails = []
    if good != len(fns):
        fails.append(f"MF001 accepting registry took {good} of {len(fns)} tools")
    if bad != 0:
        fails.append(f"MF002 refusing registry still counted {bad} tools")
    if any(not n.startswith(_PREFIX) for n in ok.seen):
        offenders = [n for n in ok.seen if not n.startswith(_PREFIX)]
        fails.append(f"MF003 tools outside the declared glob: {offenders}")

    for f in fails:
        print(f)
    if fails:
        return 1
    print(f"OK - {good} {_PREFIX}* tools register; a refusing registry yields 0")
    return 0
