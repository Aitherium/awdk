"""Vision Plugin for AitherShell (product layer 6)
=================================================

The shell door onto the ``awscreen`` pixel capabilities: read a screen into a
structured ScreenState, diff two screens element-by-element, and paint a manifest to
a deterministic PNG. Every verb DELEGATES to the ``awscreen`` package (no rival
logic lives here); if the package is not importable the verb says so and exits
cleanly instead of raising a traceback into the shell.

Usage:
    /vision state IMAGE [--proposer auto] [--no-inspect] [--json] [--out STATE.json]
    /vision compare A B [--proposer blobs] [--move-px N] [--json]
    /vision paint MANIFEST.json [--out OUT.png]

    IMAGE / A / B are PNG or JPEG paths. ``state`` reads one frame, ``compare`` diffs
    two frames (added / removed / moved / unchanged with pixel deltas), ``paint``
    renders a JSON paint manifest to a PNG.

Aliases: /vscreen
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from adk.shell.plugins import SlashCommand as _SlashCommand
except ImportError:  # loaded outside the ADK package (tests import by path)
    class _SlashCommand:  # type: ignore[no-redef]
        """Minimal stand-in so the module loads and its verbs stay unit-testable."""

        def __init__(self, name: str = "", description: str = "",
                     aliases: Optional[List[str]] = None, **_kw: Any) -> None:
            self.name = name
            self.description = description
            self.aliases = list(aliases or [])


def _package_paths() -> List[str]:
    """Where a source checkout keeps awscreen/awvision, best effort."""
    out: List[str] = []
    env = os.environ.get("AITHER_AWSCREEN_PATH", "").strip()
    if env:
        out.append(env)
    here = Path(__file__).resolve()
    # repo/awdk/adk/shell/plugins/builtins/vision.py -> repo root at parents[5]
    try:
        root = here.parents[5]
    except IndexError:
        root = None
    if root is not None and (root / "AitherOS" / "packages").is_dir():
        out.append(str(root / "AitherOS" / "packages" / "awscreen"))
        out.append(str(root / "AitherOS" / "packages" / "awvision"))
    return out


def _load(module: str) -> Any:
    """Import ``awscreen.<module>``, adding a source checkout to sys.path if needed."""
    try:
        return importlib.import_module(f"awscreen.{module}")
    except ImportError:
        for cand in reversed(_package_paths()):
            if Path(cand).is_dir() and cand not in sys.path:
                sys.path.insert(0, cand)
        return importlib.import_module(f"awscreen.{module}")


def _parse(args: List[str]) -> Dict[str, Any]:
    """Split ``[verb, positionals..., --flags]`` without argparse's SystemExit."""
    opts: Dict[str, Any] = {"positionals": [], "json": False, "inspect": True,
                            "out": "", "proposer": "", "move_px": 3.0}
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--json":
            opts["json"] = True
        elif token == "--no-inspect":
            opts["inspect"] = False
        elif token in ("--out", "--proposer", "--move-px"):
            if i + 1 >= len(args):
                raise ValueError(f"{token} needs a value")
            value = args[i + 1]
            if token == "--out":
                opts["out"] = value
            elif token == "--proposer":
                opts["proposer"] = value
            else:
                opts["move_px"] = float(value)
            i += 1
        elif token.startswith("--"):
            raise ValueError(f"unknown flag {token}")
        else:
            opts["positionals"].append(token)
        i += 1
    return opts


def run_state(image: str, *, proposer: str = "auto", inspect: bool = True) -> Dict[str, Any]:
    """ScreenState dict for one frame (delegates to awscreen.state)."""
    state_mod = _load("state")
    name = str(proposer or "auto").strip().lower()
    if name in ("", "auto", "default"):
        chain, explicit = ("omniparser", "ocr", "blobs", "grid"), False
    else:
        chain, explicit = (name,), True
    state = state_mod.build_screen_state(str(Path(image).expanduser()),
                                         proposers=chain, explicit_proposer=explicit,
                                         inspect=bool(inspect))
    return state.to_dict()


def run_compare(path_a: str, path_b: str, *, proposer: str = "blobs",
                move_px: float = 3.0) -> Dict[str, Any]:
    """Element-level diff of two frames (delegates to awscreen.compare)."""
    compare_mod = _load("compare")
    return compare_mod.compare_images(str(Path(path_a).expanduser()),
                                      str(Path(path_b).expanduser()),
                                      proposer=proposer or "blobs", inspect=False,
                                      move_px=float(move_px))


def run_paint(manifest_path: str, *, out: str = "") -> Dict[str, Any]:
    """Render a paint manifest to a PNG file (delegates to awscreen.paint)."""
    paint_mod = _load("paint")
    text = Path(manifest_path).expanduser().read_text(encoding="utf-8")
    manifest = json.loads(text)
    info = paint_mod.describe(manifest)
    data = paint_mod.paint_png_bytes(manifest)
    if out:
        target = Path(out).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        info["out"] = str(target)
    info["png_bytes"] = len(data)
    return info


class VisionPlugin(_SlashCommand):  # type: ignore[misc]
    """``/vision`` — screen state, element diff and deterministic paint."""

    name: str = "vision"
    aliases: List[str] = ["vscreen"]
    description: str = "Screen state, element-level diff, and deterministic pixel paint"
    category: str = "vision"

    def __init__(self) -> None:
        super().__init__(name="vision",
                         description="Screen state, element diff and deterministic paint",
                         aliases=["vscreen"])

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return self.get_help()
        verb = args[0].lower()
        try:
            opts = _parse(args[1:])
            if verb == "state":
                return self._state(opts)
            if verb == "compare":
                return self._compare(opts)
            if verb == "paint":
                return self._paint(opts)
            if verb in ("help", "-h", "--help"):
                return self.get_help()
            return f"unknown verb {verb!r}; try: state | compare | paint | help"
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return f"vision: {exc}"
        except ImportError as exc:
            return ("vision: awscreen is not installed or importable here -- install the "
                    f"package or set AITHER_AWSCREEN_PATH ({exc})")

    def _state(self, opts: Dict[str, Any]) -> str:
        if not opts["positionals"]:
            return "usage: /vision state IMAGE [--no-inspect] [--proposer NAME] [--json]"
        doc = run_state(opts["positionals"][0],
                        proposer=opts["proposer"] or "auto", inspect=opts["inspect"])
        if opts["out"]:
            Path(opts["out"]).expanduser().write_text(
                json.dumps(doc, indent=1), encoding="utf-8")
        if opts["json"]:
            return json.dumps(doc, indent=1)
        els = doc.get("elements") or []
        lines = [f"elements: {len(els)}  proposer: {(doc.get('proposer') or {}).get('ran')}"]
        for el in els:
            lines.append(f"  {el.get('id'):>4} {el.get('role'):<7} {el.get('bbox')} "
                         f"{str(el.get('label') or '')[:40]!r}")
        return "\n".join(lines)

    def _compare(self, opts: Dict[str, Any]) -> str:
        pos = opts["positionals"]
        if len(pos) < 2:
            return "usage: /vision compare A B [--proposer blobs] [--move-px N] [--json]"
        result = run_compare(pos[0], pos[1], proposer=opts["proposer"] or "blobs",
                             move_px=opts["move_px"])
        if opts["json"]:
            return json.dumps(result, indent=1)
        summary = result.get("summary") or {}
        lines = ["changed: " + "  ".join(f"{k}={v}" for k, v in summary.items())]
        for entry in result.get("moved") or []:
            lines.append(f"  moved     {entry.get('label')!r} "
                         f"d=({entry.get('dx')},{entry.get('dy')})")
        for entry in result.get("added") or []:
            lines.append(f"  added     {entry.get('label')!r} {entry.get('bbox')}")
        for entry in result.get("removed") or []:
            lines.append(f"  removed   {entry.get('label')!r} {entry.get('bbox')}")
        return "\n".join(lines)

    def _paint(self, opts: Dict[str, Any]) -> str:
        if not opts["positionals"]:
            return "usage: /vision paint MANIFEST.json [--out OUT.png]"
        info = run_paint(opts["positionals"][0], out=opts["out"])
        lines = [f"painted {info['width']}x{info['height']} ops={info['ops']} "
                 f"bytes={info['png_bytes']} sha256={info['manifest_sha256'][:16]}"]
        if info.get("out"):
            lines.append(f"wrote {info['out']}")
        else:
            lines.append("pass --out FILE.png to save the render")
        return "\n".join(lines)

    def get_help(self) -> str:
        return """👁 **Vision** — screen state, element diff, deterministic paint

| Command | Description |
|---------|-------------|
| `/vision state IMAGE` | Read one frame into a ScreenState (elements + bboxes) |
| `/vision compare A B` | Diff two frames: added / removed / moved / unchanged |
| `/vision paint MANIFEST.json` | Render a paint manifest to a PNG |

Flags: `--json`, `--out FILE`, `--no-inspect`, `--proposer NAME`, `--move-px N`.
All verbs delegate to the `awscreen` package; install it or set `AITHER_AWSCREEN_PATH`.
"""
