"""adk harness parts -- which parts of the composed harness this machine has.

The Aither World registry (ecosystem.yaml) names ONE umbrella for the kit, the shell
and the bricks an agent works with, and lists the bricks it composes. That block is
carried in the vendored ``adk/data/family.json`` (``harness``), so this command needs
no network and no checkout.

What it does, and all it does: for each composed brick it reports

* ``installed`` with the version -- a pip distribution found by importlib.metadata, or
  an npm-installed command found on PATH that answers ``--version``;
* ``missing`` with the brick's install line;
* ``unknown`` when the install line is neither ``pip install`` nor ``npm i -g`` (a git
  clone, a container build): that cannot be checked from Python, and it is NOT counted
  as installed.

It installs nothing and starts nothing. There is no single install for the umbrella
today; the registry's ``today`` and ``planned`` lines are printed with the table so the
output never reads as more than it is.

Exit 0 when nothing is missing, 1 when a part is missing, 2 when the family index does
not carry the harness block. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

from adk import brick_family

_PIP = re.compile(r"^\s*pip install\s+(\S+)")
_NPM = re.compile(r"^\s*npm (?:i|install)\s+(?:-g\s+)?(\S+)")
_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?[0-9A-Za-z.+\-]*")


class HarnessDataMissingError(RuntimeError):
    """family.json has no usable ``harness`` block."""


def harness(path=None) -> Dict[str, Any]:
    """The umbrella block. Raises rather than inventing a name."""
    doc = brick_family.load(path)
    block = doc.get("harness")
    if not isinstance(block, dict) or not block.get("name") \
            or not isinstance(block.get("composes"), list) or not block["composes"]:
        raise HarnessDataMissingError(
            "the family index carries no `harness` block (name + composes). It is "
            "written by `gen_ecosystem.py --emit` from ecosystem.yaml; this awdk is "
            "older than the umbrella, or the index was edited by hand.")
    return block


def _pip_probe(dist: str) -> Optional[Dict[str, Any]]:
    from importlib import metadata as md
    try:
        d = md.distribution(dist)
    except md.PackageNotFoundError:
        return None
    editable = False
    try:
        raw = d.read_text("direct_url.json")
        if raw:
            editable = bool(json.loads(raw).get("dir_info", {}).get("editable"))
    except (OSError, ValueError):
        editable = False       # no readable direct_url.json: a plain wheel install
    return {"version": d.version, "editable": editable}


def _command_probe(command: str, which=shutil.which, run=subprocess.run
                   ) -> Optional[Dict[str, Any]]:
    """An npm-installed CLI: on PATH, and what ``--version`` prints. A command that is
    on PATH but will not answer is reported installed with version None, never with a
    guessed one."""
    exe = which(command)
    if not exe:
        return None
    version = None
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        res = run([exe, "--version"], capture_output=True, text=True, timeout=15,
                  stdin=subprocess.DEVNULL, creationflags=flags)
        m = _VERSION.search((res.stdout or "") + " " + (res.stderr or ""))
        version = m.group(0) if m else None
    except (OSError, subprocess.SubprocessError, ValueError):
        version = None
    return {"version": version, "editable": False, "path": exe}


def probe(brick: Dict[str, Any], pip_probe=_pip_probe,
          command_probe=_command_probe) -> Dict[str, Any]:
    """One composed brick -> {id, state, version, via, install, note}."""
    bid = str(brick.get("id"))
    install = str(brick.get("install") or "").strip()
    row: Dict[str, Any] = {"id": bid, "state": "unknown", "version": None,
                           "via": None, "install": install or None, "note": None}
    m = _PIP.match(install)
    if m:
        target = m.group(1)
        # `pip install git+https://.../awpermit` has no dist name in the line; the
        # family's convention is that the distribution is named after the brick.
        dist = bid if target.startswith("git+") else re.split(r"[\[<>=!~]", target)[0]
        got = pip_probe(dist)
        row["via"] = "pip"
        if got is None:
            row["state"] = "missing"
        else:
            row["state"] = "installed"
            row["version"] = got.get("version")
            if got.get("editable"):
                row["note"] = "editable (a source checkout)"
        return row
    if _NPM.match(install):
        got = command_probe(bid)
        row["via"] = "npm"
        if got is None:
            row["state"] = "missing"
            row["note"] = f"no `{bid}` command on PATH"
        else:
            row["state"] = "installed"
            row["version"] = got.get("version")
            if not got.get("version"):
                row["note"] = f"`{bid}` is on PATH but `--version` gave no version"
        return row
    row["note"] = "not a pip or npm install; cannot be checked from here"
    return row


def report(path=None, pip_probe=_pip_probe, command_probe=_command_probe
           ) -> Dict[str, Any]:
    block = harness(path)
    by_id = brick_family.by_id(path)
    parts: List[Dict[str, Any]] = []
    for bid in block["composes"]:
        brick = by_id.get(bid)
        if brick is None:
            parts.append({"id": bid, "state": "unknown", "version": None, "via": None,
                          "install": None,
                          "note": "named by the harness block but absent from the family"})
            continue
        parts.append(probe(brick, pip_probe, command_probe))
    counts = {s: sum(1 for p in parts if p["state"] == s)
              for s in ("installed", "missing", "unknown")}
    return {"name": block["name"], "tagline": block.get("tagline"),
            "status": block.get("status"), "today": block.get("today"),
            "planned": block.get("planned"), "parts": parts, "counts": counts}


def render(rep: Dict[str, Any]) -> str:
    out = [f"{rep['name']} -- {rep.get('tagline') or ''} (status: {rep.get('status')})",
           ""]
    wid = max([len("PART")] + [len(p["id"]) for p in rep["parts"]])
    out.append(f"{'PART'.ljust(wid)}  {'STATE'.ljust(9)}  {'VERSION'.ljust(12)}  DETAIL")
    for p in rep["parts"]:
        detail = ""
        if p["state"] != "installed" and p.get("install"):
            detail = p["install"]
        if p.get("note"):
            detail = f"{detail}  ({p['note']})" if detail else p["note"]
        out.append(f"{p['id'].ljust(wid)}  {p['state'].ljust(9)}  "
                   f"{str(p.get('version') or '-').ljust(12)}  {detail}".rstrip())
    c = rep["counts"]
    out += ["",
            f"{c['installed']} of {len(rep['parts'])} installed, {c['missing']} missing, "
            f"{c['unknown']} not checkable from here.",
            "",
            f"Today: {rep.get('today')}",
            f"Planned, not built: {rep.get('planned')}"]
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="adk harness parts",
        description="Which parts of the composed harness this machine has: installed "
                    "(with version), missing (with the install line), or not checkable.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    try:
        rep = report()
    except (HarnessDataMissingError, brick_family.FamilyDataMissingError) as exc:
        print(f"adk harness parts: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(rep, indent=2) if args.json else render(rep))
    return 1 if rep["counts"]["missing"] else 0


if __name__ == "__main__":
    sys.exit(main())
