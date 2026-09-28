"""Which WSL distro hosts the AitherOS podman fleet (awdk's copy of the rule).

awdk ships to strangers and must not import the AitherOS tree, so this is a
stdlib-only twin of ``AitherOS/lib/core/fleet_distro.py``; the monorepo test
``AitherOS/dev/tests/test_fleet_distro_resolvers.py`` runs both against the same
fixtures so they cannot drift. Order:

1. env ``AITHER_FLEET_DISTRO``, then ``AITHER_WSL_DISTRO``, ``FLEET_DISTRO``,
   ``AWDESK_FLEET_DISTRO`` (first non-empty wins);
2. ``fleet_distro:`` of node ``debian-fleet`` in ``AitherOS/config/nodes.yaml``
   (env ``AITHER_NODES_YAML``; else the monorepo this file sits in; else
   ``$AITHEROS_ROOT``; else ``C:/AitherOS-Fresh``);
3. ``awnix``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Mapping, Optional, Tuple

DEFAULT_FLEET_DISTRO = "awnix"
FLEET_NODE_ID = "debian-fleet"
ENV_VARS: Tuple[str, ...] = (
    "AITHER_FLEET_DISTRO",
    "AITHER_WSL_DISTRO",
    "FLEET_DISTRO",
    "AWDESK_FLEET_DISTRO",
)
_NODE_RE = re.compile(r"^(\s*)" + re.escape(FLEET_NODE_ID) + r":\s*(#.*)?$")
_KEY_RE = re.compile(r"^\s*fleet_distro:\s*(.*)$")
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _nodes_candidates(env: Mapping[str, str]) -> List[Path]:
    out: List[Path] = []
    if (env.get("AITHER_NODES_YAML") or "").strip():
        return [Path(env["AITHER_NODES_YAML"].strip())]
    out.append(Path(__file__).resolve().parents[2] / "AitherOS" / "config" / "nodes.yaml")
    root = (env.get("AITHEROS_ROOT") or "").strip()
    if root:
        out.append(Path(root) / "AitherOS" / "config" / "nodes.yaml")
    out.append(Path("C:/AitherOS-Fresh/AitherOS/config/nodes.yaml"))
    return out


def _clean(value: str) -> str:
    v = value.strip()
    if v[:1] in ("'", '"'):
        end = v.find(v[0], 1)
        return v[1:end] if end > 0 else v[1:]
    return v.split(" #", 1)[0].split("\t#", 1)[0].strip()


def read_nodes_fleet_distro(path: Path) -> Optional[str]:
    """``nodes.<debian-fleet>.fleet_distro`` from one nodes.yaml, or None."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return None
    indent: Optional[int] = None
    for line in lines:
        if indent is None:
            m = _NODE_RE.match(line)
            if m:
                indent = len(m.group(1))
            continue
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= indent:
            return None
        k = _KEY_RE.match(line)
        if k:
            v = _clean(k.group(1))
            return v if _NAME_RE.match(v) else None
    return None


def resolve_fleet_distro(env: Optional[Mapping[str, str]] = None) -> Tuple[str, str]:
    """``(name, source)`` -- source is ``env:<VAR>``, ``nodes.yaml`` or ``default``."""
    e = os.environ if env is None else env
    for var in ENV_VARS:
        v = (e.get(var) or "").strip()
        if v:
            return v, f"env:{var}"
    for p in _nodes_candidates(e):
        if p.is_file():
            n = read_nodes_fleet_distro(p)
            if n:
                return n, "nodes.yaml"
            break
    return DEFAULT_FLEET_DISTRO, "default"


def fleet_distro() -> str:
    """The fleet distro name."""
    return resolve_fleet_distro()[0]


def wsl_argv(*cmd: str, user: Optional[str] = "root") -> List[str]:
    """``["wsl", "-d", <distro>, "-u", user, "--", *cmd]``."""
    argv = ["wsl", "-d", fleet_distro()]
    if user:
        argv += ["-u", user]
    if cmd:
        argv += ["--", *cmd]
    return argv


if __name__ == "__main__":  # pragma: no cover - parity-test CLI
    import json
    import sys

    n, s = resolve_fleet_distro()
    print(json.dumps({"name": n, "source": s}) if "--json" in sys.argv else n)
