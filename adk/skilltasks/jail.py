"""A real jail for the skill-task sandbox: task commands run inside a podman container.

``permits()`` (:func:`adk.skilltasks.terminal.sandbox_policy`) stays the POLICY layer on
top: it refuses what it can see in a command. This module is the JAIL underneath it, for
what the policy cannot see -- a program the model writes and then runs:

* ``--network=none``: no interface but loopback, so a connect() fails whatever the code;
* ``--read-only`` root, the task workspace bind-mounted read-write at ``/work`` and a
  ``tmpfs`` ``/tmp``: nothing outside the workspace is visible, let alone writable (the
  verifier's ``private/`` state and the frozen tests are simply not in the container);
* a non-root user (uid 1000), ``--cap-drop=ALL``, ``no-new-privileges``, CPU, memory and
  pids limits.

WHO OWNS THE WORKSPACE. Under ROOTFUL podman driven by root (Linux), container uid 1000
would be HOST uid 1000 -- a real account on most hosts -- and the workspace is root's
(``mkdtemp``, 0700), so the jail user could not write it and no jail ever started
(measured: all eight real-podman tests failed that way). There the container gets a user
namespace of its own (``--uidmap``/``--gidmap``) on a 65536-id block no host account
lives in, and the harness hands every read-write mount to the jail user's HOST uid
(block + 1000) before each command: root still reads and writes all of it, and no real
account ever owns a task file. ``--userns=auto`` would pick the block itself but needs a
``containers`` entry in ``/etc/subuid``, which this module must not require or write.
The block is random per episode and avoids every ``/etc/subuid``/``/etc/subgid`` range,
every range a live process's user namespace maps (``/proc/<pid>/uid_map``: the running
containers of any engine) and any block whose jail uid is an account. It costs no
storage: measured on overlay with idmapped mounts, five jails on five blocks added five
29 kB container layers and no image layer, and nothing was left after they closed.

That hand-over is a chown BY ROOT in a tree the model wrote, so it is done through
directory file descriptors only (``O_NOFOLLOW``, ``dir_fd``; a regular file is opened
and re-owned through its own descriptor): no path is resolved twice, so a link swapped
in underneath cannot redirect it. It leaves alone what is not the workspace's own:
another filesystem mounted inside it, and a regular file with more than one hard link
(another name of that inode may live outside). It is bounded (:data:`MAX_OWN_ENTRIES`,
:data:`MAX_OWN_DEPTH`) and iterative: a tree past the bound ends the episode with a
:class:`JailBrokenError` instead of a slow walk before every command or a
``RecursionError``. The whole tree is walked each time rather than "what the harness
wrote": the harness writes through several paths (``write``/``patch``, the rollback,
the verifier's restore, a caller writing by path), a missed one would be a file the
jail silently cannot edit, and directory mtimes are too coarse to prune by.

A jail that could NOT be removed (``podman rm -f`` failed and the container still
exists) is fatal for the episode (:class:`JailBrokenError`): something may still run in
the workspace, and neither the hand-over nor the harness may touch it again.

WHAT THE MODEL IS TOLD. A refusal names its precise cause (host uid, id block, mode,
host path, podman's own error) in the HOST log only. The exception text -- which reaches
the model as a tool error and the run report as ``sandbox_why`` -- is generic.

Starting a container costs seconds (measured 10-20 s per ``podman run``/``exec`` on a
busy host), so ONE container serves a whole episode: its
main process is a tiny command server that reads one JSON request per line on stdin and
runs each command with ``bash -c`` inside the jail. The command travels base64-encoded,
so no quoting survives or breaks on the ``wsl.exe`` hop.

Where podman runs:

* Linux: ``podman`` on PATH;
* Windows: a WSL distro that has podman (``wsl -d <distro> -u root --exec podman``; the
  distro is ``ADK_SKILLTASK_JAIL_DISTRO``, default ``Debian``), ONLY when that distro is
  already running -- this module never starts a WSL distro (every distro shares one
  utility VM, and starting another can disturb what already runs there).

The VERIFIER is jailed too (:func:`open_verifier_jail`): ``tests/test.py`` is the task's
own frozen code, but it EXECUTES what the model left in the workspace (a checker it
rewrote, a repository whose ``.git/config`` it controls), so it runs in a second
container with the same confinement -- the workspace at ``/work``, the verifier's
``private/`` at ``/private`` and the frozen tests read-only at ``/tests``. It is a
separate container because the shell jail must never see ``private/`` or the tests.

NOTHING OUTLIVES ITS COMMAND, in either jail: after every command the command server
(pid 1 of the container) kills every other process in it. The host harness works on the
same workspace by name between commands (``read``/``write``/``patch``, the ``try_change``
rollback); a background process the model left running could swap a directory for a
symlink between the harness's path check and its ``open`` and so make the HOST write or
read outside the workspace. With no process left alive there is nobody to race: a
``server & client`` pair still works inside one command, a daemon across commands does
not. A jail whose server stops answering is removed (``podman rm -f``, waited for) before
the harness touches the workspace again.

THE REPLY CHANNEL IS NOT THE COMMAND'S TO WRITE. "The command returned" is what lets the
harness touch the workspace, and the command runs as the same uid as the server, so a
program the model wrote could otherwise open ``/proc/1/fd/1`` (the server's stdout, i.e.
this channel) and print its own reply while it is still alive. Two things stop that:

* the server makes itself NON-DUMPABLE (``prctl(PR_SET_DUMPABLE, 0)``) before it reads a
  request: the kernel then refuses every other unprivileged process -- same uid or not --
  ``/proc/1/fd/*``, ``/proc/1/mem``, ``process_vm_readv`` and ptrace. The server reports
  it in its ready line and a jail that is not sealed is refused (not usable);
* every request carries a fresh random nonce on the server's stdin, which the reply must
  echo. It never reaches the command (not in its argv, environment or inherited fds) and
  the sealed server's memory cannot be read. A reply with the right id and the wrong
  nonce is a forgery: the jail is removed (waited for) and the command fails.

When neither is available -- or the container cannot START here (the jail user cannot
write the workspace, e.g. rootless podman mapping uid 1000 to a sub-uid, or an SELinux
label refusing the mount; the refusal names the owner, the mode and which case it
is) -- the caller falls back to the policy mode, marked
:data:`POLICY_ONLY` ("policy-only, not a jail") and logged as a warning, never silently.
:func:`open_jail` STARTS the container, so that is decided once, before any task command.

Stdlib only. 3.10-compatible.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import queue
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "IDMAP_SIZE",
    "JAIL_UID",
    "MAX_OWN_DEPTH",
    "MAX_OWN_ENTRIES",
    "Jail",
    "JailBrokenError",
    "JailUnavailableError",
    "POLICY_ONLY",
    "JAIL_IMAGE",
    "probe_podman",
    "open_jail",
    "open_verifier_jail",
    "VERIFY_PRIVATE",
    "VERIFY_TESTS",
]

_log = logging.getLogger(__name__)

POLICY_ONLY = "policy-only, not a jail"
JAIL_IMAGE = os.environ.get("ADK_SKILLTASK_JAIL_IMAGE", "localhost/adk-skilltask-jail:1")
#: The image recipe: a small python + git + bash base, a non-root user. Built once, from
#: a base image that is usually already local (python:3.12-alpine, ~50 MB).
JAIL_CONTAINERFILE = (
    "FROM docker.io/library/python:3.12-alpine\n"
    "RUN apk add --no-cache git bash coreutils && adduser -D -u 1000 -h /work task\n"
    "USER 1000:1000\n"
    "WORKDIR /work\n"
)
WSL_DISTRO = os.environ.get("ADK_SKILLTASK_JAIL_DISTRO", "Debian")
LIMITS = ("--cpus", "2", "--memory", "1g", "--pids-limit", "256")
START_TIMEOUT_S = 240.0
CLOSE_WAIT_S = 30.0  # how long a jail gets to exit once its stdin is closed
RM_TIMEOUT_S = 120.0  # removing a jail whose server stopped answering
#: Where the verifier jail sees the verifier's private state and the frozen tests.
VERIFY_PRIVATE = "/private"
VERIFY_TESTS = "/tests"

#: The in-container command server (runs as the container's main process).
_SERVER = r"""
import base64, json, os, signal, subprocess, sys
def seal():  # non-dumpable: no command (same uid) can open /proc/<this>/fd/1, mem, or ptrace
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        z = ctypes.c_ulong(0)
        libc.prctl(4, z, z, z, z)  # PR_SET_DUMPABLE, 0
        return libc.prctl(3, z, z, z, z) == 0  # PR_GET_DUMPABLE: the kernel's own answer
    except Exception:
        return False
rw = [p for p in os.environ.get("ADK_JAIL_RW", "/work").split(":") if p]
sys.stdout.write(json.dumps({"ready": 1, "uid": os.getuid(), "nd": seal(),
                             "w": all(os.access(p, os.W_OK) for p in rw)}) + "\n")
sys.stdout.flush()
def reap(req):  # nothing a command started outlives it (shell jail and verifier jail)
    if req.get("k") and os.getpid() == 1:
        try:
            os.kill(-1, signal.SIGKILL)  # every process but pid 1 (this server)
        except OSError:
            pass
for line in sys.stdin:
    try:
        req = json.loads(line)
    except ValueError:
        continue
    cmd = base64.b64decode(req.get("c", "")).decode("utf-8", "replace")
    t = float(req.get("t", 60))
    rc, out, err, to = 127, b"", b"", False
    try:
        p = subprocess.Popen(["bash", "-c", cmd], cwd=req.get("d") or "/work",
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            out, err = p.communicate(timeout=t)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            to = True
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            reap(req)  # a daemon that left the group still holds the pipes
            out, err = p.communicate()
            rc = -9
    except OSError as exc:
        err = str(exc).encode()
    reap(req)
    sys.stdout.write(json.dumps({"id": req.get("id"), "n": req.get("n"), "rc": rc, "to": to,
                                 "o": base64.b64encode(out[-65536:]).decode(),
                                 "e": base64.b64encode(err[-65536:]).decode()}) + "\n")
    sys.stdout.flush()
"""


class JailUnavailableError(RuntimeError):
    """podman (or the jail image) is not usable on this host. Its text reaches the model
    and the run report: it never carries a host path, uid, id block or mode (those are
    logged where it is raised)."""


class JailBrokenError(JailUnavailableError):
    """The jail failed in a way the EPISODE cannot survive: a container that could not be
    removed (a command may still run in the workspace), or a workspace too large or too
    deep to hand to the jail user. The harness must not touch the workspace again."""


JAIL_UID = 1000  # the ``--user`` of both jails, INSIDE the container
IDMAP_SIZE = 65536  # ids in the jail's own user namespace (rootful podman only)
_IDMAP_FLOOR = 0x40000000  # blocks start here: far above accounts and /etc/subuid ranges
_IDMAP_BLOCKS = 8192  # ... and stay below 2**31
#: The hand-over walk stops here and the episode ends (measured on tmpfs: about 0.15 s per
#: 50 000 entries that already belong to the jail user).
MAX_OWN_ENTRIES = 200000
MAX_OWN_DEPTH = 64  # one open directory descriptor per level


def rootful_here() -> bool:
    """This process is root on a POSIX host, so the podman it runs is rootful and a
    container uid is the SAME host uid unless the jail maps it away."""
    return os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0


def _reserved_id_ranges(
    subid_files: Sequence[str] = ("/etc/subuid", "/etc/subgid"), proc: str = "/proc"
) -> List[Tuple[int, int]]:
    """``(first host id, count)`` of every range something else may use: each
    ``/etc/subuid``/``/etc/subgid`` line, and each range a live process's user namespace
    maps -- the running containers of any engine, read from ``/proc`` (measured: 30 ms
    for 1400 processes, against one ``podman inspect`` per container)."""
    out: List[Tuple[int, int]] = []
    for name in subid_files:
        try:
            with open(name, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue  # no such file: nothing is delegated there
        for line in lines:
            parts = line.split("#", 1)[0].strip().split(":")
            if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                out.append((int(parts[1]), int(parts[2])))
    try:
        pids = [n for n in os.listdir(proc) if n.isdigit()]
    except OSError:
        pids = []
    seen = set()
    for pid in pids:
        for which in ("uid_map", "gid_map"):
            try:
                with open(os.path.join(proc, pid, which), encoding="ascii", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue  # the process ended meanwhile
            if text in seen:
                continue
            seen.add(text)
            for line in text.splitlines():
                f = line.split()
                if len(f) != 3 or not all(x.isdigit() for x in f):
                    continue
                inside, outside, count = int(f[0]), int(f[1]), int(f[2])
                if inside == 0 and outside == 0 and count >= 0xFFFFFFFF:
                    continue  # the initial namespace maps everything onto itself
                out.append((outside, count))
    return out


def pick_idmap_base() -> int:
    """The first host id of a :data:`IDMAP_SIZE` block for one episode's jails: random
    (two episodes do not share an owner), outside every reserved range
    (:func:`_reserved_id_ranges`) and never one whose jail uid is an account."""
    import pwd  # POSIX only; only reached when rootful_here()

    reserved = _reserved_id_ranges()
    for _ in range(64):
        base = _IDMAP_FLOOR + secrets.randbelow(_IDMAP_BLOCKS) * IDMAP_SIZE
        if any(base < first + count and first < base + IDMAP_SIZE for first, count in reserved):
            continue
        try:
            pwd.getpwuid(base + JAIL_UID)
        except KeyError:
            return base
    raise JailUnavailableError("no free host id block for the jail user")


def _give_to(root: Path, uid: int) -> None:
    """Hand the tree at ``root`` to host ``uid``, as root, without ever resolving a path
    twice: every directory is opened ``O_NOFOLLOW`` relative to its parent's descriptor
    and re-owned through its own, a regular file likewise, anything else by name relative
    to the directory descriptor without following it. A symlink the model planted is
    re-owned itself, never its target, also if the tree changes during the walk.

    Left alone: what lives on another filesystem (a mount inside the workspace) and a
    regular file with more than one hard link. Iterative and bounded: past
    :data:`MAX_OWN_ENTRIES` or :data:`MAX_OWN_DEPTH` it raises :class:`JailBrokenError`."""
    if os.name == "nt" or not hasattr(os, "fchown"):
        return  # Windows: podman sees the drive through the distro's own mount
    dflags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    fflags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        top = os.open(str(root), dflags)
    except OSError as exc:  # not there, or a link: the server's "w" check reports it
        _log.debug("could not open %s to hand it to uid %d: %s", root, uid, exc)
        return
    seen = 0
    stack: List[Tuple[int, List[str]]] = []

    def enter(fd: int, dev: int) -> List[str]:
        """Re-own the directory ``fd`` and what is in it; the names of its sub-directories."""
        nonlocal seen
        if os.fstat(fd).st_uid != uid:
            os.fchown(fd, uid, uid)
        with os.scandir(fd) as it:
            names = [entry.name for entry in it]
        seen += len(names)
        if seen > MAX_OWN_ENTRIES:
            raise JailBrokenError(
                "the workspace holds more than %d entries; it is too large to hand to "
                "the jail user before every command" % MAX_OWN_ENTRIES
            )
        subdirs: List[str] = []
        for name in names:
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if st.st_dev != dev:
                    continue  # another filesystem: not the workspace's own
                if stat.S_ISDIR(st.st_mode):
                    subdirs.append(name)
                elif st.st_uid == uid:
                    continue
                elif stat.S_ISREG(st.st_mode):
                    ffd = os.open(name, fflags, dir_fd=fd)
                    try:  # judge and re-own the SAME inode: the one this descriptor holds
                        fst = os.fstat(ffd)
                        if stat.S_ISREG(fst.st_mode) and fst.st_dev == dev and fst.st_nlink == 1:
                            os.fchown(ffd, uid, uid)
                    finally:
                        os.close(ffd)
                else:
                    os.chown(name, uid, uid, dir_fd=fd, follow_symlinks=False)
            except OSError as exc:
                # gone meanwhile, swapped for a link, or refused: the server's "w" check
                # is what reports a workspace the jail cannot write
                _log.debug("could not hand %r to uid %d: %s", name, uid, exc)
        return subdirs

    stack.append((top, []))  # on the stack BEFORE anything can raise: closed below
    try:
        dev = os.fstat(top).st_dev
        stack[-1][1].extend(enter(top, dev))
        while stack:
            fd, subdirs = stack[-1]
            if not subdirs:
                os.close(fd)
                stack.pop()
                continue
            name = subdirs.pop()
            if len(stack) >= MAX_OWN_DEPTH:
                raise JailBrokenError(
                    "the workspace is more than %d directories deep; it is too deep to "
                    "hand to the jail user" % MAX_OWN_DEPTH
                )
            try:
                child = os.open(name, dflags, dir_fd=fd)
            except OSError as exc:  # gone, or swapped for a link: never followed
                _log.debug("could not open %r to hand it to uid %d: %s", name, uid, exc)
                continue
            stack.append((child, []))
            if os.fstat(child).st_dev != dev:  # a mount point: not the workspace's own
                os.close(child)
                stack.pop()
                continue
            stack[-1][1].extend(enter(child, dev))
    finally:
        for fd, _subdirs in stack:
            try:
                os.close(fd)
            except OSError as exc:
                _log.debug("closing a directory descriptor: %s", exc)


def _owner_of(path: Path) -> str:
    try:
        st = os.lstat(str(path))
    except OSError as exc:
        return "unreadable here: %s" % exc
    return "owned by host uid %d, mode %04o" % (st.st_uid, st.st_mode & 0o7777)


def _running_wsl_distros() -> List[str]:
    try:
        raw = subprocess.run(
            ["wsl", "-l", "--running", "-q"], capture_output=True, timeout=20
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    text = raw.decode("utf-16-le", "replace") if b"\x00" in raw else raw.decode("utf-8", "replace")
    return [ln.strip().strip("\x00") for ln in text.splitlines() if ln.strip().strip("\x00")]


def podman_prefix() -> Optional[List[str]]:
    """The argv prefix that runs podman here, or None. Never starts a WSL distro."""
    override = os.environ.get("ADK_SKILLTASK_PODMAN")
    if override:
        return override.split()
    if os.name == "nt":
        if not shutil.which("wsl"):
            return None
        if WSL_DISTRO not in _running_wsl_distros():
            return None
        return ["wsl", "-d", WSL_DISTRO, "-u", "root", "--exec", "podman"]
    pm = shutil.which("podman")
    return [pm] if pm else None


def host_path(p: Path) -> str:
    """``p`` as the podman host sees it (a Windows drive path becomes ``/mnt/<drive>/...``)."""
    if os.name != "nt":
        return str(p)
    w = PureWindowsPath(str(Path(p).resolve()))
    if not w.drive or not w.drive.endswith(":"):
        _log.warning("the workspace %s is not on a drive letter the distro mounts", p)
        raise JailUnavailableError("the workspace is not on a drive letter the distro mounts")
    return "/mnt/%s/%s" % (w.drive[0].lower(), "/".join(w.parts[1:]))


def _pm(prefix: List[str], *args: str, timeout: float = 120.0, stdin: Optional[str] = None) -> Any:
    return subprocess.run(
        prefix + list(args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        input=stdin,
    )


def probe_podman(build: bool = True) -> Tuple[Optional[List[str]], str]:
    """``(prefix, "")`` when podman works here and the jail image exists (building it once
    when it is missing and ``build``), else ``(None, why)``."""
    prefix = podman_prefix()
    if prefix is None:
        if os.name == "nt":
            return None, "no running %r WSL distro with podman (never started here)" % WSL_DISTRO
        return None, "podman is not on PATH"
    try:
        if _pm(prefix, "image", "exists", JAIL_IMAGE, timeout=120).returncode != 0:
            if not build:
                return None, "the jail image %s is missing" % JAIL_IMAGE
            _log.warning("building the skill-task jail image %s (once)", JAIL_IMAGE)
            ctx = "/tmp" if os.name == "nt" else tempfile.mkdtemp(prefix="adk-jail-ctx-")
            r = _pm(
                prefix,
                "build",
                "-t",
                JAIL_IMAGE,
                "-f",
                "-",
                ctx,
                timeout=900,
                stdin=JAIL_CONTAINERFILE,
            )
            if r.returncode != 0:
                _log.warning("building %s failed: %s", JAIL_IMAGE, (r.stderr or r.stdout)[-300:])
                return None, "building the jail image %s failed (see the host log)" % JAIL_IMAGE
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("podman did not answer: %s", exc)
        return None, "podman did not answer (see the host log)"
    return prefix, ""


class Jail:
    """One container per episode; :meth:`run` executes a command inside it."""

    def __init__(
        self,
        workspace: Path,
        prefix: List[str],
        *,
        image: str = JAIL_IMAGE,
        mounts: Sequence[Tuple[Path, str, str]] = (),
        home: str = "/work",
        reap: bool = False,
        idmap_base: Optional[int] = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.prefix = list(prefix)
        self.image = image
        #: extra ``(host path, container path, "rw" | "ro")`` mounts beside the workspace
        self.mounts = [(Path(h), str(c), str(m)) for h, c, m in mounts]
        self.home = home
        #: kill every process a command left behind once it returns. Both jails set it:
        #: a survivor shares the workspace with the host harness and can race its file
        #: operations (a symlink swapped in between the path check and the open)
        self.reap = bool(reap)
        #: first host id of the container's own user namespace (rootful podman run by
        #: root), else None: the engine's default mapping. The verifier jail takes the
        #: shell jail's, so both see the workspace as theirs.
        self.idmap_base: Optional[int] = None
        if idmap_base is not None:
            self.idmap_base = int(idmap_base)
        elif rootful_here():
            self.idmap_base = pick_idmap_base()
        self.name = ""
        #: why this jail ended the episode ("" while it has not): a container that could
        #: not be removed, or a workspace past the hand-over bound. Never cleared.
        self.broken = ""
        self.proc: Optional[subprocess.Popen] = None
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self.starts = 0
        self.commands = 0

    # -- lifecycle ---------------------------------------------------------------------
    def argv(self) -> List[str]:
        """The ``podman run`` argv of the jail (the flags ARE the jail; tests read them)."""
        code = base64.b64encode(_SERVER.encode()).decode()
        extra: List[str] = []
        rw = ["/work"]
        for host, inside, mode in self.mounts:
            extra += ["-v", "%s:%s:%s" % (host_path(host), inside, mode)]
            if mode == "rw":
                rw.append(inside)
        userns: List[str] = []
        if self.idmap_base is not None:
            ids = "0:%d:%d" % (self.idmap_base, IDMAP_SIZE)
            userns = ["--uidmap", ids, "--gidmap", ids]
        return self.prefix + [
            "run", "-i", "--rm", "--name", self.name,
            "--label", "adk.skilltask.jail=1",
            "--network=none",
            "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
            "--cap-drop=ALL",
            "--security-opt", "no-new-privileges",
            *userns,
            "--user", "%d:%d" % (JAIL_UID, JAIL_UID),
            *LIMITS,
            "-v", "%s:/work:rw" % host_path(self.workspace),
            *extra,
            "-w", "/work",
            "-e", "HOME=%s" % self.home, "-e", "TMPDIR=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "PYTHONUTF8=1", "-e", "ADK_JAIL_RW=%s" % ":".join(rw),
            "-e", "GIT_TERMINAL_PROMPT=0", "-e", "GIT_CONFIG_NOSYSTEM=1",
            "-e", "GIT_CONFIG_COUNT=1", "-e", "GIT_CONFIG_KEY_0=safe.directory",
            "-e", "GIT_CONFIG_VALUE_0=*",
            self.image,
            "python3", "-u", "-c", "import base64;exec(base64.b64decode('%s'))" % code,
        ]  # fmt: skip

    def host_uid(self) -> Optional[int]:
        """The HOST uid the jail user is, when this jail maps it (else None)."""
        return None if self.idmap_base is None else self.idmap_base + JAIL_UID

    def _rw_mounts(self) -> List[Path]:
        return [self.workspace] + [h for h, _c, m in self.mounts if m == "rw"]

    def _own(self) -> None:
        """Every read-write mount belongs to the jail user: the workspace starts as the
        harness's, and so does each file the harness writes into it between commands.
        Raises :class:`JailBrokenError` past the walk's bound."""
        uid = self.host_uid()
        if uid is None:
            return
        for path in self._rw_mounts():
            _give_to(path, uid)

    def _unwritable_detail(self) -> str:
        """What was measured, for the HOST log: who owns each mount and which case."""
        what = "; ".join("%s is %s" % (p, _owner_of(p)) for p in self._rw_mounts())
        uid = self.host_uid()
        if uid is not None:
            cause = (
                "the jail user is host uid %d (rootful podman, ids mapped from %d) and "
                "the harness could not hand it the mount (a filesystem that refuses "
                "chown, or one mounted read-only)" % (uid, uid - JAIL_UID)
            )
        elif os.name == "nt":
            cause = (
                "podman runs in the WSL distro and container uid %d has no write access "
                "to that path through the distro's mount" % JAIL_UID
            )
        else:
            cause = (
                "the harness is uid %d, not root, so podman is rootless here: container "
                "uid %d is one of that user's sub-uids, not the owner" % (os.geteuid(), JAIL_UID)
            )
        return "%s -- %s" % (what, cause)

    def _stop(self, kill: bool = False) -> None:
        """:meth:`close`, and the episode ends here if the container may have survived."""
        self.close(kill=kill)
        if self.broken:
            raise JailBrokenError(self.broken)

    def start(self) -> None:
        if self.broken:
            raise JailBrokenError(self.broken)
        self._stop()
        self._own()
        self.name = "adk-jail-" + uuid.uuid4().hex[:12]
        self._lines = queue.Queue()
        self.proc = subprocess.Popen(
            self.argv(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.starts += 1
        threading.Thread(target=self._pump, args=(self.proc, self._lines), daemon=True).start()
        ready = self._next(START_TIMEOUT_S)
        if ready is None or not ready.get("ready"):
            err = ""
            if self.proc is not None and self.proc.poll() is not None and self.proc.stderr:
                err = self.proc.stderr.read()[-300:]
            self._stop()
            # podman's own error names host paths: the log has it, the model does not
            _log.warning("jail %s did not start: %s", self.name, err or "no answer")
            raise JailUnavailableError(
                "the jail container did not start (podman's error is in the host log)"
            )
        if int(ready.get("uid", 0)) == 0:
            self._stop()
            raise JailUnavailableError("the jail runs as root; refusing")
        if ready.get("nd") is not True:
            # its stdout is the reply channel: a server a command of the same uid can
            # write as (/proc/1/fd/1) or read (/proc/1/mem) cannot say "the command ended"
            self._stop()
            raise JailUnavailableError(
                "the jail's command server could not seal itself (PR_SET_DUMPABLE); a "
                "command could forge its replies; refusing"
            )
        if ready.get("w") is False:
            self._stop()
            _log.warning(
                "jail %s: the jail user cannot write the workspace: %s",
                self.name,
                self._unwritable_detail(),
            )
            raise JailUnavailableError(
                "the jail user cannot write the workspace; every task command would fail "
                "(the cause is in the host log)"
            )

    @staticmethod
    def _pump(proc: subprocess.Popen, q: "queue.Queue[Optional[str]]") -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            q.put(line)
        q.put(None)

    def _next(self, timeout: float) -> Optional[Dict[str, Any]]:
        while True:
            try:
                line = self._lines.get(timeout=timeout)
            except queue.Empty:
                return None
            if line is None:
                return None
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except ValueError:
                    continue

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def run(
        self, cmd: str, timeout: float = 60.0, cwd: Optional[str] = None
    ) -> Tuple[int, str, str, bool]:
        """``(returncode, stdout, stderr, timed_out)`` of ``bash -c cmd`` inside the jail
        (in ``cwd``, a path INSIDE the container; default ``/work``). Raises
        :class:`JailBrokenError` when the episode cannot go on (see :attr:`broken`)."""
        if self.broken:
            raise JailBrokenError(self.broken)
        if not self.alive():
            self.start()
        assert self.proc is not None and self.proc.stdin is not None
        try:
            self._own()  # what the harness wrote since the last command is the harness's
        except JailBrokenError as exc:
            self.broken = str(exc)
            _log.error("jail %s: %s; the episode cannot go on", self.name, exc)
            self.close()
            raise
        self.commands += 1
        rid = self.commands
        # the reply must echo this; it goes down the server's stdin only, never to the
        # command, so a program inside the jail cannot print a reply the harness accepts
        nonce = secrets.token_hex(16)
        req: Dict[str, Any] = {
            "id": rid,
            "n": nonce,
            "t": float(timeout),
            "c": base64.b64encode(str(cmd).encode()).decode(),
        }
        if cwd:
            req["d"] = cwd
        if self.reap:
            req["k"] = 1
        try:
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
        except OSError as exc:
            _log.warning("jail %s went away: %s", self.name, exc)
            self._stop()
            return 127, "", "the jail went away; it is restarted for the next command", False
        while True:
            rep = self._next(float(timeout) + 60.0)
            if rep is None:  # the server died (a command killed it) or hung: a fresh jail next
                self._stop()
                return (
                    127,
                    "",
                    "the jail stopped answering; it is restarted for the next command",
                    True,
                )
            if rep.get("id") != rid:
                continue  # a stale reply
            if hmac.compare_digest(str(rep.get("n", "")).encode(), nonce.encode()):
                break
            # the right id without the nonce did not come from the server: the command
            # is forging its own "I ended" and is still alive. Remove the jail (waited
            # for) before the harness touches the workspace.
            _log.warning("jail %s: a forged reply to command %d; removing it", self.name, rid)
            self._stop(kill=True)
            return (
                127,
                "",
                "the command forged a jail reply; the jail was removed and is restarted "
                "for the next command",
                False,
            )
        out = base64.b64decode(rep.get("o", "")).decode("utf-8", "replace")
        err = base64.b64decode(rep.get("e", "")).decode("utf-8", "replace")
        return int(rep.get("rc", 127)), out, err, bool(rep.get("to"))

    def _removed(self) -> bool:
        """``podman rm -f`` the container and KNOW it is gone: rm succeeded, or podman
        says no such container exists. Anything else (rm failed and it still exists,
        podman did not answer) is "not removed"."""
        try:
            rm = subprocess.run(
                self.prefix + ["rm", "-f", self.name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=RM_TIMEOUT_S,
            )
            if rm.returncode == 0:
                return True
            there = subprocess.run(
                self.prefix + ["container", "exists", self.name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=RM_TIMEOUT_S,
            )
            if there.returncode == 1:  # 0 = it exists, 1 = it does not, else podman failed
                return True
            _log.error(
                "podman rm -f %s failed (exit %d, exists: exit %d): %s",
                self.name,
                rm.returncode,
                there.returncode,
                (rm.stderr or b"")[-300:].decode("utf-8", "replace"),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _log.error("podman rm -f %s did not answer: %s", self.name, exc)
        return False

    def close(self, kill: bool = False) -> None:
        """Stop the jail. ``kill``: a command is known to be alive in it, so do not wait
        for the server to exit by itself -- remove the container now. A container that
        could not be removed sets :attr:`broken`: the episode is over."""
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()  # the server's stdin ends: it exits and --rm removes it
            if kill:
                raise subprocess.SubprocessError("not waiting for a hostile jail")
            proc.wait(timeout=CLOSE_WAIT_S)
        except (OSError, subprocess.SubprocessError):
            proc.kill()
            # never leave a jail behind, and KNOW it is gone: killing the podman client
            # does not kill the container, and what still runs in it shares the workspace
            # the host harness (root, by name) is about to touch again
            if self.name and not self._removed():
                self.broken = (
                    "the jail container could not be removed, so a command may still be "
                    "running in the workspace"
                )


def open_jail(workspace: Path, mode: str = "auto") -> Tuple[Optional[Jail], str]:
    """``(jail, "")`` with the jail STARTED, or ``(None, why)``. ``mode``: ``auto`` (podman
    when usable), ``podman`` (required: raise :class:`JailUnavailableError` otherwise),
    ``off``.

    The container is started here, not on the first command: a jail that exists but
    cannot start (unwritable workspace, root, no answer) is "not usable" exactly like a
    missing podman, so ``auto`` falls back to the policy and ``podman`` raises -- instead
    of every later command failing in a run reported as jailed."""
    mode = (mode or "auto").lower()
    if mode in ("off", "policy", "none", "0"):
        return None, "disabled (ADK_SKILLTASK_JAIL=%s)" % mode
    prefix, why = probe_podman()
    if prefix is None:
        if mode == "podman":
            raise JailUnavailableError(why)
        return None, why
    try:
        host_path(workspace)
        jail = Jail(workspace, prefix, reap=True)
        jail.start()
    except JailUnavailableError as exc:
        if mode == "podman":
            raise
        return None, str(exc)
    except (OSError, subprocess.SubprocessError) as exc:  # podman vanished under us
        _log.warning("podman did not start the jail: %s", exc)
        if mode == "podman":
            raise JailUnavailableError("podman did not start the jail") from exc
        return None, "podman did not start the jail (see the host log)"
    return jail, ""


def open_verifier_jail(shell: Jail, private: Path, tests: Path) -> Jail:
    """The STARTED jail the task verifier runs in, beside the shell jail ``shell``: the
    same podman, image and confinement, with the verifier's ``private`` state at
    :data:`VERIFY_PRIVATE` (rw) and the frozen ``tests`` at :data:`VERIFY_TESTS` (ro).
    ``HOME`` is the tmpfs, not the workspace, so a ``~/.gitconfig`` the model wrote is
    not the verifier's. Raises :class:`JailUnavailableError` when it cannot start: the
    caller must then not report the run as jailed, and must never verify on the host
    instead."""
    try:
        jail = Jail(
            shell.workspace,
            shell.prefix,
            image=shell.image,
            mounts=((Path(private), VERIFY_PRIVATE, "rw"), (Path(tests), VERIFY_TESTS, "ro")),
            home="/tmp",
            reap=True,
            idmap_base=shell.idmap_base,
        )
        jail.start()
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("podman did not start the verifier jail: %s", exc)
        raise JailUnavailableError("podman did not start the verifier jail") from exc
    return jail
