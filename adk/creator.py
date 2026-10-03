"""Creator mode: an agent authors a tool pack, switches it on, and uses it in one session.

A pack has always been the unit of capability for an awdk agent, and
``adk pack new|validate|dev|build`` has long been the authoring path, but only
for a human at a terminal. Creator mode gives the same verbs to the agent:

    aw_pack_new(pack_id)       scaffold a working pack in the creator dir
    aw_pack_validate(pack_id)  static checks; nothing is imported
    aw_pack_on(pack_id)        validate, then load it onto THIS agent (approval-gated)
    aw_pack_off(pack_id)       reverse everything aw_pack_on did
    aw_pack_active()           what is mounted now, and what each pack added

The agent edits the scaffold with its ordinary file tools between ``new`` and
``on``. ``on`` after an edit reloads the pack from source. ``aw_pack_on`` runs pack
code in-process with the host user's permissions, so it is a runtime approval
gate (``adk.approval.set_runtime_gates``): the loop pauses for an Allow/Deny on
every call, keyed per call. A pack that fails validation never reaches import.

Turn it on with ``AITHER_CREATOR_MODE=1`` (read at agent construction) or
:func:`enable_creator`. Packs live in ``AITHER_CREATOR_PACKS_DIR``, default
``~/.aither/packs/creator``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

GATED = ("aw_pack_on",)


def creator_dir() -> Path:
    env = os.environ.get("AITHER_CREATOR_PACKS_DIR", "").strip()
    d = Path(env).expanduser() if env else Path.home() / ".aither" / "packs" / "creator"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _purge_modules(pack_id: str) -> None:
    """Drop a file-loaded pack's modules so the next load reads the edited source."""
    from adk.tool_pack_loader import _module_name

    root = _module_name(pack_id)
    for key in [k for k in sys.modules if k == root or k.startswith(root + ".")]:
        sys.modules.pop(key, None)


def _report(rep: Any) -> dict:
    return {"ok": bool(rep.ok), "judged": bool(rep.judged),
            "findings": [str(f) for f in rep.findings]}


def enable_creator(agent: Any) -> list[str]:
    """Register the aw_pack_* tools on *agent* and gate the one that runs code."""
    from adk import pack_author
    from adk.approval import runtime_gates, set_runtime_gates
    from adk.pack_activation import activate, deactivate
    from adk.tool_pack_loader import ToolPackLoader

    reg = agent._tools

    def aw_pack_new(pack_id: str) -> str:
        """Scaffold a new tool pack (manifest, one tool, test). Returns its files."""
        try:
            path = pack_author.scaffold(pack_id, creator_dir())
        except (ValueError, FileExistsError) as exc:
            return json.dumps({"error": str(exc)})
        files = sorted(str(p) for p in path.rglob("*") if p.is_file())
        return json.dumps({"pack_dir": str(path), "files": files,
                           "next": "edit tools.py / __init__.py, then aw_pack_on"})

    def aw_pack_validate(pack_id: str) -> str:
        """Statically validate a creator pack. Nothing is imported or executed."""
        d = creator_dir() / pack_id
        if not d.is_dir():
            return json.dumps({"error": f"no creator pack {pack_id!r}"})
        return json.dumps(_report(pack_author.validate(d)))

    def aw_pack_on(pack_id: str) -> str:
        """Validate a creator pack, then load it onto this agent (reloads if active)."""
        d = creator_dir() / pack_id
        if not d.is_dir():
            return json.dumps({"error": f"no creator pack {pack_id!r}"})
        rep = pack_author.validate(d)
        if not rep.ok:
            return json.dumps({"error": "validation failed", **_report(rep)})
        deactivate(agent, pack_id)
        _purge_modules(pack_id)
        loader = ToolPackLoader(extra_dirs=[creator_dir()], enforce_entitlements=False)
        loader.discover()
        found = [m for m in loader.load_packs([pack_id]) if Path(m.path) == d]
        if not found:
            return json.dumps({"error": f"{pack_id!r} not discoverable (id shadowed?)"})
        return json.dumps(activate(agent, found[0], loader).summary())

    def aw_pack_off(pack_id: str) -> str:
        """Unload a pack: every tool, directive and skill it added is removed."""
        act = deactivate(agent, pack_id)
        if act is None:
            return json.dumps({"error": f"{pack_id!r} is not active"})
        return json.dumps(act.summary())

    def aw_pack_active() -> str:
        """List the packs active on this agent and what each one added."""
        acts = getattr(agent, "_pack_activations", {}) or {}
        return json.dumps([a.summary() for a in acts.values()])

    fns = [aw_pack_new, aw_pack_validate, aw_pack_on, aw_pack_off, aw_pack_active]
    for fn in fns:
        reg.register(fn)
    set_runtime_gates(agent.name, runtime_gates(agent.name) | set(GATED))
    return [fn.__name__ for fn in fns]
