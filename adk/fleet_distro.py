"""Which WSL distro hosts the AitherOS podman fleet (awdk's copy of the rule).

awdk ships to strangers and must not import the AitherOS tree, so this is a
stdlib-only twin of ``AitherOS/lib/core/fleet_distro.py``; the monorepo test
``AitherOS/dev/tests/test_fleet_distro_resolvers.py`` runs both against the same
fixtures so they cannot drift. Order:

1. env ``AITHER_FLEET_DISTRO``, then ``AITHER_WSL_DISTRO``, ``FLEET_DISTRO``,
   ``AWDESK_FLEET_DISTRO`` (first non-empty wins);
2. ``fleet_distro:`` of node ``debian-fleet`` in ``AitherOS/config/nodes.yaml``
   (env ``AITHER_NODES_YAML``; else the monorepo this file sits in; else
   ``$AITHEROS_ROOT``). A pip-installed copy with neither falls through to 3,
   which is the right answer for it -- no checkout path is baked in;
3. ``awnix``.

TRANSPORT -- how to reach the fleet host (same rule as the lib resolver):
env ``AITHER_FLEET_TRANSPORT`` (``wsl[:d]`` | ``local`` | ``ssh:<user@host>`` |
``machine[:m]``; ``auto``/empty falls through) > nodes.yaml ``fleet_transport`` >
platform default (win32 -> wsl, linux+systemd+podman -> local, darwin ->
``machine:podman-machine-default``, else :class:`FleetHostUnresolved`).
``fleet_argv`` / ``fleet_script_argv`` / ``fleet_podman_argv`` build the argv;
``host_roots`` gives the host-side paths for the host's layout.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

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


# ─── fleet-host TRANSPORT (twin of lib/core/fleet_distro.py) ─────────────────

TRANSPORT_ENV = "AITHER_FLEET_TRANSPORT"
TRANSPORT_KINDS: Tuple[str, ...] = ("wsl", "local", "ssh", "machine")
DEFAULT_PODMAN_MACHINE = "podman-machine-default"
ROOT_KEYS: Tuple[str, ...] = ("deploy_root", "library_root", "hf_cache", "auth_file")
DEFAULT_AUTH_FILE = "/etc/aither/ghcr-auth.json"
WSL_INTEROP = "/proc/sys/fs/binfmt_misc/WSLInterop"
FLEET_HOST_MARKER = "/etc/aither/awnix-migration.env"
FLEET_HOST_UNIT = "aither-attach-fleet-data.service"
_SSH_TARGET_RE = re.compile(r"^(?:[A-Za-z0-9._-]{1,64}@)?[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")
_MACHINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_USER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_ROOT_RE = re.compile(r"^/[A-Za-z0-9._/@+-]{0,255}$")
_ROOT_DEFAULTS: Dict[str, Dict[str, str]] = {
    "wsl": {"deploy_root": "/mnt/c/AitherOS-Fresh",
            "library_root": "/mnt/c/AitherOS-Data/Library",
            "hf_cache": "", "auth_file": DEFAULT_AUTH_FILE},
    "linux": {"deploy_root": "/var/lib/aither/src",
              "library_root": "/var/lib/aither/Library",
              "hf_cache": "/var/lib/aither/hf-cache", "auth_file": DEFAULT_AUTH_FILE},
    "machine": {"deploy_root": "/Users/{user}/AitherOS-Fresh",
                "library_root": "/Users/{user}/AitherOS-Data/Library",
                "hf_cache": "/Users/{user}/.cache/huggingface",
                "auth_file": DEFAULT_AUTH_FILE},
}


class FleetHostUnresolved(RuntimeError):
    """The fleet host cannot be judged (exit 2)."""

    exit_code = 2


class FleetHost(NamedTuple):
    """``kind`` wsl|local|ssh|machine, ``target``, ``distro`` (wsl only), ``source``."""

    kind: str
    target: str
    distro: str
    source: str

    @property
    def spec(self) -> str:
        return f"{self.kind}:{self.target}" if self.target else self.kind


def parse_transport(spec: str, distro: str) -> Tuple[str, str, str]:
    """``(kind, target, distro)``; raises ValueError on anything invalid."""
    kind, _, target = spec.strip().partition(":")
    kind = kind.strip().lower()
    target = target.strip()
    if kind == "wsl":
        d = target or distro
        if not _NAME_RE.match(d):
            raise ValueError(f"invalid wsl distro in {spec!r}")
        return "wsl", d, d
    if kind == "local":
        if target:
            raise ValueError(f"'local' takes no target: {spec!r}")
        return "local", "", ""
    if kind == "ssh":
        if not _SSH_TARGET_RE.match(target):
            raise ValueError(f"invalid ssh target in {spec!r}")
        return "ssh", target, ""
    if kind == "machine":
        m = target or DEFAULT_PODMAN_MACHINE
        if not _MACHINE_RE.match(m):
            raise ValueError(f"invalid podman machine name in {spec!r}")
        return "machine", m, ""
    raise ValueError(f"unknown fleet transport {spec!r}")


def _node_lookup(lines: Sequence[str], dotted: str) -> Optional[str]:
    path = [FLEET_NODE_ID] + dotted.split(".")
    i, n = 0, len(lines)
    parent = -1
    for depth, seg in enumerate(path):
        key_re = re.compile(r"^(\s*)" + re.escape(seg) + r":(?:\s+(.*)|\s*)$")
        child: Optional[int] = None
        found = False
        while i < n:
            line = lines[i]
            i += 1
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            ind = len(line) - len(line.lstrip())
            if depth > 0:
                if ind <= parent:
                    return None
                if child is None:
                    child = ind
                if ind != child:
                    continue
            m = key_re.match(line)
            if not m:
                continue
            val = (m.group(2) or "").strip()
            if depth == len(path) - 1:
                v = _clean(val) if val and not val.startswith("#") else ""
                return v or None
            if val and not val.startswith("#"):
                return None
            parent = ind
            found = True
            break
        if not found:
            return None
    return None


def _nodes_file(env: Mapping[str, str]) -> Optional[Path]:
    for p in _nodes_candidates(env):
        if p.is_file():
            return p
    return None


def read_nodes_value(dotted: str, env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """``nodes.<debian-fleet>.<dotted>`` from the first nodes.yaml found, or None."""
    p = _nodes_file(os.environ if env is None else env)
    if p is None:
        return None
    try:
        return _node_lookup(p.read_text(encoding="utf-8-sig").splitlines(), dotted)
    except OSError:
        return None


def _linux_local_ok() -> bool:
    return os.path.isdir("/run/systemd/system") and shutil.which("podman") is not None


def resolve_fleet_host(env: Optional[Mapping[str, str]] = None, platform: Optional[str] = None,
                       linux_local_ok: Optional[Callable[[], bool]] = None) -> FleetHost:
    """env AITHER_FLEET_TRANSPORT > nodes.yaml fleet_transport > platform default."""
    e = os.environ if env is None else env
    distro = resolve_fleet_distro(e)[0]
    for value, source in (((e.get(TRANSPORT_ENV) or "").strip(), f"env:{TRANSPORT_ENV}"),
                          ((read_nodes_value("fleet_transport", e) or "").strip(),
                           "nodes.yaml")):
        if not value or value.lower() == "auto":
            continue
        try:
            kind, target, d = parse_transport(value, distro)
        except ValueError as exc:
            raise FleetHostUnresolved(f"{source}: {exc}") from None
        return FleetHost(kind, target, d, source)
    plat = platform or sys.platform
    src = f"platform:{plat}"
    if plat.startswith("win"):
        return FleetHost("wsl", distro, distro, src)
    if plat.startswith("linux"):
        if (linux_local_ok or _linux_local_ok)():
            return FleetHost("local", "", "", src)
        raise FleetHostUnresolved("linux without systemd+podman: set AITHER_FLEET_TRANSPORT")
    if plat == "darwin":
        return FleetHost("machine", DEFAULT_PODMAN_MACHINE, "", src)
    raise FleetHostUnresolved(f"no fleet transport default for platform {plat!r}")


def _become(user: Optional[str], login: str) -> List[str]:
    if not user or user == login:
        return []
    return ["sudo", "-n"] if user == "root" else ["sudo", "-n", "-u", user]


def fleet_argv(*cmd: str, user: Optional[str] = "root",
               host: Optional[FleetHost] = None) -> List[str]:
    """argv that runs ``cmd`` as ``user`` on the fleet host (see the lib resolver)."""
    h = host or resolve_fleet_host()
    if h.kind == "wsl":
        argv = ["wsl.exe", "-d", h.target]
        if user:
            argv += ["-u", user]
        return argv + ["--exec", *cmd]
    if h.kind == "local":
        geteuid = getattr(os, "geteuid", None)
        return _become(user, "root" if not geteuid or geteuid() == 0 else "") + list(cmd)
    if h.kind == "ssh":
        login = h.target.split("@", 1)[0] if "@" in h.target else ""
        remote = _become(user, login) + list(cmd)
        return ["ssh", "-o", "BatchMode=yes", h.target, "--", shlex.join(remote)]
    if h.kind == "machine":
        return ["podman", "machine", "ssh", h.target, "--",
                shlex.join(_become(user, "") + list(cmd))]
    raise FleetHostUnresolved(f"unknown transport kind {h.kind!r}")


def fleet_script_argv(*script_args: str, user: Optional[str] = "root", shell: str = "bash",
                      host: Optional[FleetHost] = None) -> List[str]:
    """argv that runs a script fed on STDIN on the fleet host."""
    cmd = [shell, "-s"] + (["--", *script_args] if script_args else [])
    return fleet_argv(*cmd, user=user, host=host)


def fleet_podman_argv(*args: str, host: Optional[FleetHost] = None) -> List[str]:
    """argv for root ``podman <args>`` on the fleet host."""
    return fleet_argv("podman", *args, user="root", host=host)


def host_layout(host: FleetHost, under_wsl: Optional[bool] = None) -> str:
    """``wsl`` | ``linux`` | ``machine``."""
    if host.kind == "wsl":
        return "wsl"
    if host.kind == "machine":
        return "machine"
    if host.kind == "local" and (os.path.exists(WSL_INTEROP) if under_wsl is None else under_wsl):
        return "wsl"
    return "linux"


def host_roots(host: Optional[FleetHost] = None, env: Optional[Mapping[str, str]] = None,
               under_wsl: Optional[bool] = None) -> Dict[str, str]:
    """Host-side deploy_root/library_root/hf_cache/auth_file ("" = not declared)."""
    e = os.environ if env is None else env
    h = host or resolve_fleet_host(e)
    layout = host_layout(h, under_wsl)
    user = (e.get("USER") or e.get("LOGNAME") or "").strip()
    out: Dict[str, str] = {}
    for key in ROOT_KEYS:
        v = read_nodes_value(f"host_roots.{layout}.{key}", e)
        if v and _ROOT_RE.match(v):
            out[key] = v
            continue
        d = _ROOT_DEFAULTS[layout][key]
        if "{user}" in d:
            d = d.format(user=user) if _USER_RE.match(user) else ""
        out[key] = d
    return out


def is_fleet_host() -> bool:
    """THIS machine is the fleet host (marker file or the data-disk unit enabled)."""
    if os.path.exists(FLEET_HOST_MARKER):
        return True
    if shutil.which("systemctl") is None:
        return False
    try:
        r = subprocess.run(["systemctl", "is-enabled", FLEET_HOST_UNIT],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.stdout.strip() == "enabled"


if __name__ == "__main__":  # pragma: no cover - parity-test CLI
    import json

    if "--host" in sys.argv:
        try:
            _h = resolve_fleet_host()
        except FleetHostUnresolved as _exc:
            print(f"UNRESOLVED {_exc}")
            sys.exit(2)
        print(json.dumps({"kind": _h.kind, "target": _h.target, "source": _h.source,
                          "roots": host_roots(_h)}) if "--json" in sys.argv else _h.spec)
        sys.exit(0)
    n, s = resolve_fleet_distro()
    print(json.dumps({"name": n, "source": s}) if "--json" in sys.argv else n)
