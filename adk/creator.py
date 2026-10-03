"""Creator mode: an agent authors a tool pack, switches it on, and uses it in one session.

A pack has always been the unit of capability for an awdk agent, and
``adk pack new|validate|dev|build`` has long been the authoring path, but only
for a human at a terminal. Creator mode gives the same verbs to the agent:

    aw_pack_new(pack_id)                   scaffold a pack (or hand back an existing one)
    aw_pack_read(pack_id, path)            read one file of that pack
    aw_pack_write(pack_id, path, content)  replace one file of that pack
    aw_pack_validate(pack_id)              static checks; nothing is imported
    aw_pack_on(pack_id)                    validate, then load onto THIS agent (approval-gated)
    aw_pack_off(pack_id)                   reverse everything aw_pack_on did
    aw_pack_active()                       what is mounted now, and what each pack added

The ordinary file tools are jailed to the allowed roots and the creator dir is
deliberately not one of them; ``aw_pack_read``/``aw_pack_write`` reach nothing
outside the pack they name. ``on`` after an edit reloads the pack from source.
``aw_pack_on`` runs pack code in-process with the host user's permissions, so it
is a runtime approval gate (``adk.approval.set_runtime_gates``): the loop pauses
for an Allow/Deny on every call, keyed per call. A pack that fails validation never reaches import.

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
        exists = False
        try:
            path = pack_author.scaffold(pack_id, creator_dir())
        except FileExistsError:
            # A turn resumed after an approval replays from the start; the second
            # new must hand back the pack, not an error that derails the replay.
            path, exists = creator_dir() / pack_id, True
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        files = sorted(p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file())
        tools_src = path / "tools.py"
        return json.dumps({"pack_id": pack_id, "exists": exists, "files": files,
                           "tools.py": (tools_src.read_text(encoding="utf-8")
                                        if tools_src.is_file() else ""),
                           "next": "aw_pack_write(pack_id, 'tools.py', ...), then aw_pack_on"})

    def _in_pack(pack_id: str, path: str) -> tuple[Path | None, Path | None, str]:
        root = (creator_dir() / pack_id).resolve()
        if not root.is_dir():
            return None, None, f"no creator pack {pack_id!r}; aw_pack_new first"
        target = (root / path).resolve()
        if target == root or not target.is_relative_to(root):
            return None, None, f"{path!r} is outside pack {pack_id!r}"
        return root, target, ""

    def aw_pack_read(pack_id: str, path: str) -> str:
        """Read one file (e.g. tools.py) of a creator pack. Use this, not file_read."""
        root, target, err = _in_pack(pack_id, path)
        if err:
            return json.dumps({"error": err})
        if not target.is_file():
            return json.dumps({"error": f"{path!r} does not exist in {pack_id!r}"})
        return target.read_text(encoding="utf-8", errors="replace")

    def aw_pack_write(pack_id: str, path: str, content: str) -> str:
        """Write one file (e.g. tools.py) of a creator pack. Use this, not file_write."""
        root, target, err = _in_pack(pack_id, path)
        if err:
            return json.dumps({"error": err})
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return json.dumps({"written": target.relative_to(root).as_posix(),
                           "bytes": len(content.encode("utf-8"))})

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

    fns = [aw_pack_new, aw_pack_read, aw_pack_write, aw_pack_validate,
           aw_pack_on, aw_pack_off, aw_pack_active]
    for fn in fns:
        reg.register(fn)
    set_runtime_gates(agent.name, runtime_gates(agent.name) | set(GATED))
    return [fn.__name__ for fn in fns]
