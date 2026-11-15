"""One resolver for every add-on: awpack packs, Library packs, awnode extensions, awskills.

The bricks already exist and each owns its own install; this module only FRONTS them, so
every surface (``adk pack``, ``adk skills``, the agent ``install_pack`` tool, awsh
``/packs`` and ``/skills``, the desk Install button) resolves an id the same way and
writes the same record (:mod:`adk.addon_lock`).

Resolution order for a bare id (the first source that has it wins):

1. **awpack** -- first-party agent packs on the awpack shelf (``awpack install``, then
   ``awpack verify``, whose content hash is what gets recorded).
2. **library** -- marketplace tool/agent packs (``adk pack install``'s download path) and
   packs bundled with adk.
3. **awnode-ext** -- local-gateway extensions (``awnode ext install``).

Skills are never guessed from a bare id: ``skill:<name>`` (or ``adk skills install``)
copies one awskills ``.md`` into a harness skills dir. Third-party skills come from the
same two places Hermes Agent reads, fetched as one ``SKILL.md`` and recorded the same way:

- ``hermes:<owner>/<repo>/<skill>`` -- a Hermes skills TAP (``<repo>/skills/<skill>/``).
- ``agentskills:<owner>/<repo>/<path>`` -- any GitHub folder in the agentskills.io layout
  (``<path>/SKILL.md``), or ``agentskills:https://.../SKILL.md``.

Those are ``community`` trust unless the owner is Aitherium: the CLI needs
``--allow-community`` and the agent tool asks the owner first. A ``<kind>:`` prefix
(``awpack:``, ``library:``, ``ext:``, ``skill:``) pins the source; ``community:<listing>``
is a Library community listing.

Bricks are driven through their CLIs with ``--json`` (exit 0 ok, 1 refused, 2 cannot
judge), so a brick upgrade never has to keep a Python API stable for us.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("adk.addons")

__all__ = ["Addon", "Result", "search", "resolve", "install", "remove", "trust_of",
           "skill_targets", "find_skills_shelf", "SOURCES", "install_skill", "remove_skill",
           "install_remote_skill", "remote_skill_url", "is_remote_skill"]

SOURCES = ("awpack", "library", "awnode-ext")
_PREFIX = {"awpack": "awpack", "library": "library", "ext": "awnode-ext",
           "awnode-ext": "awnode-ext", "skill": "skill"}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_BRICK_MODULES = {"awpack": "awpack.cli", "awnode": "awnode.cli"}
_BRICK_TIMEOUT_S = 600
_TRUST_FROM_PROVENANCE = {"first-party": "official", "community": "community"}


@dataclass
class Addon:
    """One installable thing, as a source reports it."""

    kind: str
    id: str
    version: str = ""
    summary: str = ""
    trust: str = "community"
    source: str = ""
    installed: bool = False
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("extra", None)
        return d


@dataclass
class Result:
    """What an install/remove did. ``record`` is the lock row written, if any."""

    ok: bool
    kind: str
    id: str
    detail: str = ""
    record: "dict | None" = None
    next: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── brick plumbing ──────────────────────────────────────────────────────────────────


def _brick(name: str, argv: "list[str]", timeout: int = _BRICK_TIMEOUT_S
           ) -> "tuple[int, str, str]":
    """Run brick *name* with *argv*: (exit code, stdout, stderr). 127 = not installed."""
    exe = shutil.which(name)
    if exe:
        cmd = [exe, *argv]
    else:
        mod = _BRICK_MODULES.get(name, "")
        try:
            import importlib.util
            found = bool(mod) and importlib.util.find_spec(mod.split(".")[0]) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            return 127, "", f"{name} is not installed (pip install {name})"
        cmd = [sys.executable, "-m", mod, *argv]
    kw: "dict[str, Any]" = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",  # noqa: S603
                           errors="replace", timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        return 124, "", f"{name} {' '.join(argv[:2])} timed out after {timeout}s"
    except OSError as exc:
        return 127, "", f"cannot run {name}: {exc}"
    return p.returncode, p.stdout or "", p.stderr or ""


def _json_out(stdout: str) -> dict:
    """The one JSON object a ``--json`` brick prints (last JSON line wins)."""
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    return {}


_CACHE: "dict[str, tuple[float, Any]]" = {}
_CACHE_TTL_S = 60.0


def _cached(key: str, fn):
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL_S:
        return hit[1]
    val = fn()
    _CACHE[key] = (time.monotonic(), val)
    return val


def clear_cache() -> None:
    """Forget cached source listings (after an install, or in tests)."""
    _CACHE.clear()


# ── sources ─────────────────────────────────────────────────────────────────────────


def _awpack_list() -> "tuple[list[Addon], str]":
    rc, out, err = _brick("awpack", ["list", "--json"], timeout=60)
    obj = _json_out(out)
    if rc != 0 or not obj.get("ok"):
        return [], (obj.get("detail") or err.strip() or f"awpack list exited {rc}")
    from adk.addon_lock import lookup
    rows = []
    for p in obj.get("packs") or []:
        pid = str(p.get("id") or "")
        if not pid:
            continue
        rows.append(Addon(kind="awpack", id=pid, version=str(p.get("version") or ""),
                          summary=str(p.get("summary") or ""),
                          trust=_TRUST_FROM_PROVENANCE.get(str(p.get("provenance")), "local"),
                          source="awpack", installed=lookup(pid, "awpack") is not None,
                          extra={"provenance": p.get("provenance")}))
    return rows, ""


def _library_list() -> "tuple[list[Addon], str]":
    from adk import cli
    rows: "dict[str, Addon]" = {}
    try:
        catalog = cli._load_pack_catalog(cli._get_genesis_url())
    except Exception as exc:  # noqa: BLE001 - the local half still answers
        catalog, why = [], f"{type(exc).__name__}: {exc}"
    else:
        why = ""
    local = cli._local_pack_manifests()
    for p in catalog:
        pid = str(p.get("id") or "")
        if pid:
            rows[pid] = Addon(kind="library", id=pid, version=str(p.get("version") or ""),
                              summary=str(p.get("description") or p.get("name") or ""),
                              trust="official", source="catalog", installed=pid in local,
                              extra={"type": p.get("type"), "tier": p.get("tier"),
                                     "tags": list(p.get("tags") or [])})
    installed_root = (Path.home() / ".aitheros" / "packs").resolve()
    for pid, m in local.items():
        if pid in rows:
            continue
        try:
            bundled = installed_root not in Path(m.path).resolve().parents
        except OSError:
            bundled = False
        from adk.addon_lock import lookup
        row = lookup(pid, "library")
        trust = "official" if bundled else (row or {}).get("trust", "local")
        rows[pid] = Addon(kind="library", id=pid, version=m.version or "",
                          summary=m.description or m.name, trust=trust,
                          source="bundled" if bundled else "installed", installed=True,
                          extra={"tags": list(getattr(m, "tags", []) or [])})
    return list(rows.values()), why


def _awnode_list() -> "tuple[list[Addon], str]":
    rc, out, err = _brick("awnode", ["ext", "list", "--json"], timeout=60)
    obj = _json_out(out)
    if rc != 0 or not obj.get("ok"):
        return [], (err.strip().splitlines()[-1] if err.strip() else f"awnode ext list "
                    f"exited {rc}")
    rows = []
    for e in obj.get("extensions") or []:
        eid = str(e.get("id") or "")
        if eid:
            rows.append(Addon(kind="awnode-ext", id=eid, version=str(e.get("version") or ""),
                              summary=str(e.get("description") or e.get("name") or ""),
                              trust="local" if e.get("custom") else "official",
                              source="awnode", installed=str(e.get("status")) in (
                                  "installed", "running", "stopped"),
                              extra={"manifest": e}))
    return rows, ""


_LISTERS = {"awpack": _awpack_list, "library": _library_list, "awnode-ext": _awnode_list}


def _source(kind: str) -> "tuple[list[Addon], str]":
    return _cached(f"list:{kind}", _LISTERS[kind])


def _matches(a: Addon, q: str) -> bool:
    tags = [str(t) for t in a.extra.get("tags", []) or []]
    return any(q in h.lower() for h in (a.id, a.summary, *tags))


def search(query: str, kinds: "tuple[str, ...]" = SOURCES) -> "tuple[list[Addon], dict]":
    """Every add-on whose id/summary/tags contain *query*, across *kinds*.

    Returns (hits, {kind: why-unavailable}) -- a source that could not answer is named,
    never silently empty.
    """
    q = (query or "").strip().lower()
    hits: "list[Addon]" = []
    down: "dict[str, str]" = {}
    for kind in kinds:
        if kind == "skill":
            rows, why = _skills_list()
        else:
            rows, why = _source(kind)
        if why and not rows:
            down[kind] = why
        hits.extend(a for a in rows if not q or _matches(a, q))
    return hits, down


def _split(ref: str) -> "tuple[str | None, str]":
    ref = (ref or "").strip()
    head, sep, tail = ref.partition(":")
    if sep and head in _PREFIX:
        return _PREFIX[head], tail
    return None, ref


def resolve(ref: str) -> "tuple[Addon | None, dict]":
    """Find *ref* (``id`` or ``<kind>:id``) in awpack, then Library, then awnode ext."""
    kind, aid = _split(ref)
    if ref.startswith("community:"):
        return Addon(kind="library", id=ref, trust="community", source="community"), {}
    if kind == "skill":
        rows, why = _skills_list()
        hit = next((a for a in rows if a.id == aid), None)
        return hit, ({} if hit or not why else {"skill": why})
    down: "dict[str, str]" = {}
    for k in ((kind,) if kind else SOURCES):
        rows, why = _source(k)
        hit = next((a for a in rows if a.id == aid), None)
        if hit:
            return hit, down
        if why:
            down[k] = why
    return None, down


def trust_of(ref: str) -> str:
    """The trust tier of *ref*; anything unresolvable is ``community`` (fail closed)."""
    if is_remote_skill(ref):
        try:
            return "official" if remote_skill_url(ref)[1] in _FIRST_PARTY_OWNERS else "community"
        except ValueError:
            return "community"
    kind, aid = _split(ref)
    if ref.startswith("community:"):
        return "community"
    if not _ID.match(aid or ""):
        return "community"
    from adk.addon_lock import lookup
    row = lookup(aid, kind)
    if row:
        return str(row.get("trust") or "community")
    hit, _ = resolve(ref)
    return hit.trust if hit else "community"


# ── install / remove per kind ───────────────────────────────────────────────────────


def _install_awpack(a: Addon, *, replace: bool, allow_community: bool) -> Result:
    from adk.addon_lock import record_install
    argv = ["install", a.id, "--json"]
    if replace:
        argv.append("--replace")
    if allow_community:
        argv.append("--allow-community")
    rc, out, err = _brick("awpack", argv)
    obj = _json_out(out)
    if rc != 0 or not obj.get("ok"):
        return Result(False, "awpack", a.id, obj.get("detail") or err.strip()
                      or f"awpack install exited {rc}")
    rc, vout, verr = _brick("awpack", ["verify", a.id, "--json"], timeout=60)
    vobj = _json_out(vout)
    if rc != 0 or not vobj.get("ok"):
        return Result(False, "awpack", a.id, "installed but `awpack verify` refused it: "
                      + (vobj.get("detail") or verr.strip() or f"exit {rc}"))
    sha = str(vobj.get("sha256") or obj.get("sha256") or "")
    dest = str(obj.get("dest") or vobj.get("dest") or "")
    if not sha and dest:
        sha = _tree_sha256(Path(dest))
    row = record_install("awpack", a.id, version=str(obj.get("version") or a.version),
                         source=f"awpack:{obj.get('provenance') or 'shelf'}", sha256=sha,
                         trust=_TRUST_FROM_PROVENANCE.get(str(obj.get("provenance")),
                                                          a.trust),
                         hash_of="content", dest=dest)
    return Result(True, "awpack", a.id, f"installed to {dest} (awpack verify ok)", row,
                  next=str(obj.get("next") or ""))


def _install_library(a: Addon) -> Result:
    from adk import cli
    from adk.addon_lock import lookup, record_install
    if a.source == "bundled":
        m = cli._local_pack_manifests().get(a.id)
        if m is None:
            return Result(False, "library", a.id, "bundled pack vanished")
        row = record_install("library", a.id, version=m.version or "bundled",
                             source="bundled:adk", sha256=_tree_sha256(Path(m.path)),
                             trust="official", hash_of="content", dest=str(m.path))
        return Result(True, "library", a.id, "ships with adk; nothing to download", row)
    import types
    rc = cli._cmd_pack(types.SimpleNamespace(pack_command="install", pack_id=a.id,
                                             library_only=True))
    if rc:
        return Result(False, "library", a.id, f"library install exited {rc}")
    local_id = a.id
    if a.id.startswith("community:"):
        rows = [r for r in _lock_rows("library") if r.get("source") == a.id]
        local_id = rows[-1]["id"] if rows else a.id
    return Result(True, "library", local_id, "installed", lookup(local_id, "library"))


def _install_awnode(a: Addon) -> Result:
    from adk.addon_lock import record_install
    rc, out, err = _brick("awnode", ["ext", "install", a.id, "--json"])
    obj = _json_out(out)
    if rc != 0 or obj.get("ok") is False:
        return Result(False, "awnode-ext", a.id, obj.get("error") or err.strip()
                      or f"awnode ext install exited {rc}", next=str(obj.get("hint") or ""))
    manifest = a.extra.get("manifest") or {"id": a.id}
    sha = hashlib.sha256(json.dumps(manifest, sort_keys=True, default=str)
                         .encode("utf-8")).hexdigest()
    row = record_install("awnode-ext", a.id, version=a.version or "unknown",
                         source="awnode:ext", sha256=sha, trust=a.trust,
                         hash_of="manifest")
    return Result(True, "awnode-ext", a.id, "installed; start it with `adk pack enable "
                  f"{a.id}` or `awnode ext start {a.id}`", row)


REMOTE_SKILL_PREFIXES = ("hermes:", "agentskills:")
_FIRST_PARTY_OWNERS = {"aitherium"}
_SEG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def is_remote_skill(ref: str) -> bool:
    return (ref or "").startswith(REMOTE_SKILL_PREFIXES)


def remote_skill_url(ref: str) -> "tuple[str, str]":
    """(raw SKILL.md URL, GitHub owner or host) for a ``hermes:``/``agentskills:`` ref.

    Raises:
        ValueError: a ref that does not name exactly one skill folder.
    """
    scheme, _, rest = ref.partition(":")
    if scheme == "agentskills" and rest.startswith("https://"):
        if not rest.endswith("SKILL.md"):
            raise ValueError("an agentskills URL must end in SKILL.md")
        host = rest.split("/")[2].lower()
        if host == "raw.githubusercontent.com":
            return rest, rest.split("/")[3].lower()
        return rest, host
    parts = [p for p in rest.strip("/").split("/") if p]
    if len(parts) < 3 or not all(_SEG.match(p) for p in parts) or ".." in parts:
        raise ValueError(f"{scheme}: wants <owner>/<repo>/<skill>, got {rest!r}")
    owner, repo, path = parts[0], parts[1], parts[2:]
    if scheme == "hermes":
        if len(path) != 1:
            raise ValueError(f"hermes: wants <owner>/<repo>/<skill>, got {rest!r}")
        path = ["skills", path[0]]  # a tap's default path
    return (f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/"
            f"{'/'.join(path)}/SKILL.md"), owner.lower()


def _http_get(url: str) -> bytes:
    """GET *url* (a GITHUB_TOKEN, when set, reaches private taps). Raises on non-200."""
    import httpx
    headers = {"User-Agent": "awdk-addons"}
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok and url.startswith("https://raw.githubusercontent.com/"):
        headers["Authorization"] = f"Bearer {tok}"
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        r = c.get(url, headers=headers)
    if r.status_code != 200:
        raise OSError(f"GET {url} -> HTTP {r.status_code}")
    if len(r.content) > 512 * 1024:
        raise OSError(f"{url}: {len(r.content)} bytes is not a SKILL.md")
    return r.content


def install_remote_skill(ref: str, *, target: str = "claude", replace: bool = False,
                         allow_community: bool = False, cwd: "Path | None" = None) -> Result:
    """Fetch one third-party ``SKILL.md`` (Hermes tap / agentskills.io) and install it.

    The skill's own frontmatter ``name`` names the folder; it must be a valid id. Only
    SKILL.md is fetched (a skill's ``references/`` are not). Recorded with its source
    URL and the sha256 of the bytes written.
    """
    from adk.addon_lock import lookup, record_install
    try:
        url, owner = remote_skill_url(ref)
    except ValueError as exc:
        return Result(False, "skill", ref, str(exc))
    trust = "official" if owner in _FIRST_PARTY_OWNERS else "community"
    if trust != "official" and not allow_community:
        return Result(False, "skill", ref, f"{ref} is a third-party skill nobody at Aitherium "
                      "reviewed; read it first, then re-run with --allow-community", next=url)
    try:
        data = _http_get(url)
    except Exception as exc:  # noqa: BLE001 - a fetch failure is the answer, not a crash
        return Result(False, "skill", ref, f"fetch failed: {exc}")
    text = data.decode("utf-8", errors="replace")
    m = _FRONT.match(text)
    name = ""
    if m:
        try:
            import yaml
            name = str((yaml.safe_load(m.group(1)) or {}).get("name") or "")
        except Exception:  # noqa: BLE001 - unparseable frontmatter = no name
            name = ""
    if not _ID.match(name):
        return Result(False, "skill", ref, f"SKILL.md at {url} has no valid frontmatter name")
    targets = skill_targets(cwd)
    if target not in targets:
        return Result(False, "skill", name, f"unknown target {target!r}")
    dest = targets[target] / name / "SKILL.md"
    prior = lookup(name, "skill")
    if dest.exists():
        if prior is None or Path(prior.get("dest", "")) != dest:
            return Result(False, "skill", name, f"{dest} exists and was not installed by "
                          "adk; not overwriting it")
        if not replace:
            return Result(False, "skill", name, f"already installed at {dest}; use update")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".md.installing")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    digest = hashlib.sha256(data).hexdigest()
    row = record_install("skill", name, version=digest[:12], source=ref, sha256=digest,
                         trust=trust, hash_of="file", dest=str(dest))
    return Result(True, "skill", name, f"installed to {dest} from {url}", row)


def install(ref: str, *, replace: bool = False, allow_community: bool = False) -> Result:
    """Resolve *ref* and install it through its own brick; records the lock row.

    A community add-on (nobody here reviewed it) needs ``allow_community`` -- an explicit
    ``community:<listing>`` id is that consent.
    """
    if is_remote_skill(ref):
        return install_remote_skill(ref, replace=replace, allow_community=allow_community)
    kind, aid = _split(ref)
    if kind == "skill":
        return install_skill(aid, replace=replace)
    if not ref.startswith("community:") and not _ID.match(aid or ""):
        return Result(False, kind or "", ref, f"invalid add-on id {ref!r}")
    hit, down = resolve(ref)
    clear_cache()
    if hit is None and kind in (None, "library"):
        # Genesis can serve a pack its catalog listing omits: the old direct path.
        res = _install_library(Addon(kind="library", id=aid, source="genesis"))
        if res.ok:
            return res
    if hit is None:
        why = "; ".join(f"{k}: {v}" for k, v in down.items())
        return Result(False, kind or "", aid, f"{aid!r} is not in "
                      f"{', '.join((kind,) if kind else SOURCES)}" + (f" ({why})" if why else ""))
    if hit.kind == "awpack":
        return _install_awpack(hit, replace=replace,
                               allow_community=allow_community)
    if hit.kind == "library":
        return _install_library(hit)
    return _install_awnode(hit)


def remove(ref: str) -> Result:
    """Remove an installed add-on through its own brick and drop its lock row."""
    from adk.addon_lock import forget_install, lookup
    kind, aid = _split(ref)
    if kind == "skill":
        return remove_skill(aid)
    row = lookup(aid, kind)
    k = (row or {}).get("kind") or kind or "library"
    if k == "awpack":
        rc, out, err = _brick("awpack", ["remove", aid, "--json"])
        obj = _json_out(out)
        if rc != 0 or not obj.get("ok"):
            return Result(False, k, aid, obj.get("detail") or err.strip() or f"exit {rc}")
    elif k == "awnode-ext":
        _brick("awnode", ["ext", "stop", aid], timeout=120)  # no `ext remove` verb yet
    else:
        import types

        from adk import cli
        rc = cli._cmd_pack(types.SimpleNamespace(pack_command="remove", pack_id=aid,
                                                 library_only=True))
        if rc:
            return Result(False, "library", aid, f"library remove exited {rc}")
    forget_install(k, aid)
    clear_cache()
    return Result(True, k, aid, "removed")


# ── skills (awskills .md -> a harness skills dir) ──────────────────────────────────

_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def skill_targets(cwd: "Path | None" = None) -> "dict[str, Path]":
    """Where a skill can go. ``claude`` is also what awsh's harness daemon reads."""
    return {"claude": Path.home() / ".claude" / "skills",
            "project": (cwd or Path.cwd()) / ".claude" / "skills",
            "adk": Path.home() / ".aither" / "skills"}


#: The public skill shelf. ``adk skills install`` used to need a hand-made clone of
#: it plus ``AWSKILLS_DIR`` (measured 2026-11-12: `adk skills install local-research`
#: failed until the tester did both); it is public, so the kit fetches it itself.
AWSKILLS_REPO = "Aitherium/awskills"
AWSKILLS_TARBALL = f"https://codeload.github.com/{AWSKILLS_REPO}/tar.gz/refs/heads/main"
SHELF_MARKER = ".adk-fetched.json"
SHELF_REFRESH_S = 24 * 3600.0
_SHELF_MAX_BYTES = 30 * 1024 * 1024
_SHELF_MAX_FILE = 512 * 1024


def _fetched_shelf_root() -> Path:
    return Path.home() / ".aither" / "awskills"


def _local_skills_shelf() -> "Path | None":
    env = os.environ.get("AWSKILLS_DIR", "").strip()
    cands: "list[Path]" = []
    if env:
        cands += [Path(env).expanduser() / "skills", Path(env).expanduser()]
    for base in (Path.cwd(), Path(__file__).resolve()):
        for parent in [base, *base.parents][:6]:
            cands.append(parent / "awskills" / "skills")
    cands.append(_fetched_shelf_root() / "skills")
    for c in cands:
        if c.is_dir() and any(c.glob("*.md")):
            return c
    return None


def shelf_age_s(shelf: "Path | None") -> "float | None":
    """Seconds since the kit fetched *shelf*, or None for a shelf it did not fetch
    (``$AWSKILLS_DIR`` or a checkout: the owner keeps those current, not the kit)."""
    if shelf is None:
        return None
    try:
        doc = json.loads((shelf.parent / SHELF_MARKER).read_text(encoding="utf-8"))
        return max(0.0, time.time() - float(doc.get("fetched_at") or 0))
    except (OSError, ValueError, TypeError):
        return None


def _download_shelf(url: str) -> bytes:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "aither-adk"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - fixed https URL
        data = resp.read(_SHELF_MAX_BYTES + 1)
    if len(data) > _SHELF_MAX_BYTES:
        raise ValueError("the awskills archive is larger than the fetch limit")
    return data


def fetch_skills_shelf(download=None) -> "tuple[Path | None, str]":
    """Fetch the public awskills shelf into ``~/.aither/awskills/skills``.

    Only top-level ``skills/<name>.md`` files are written (names validated, each
    capped), into a staging dir that replaces the previous fetched copy at the end,
    so a failed fetch leaves the old shelf in place. Returns ``(shelf, why)``:
    ``why`` is "" on success and the reason otherwise. ``AWSKILLS_NO_FETCH=1``
    refuses the network and says so.
    """
    import io
    import shutil
    import tarfile
    if os.environ.get("AWSKILLS_NO_FETCH", "").strip().lower() in ("1", "true", "yes"):
        return None, "fetching is disabled (AWSKILLS_NO_FETCH is set)"
    root = _fetched_shelf_root()
    if (root / ".git").exists():
        return None, f"{root} is a git checkout; update it with `git -C {root} pull`"
    try:
        blob = (download or _download_shelf)(AWSKILLS_TARBALL)
    except Exception as exc:  # noqa: BLE001 - reported to the person, old shelf kept
        return None, f"could not fetch {AWSKILLS_TARBALL}: {type(exc).__name__}: {exc}"
    staging = root.with_name(root.name + ".fetching")
    shutil.rmtree(staging, ignore_errors=True)
    (staging / "skills").mkdir(parents=True)
    count = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            for member in tar:
                parts = Path(member.name).parts
                if (not member.isfile() or len(parts) != 3 or parts[1] != "skills"
                        or not parts[2].endswith(".md") or member.size > _SHELF_MAX_FILE
                        or not _ID.match(parts[2][:-3])):
                    continue
                fh = tar.extractfile(member)
                if fh is None:
                    continue
                (staging / "skills" / parts[2]).write_bytes(fh.read())
                count += 1
    except (tarfile.TarError, OSError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return None, f"the awskills archive could not be read: {type(exc).__name__}: {exc}"
    if not count:
        shutil.rmtree(staging, ignore_errors=True)
        return None, "the awskills archive held no skills/*.md files"
    (staging / SHELF_MARKER).write_text(json.dumps(
        {"fetched_at": time.time(), "source": AWSKILLS_TARBALL, "skills": count}),
        encoding="utf-8")
    shutil.rmtree(root, ignore_errors=True)
    os.replace(staging, root)
    return root / "skills", ""


def find_skills_shelf(fetch: bool = True) -> "Path | None":
    """The awskills ``skills/`` dir: ``$AWSKILLS_DIR``, a checkout above cwd or adk,
    then ``~/.aither/awskills`` -- which the kit fetches itself when nothing else is
    there, and refreshes when its own copy is older than a day."""
    shelf = _local_skills_shelf()
    if not fetch:
        return shelf
    age = shelf_age_s(shelf)
    if shelf is None or (age is not None and age > SHELF_REFRESH_S):
        fetched, why = fetch_skills_shelf()
        if fetched is not None:
            return fetched
        logger.info("awskills shelf not refreshed: %s", why)
        _SHELF_NOTE["why"] = why
    return shelf


#: Why the last shelf fetch did not happen (shown instead of a bare "not found").
_SHELF_NOTE: "dict[str, str]" = {}


def _shelf_source(shelf: Path) -> str:
    """``awskills:<dir>`` plus the fetched copy's age, so a listing says how fresh
    its shelf is."""
    age = shelf_age_s(shelf)
    if age is None:
        return f"awskills:{shelf}"
    return f"awskills:{shelf} (fetched {int(age // 3600)}h ago from github.com/{AWSKILLS_REPO})"


def _describe(path: Path) -> "tuple[str, str]":
    text = path.read_text(encoding="utf-8", errors="replace")
    m = _FRONT.match(text)
    desc = ""
    if m:
        try:
            import yaml
            data = yaml.safe_load(m.group(1)) or {}
            desc = str(data.get("description") or "")
        except Exception:  # noqa: BLE001 - a bad frontmatter still lists by name
            desc = ""
    return path.stem, " ".join(desc.split())


def _skills_list() -> "tuple[list[Addon], str]":
    shelf = find_skills_shelf()
    if shelf is None:
        return [], ("no awskills shelf, and fetching it failed ("
                    + (_SHELF_NOTE.get("why") or "unknown reason")
                    + "). Needed: network access to github.com, or set AWSKILLS_DIR "
                    f"to a copy of https://github.com/{AWSKILLS_REPO}")
    from adk.addon_lock import lookup
    rows = []
    for md in sorted(shelf.glob("*.md")):
        if md.name.lower() == "readme.md":
            continue
        name, desc = _describe(md)
        rows.append(Addon(kind="skill", id=name, summary=desc, trust="official",
                          source=_shelf_source(shelf), installed=lookup(name, "skill")
                          is not None, extra={"path": str(md)}))
    return rows, ""


def install_skill(name: str, *, target: str = "claude", replace: bool = False,
                  cwd: "Path | None" = None) -> Result:
    """Copy awskills ``<name>.md`` to ``<target>/<name>/SKILL.md`` and record it.

    Refuses to overwrite a skill it did not install (a hand-written skill of the same
    name), and replaces its own only with ``replace``.
    """
    from adk.addon_lock import lookup, record_install
    if not _ID.match(name or ""):
        return Result(False, "skill", name, f"invalid skill name {name!r}")
    targets = skill_targets(cwd)
    if target not in targets:
        return Result(False, "skill", name, f"unknown target {target!r}; one of "
                      f"{', '.join(targets)}")
    rows, why = _skills_list()
    hit = next((a for a in rows if a.id == name), None)
    if hit is None:
        return Result(False, "skill", name, why or f"no skill {name!r} in awskills")
    src = Path(hit.extra["path"])
    dest = targets[target] / name / "SKILL.md"
    prior = lookup(name, "skill")
    if dest.exists():
        ours = prior is not None and Path(prior.get("dest", "")) == dest
        if not ours:
            return Result(False, "skill", name, f"{dest} exists and was not installed by "
                          "adk; not overwriting it")
        if not replace:
            return Result(False, "skill", name, f"already installed at {dest}; use update")
    data = src.read_bytes()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".md.installing")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    row = record_install("skill", name, version=hashlib.sha256(data).hexdigest()[:12],
                         source=hit.source, sha256=hashlib.sha256(data).hexdigest(),
                         trust="official", hash_of="file", dest=str(dest))
    return Result(True, "skill", name, f"installed to {dest}", row,
                  next=skill_usage_note(name, target))


def skill_usage_note(name: str, target: str) -> str:
    """Who can use a skill installed to *target*, said in the install output.

    A kit agent reads skills through its ``skill_search`` / ``skill_load`` tools,
    which scan ``adk.pack_activation._skill_search_dirs`` -- every install target
    is on that list, so the note is true for all three.
    """
    where = {"claude": "Claude Code in every project (`/" + name + "`)",
             "project": "Claude Code in this project (`/" + name + "`)",
             "adk": "kit agents only (Claude Code does not read ~/.aither/skills)"}[target]
    return (f"usable by: {where}\n"
            f"kit agents: skill_search(\"{name}\") then skill_load(\"{name}\")"
            + ("" if target != "project" else " when started in this directory"))


def remove_skill(name: str) -> Result:
    """Delete a skill adk installed (never one it did not) and drop its row."""
    from adk.addon_lock import forget_install, lookup
    row = lookup(name, "skill")
    if row is None:
        return Result(False, "skill", name, f"skill {name!r} was not installed by adk")
    dest = Path(row.get("dest", ""))
    if dest.name == "SKILL.md" and dest.is_file():
        dest.unlink()
        try:
            dest.parent.rmdir()
        except OSError:
            logger.debug("left %s: not empty", dest.parent)
    forget_install("skill", name)
    return Result(True, "skill", name, f"removed {dest}")


# ── helpers ─────────────────────────────────────────────────────────────────────────


def _lock_rows(kind: str) -> "list[dict]":
    from adk.addon_lock import installs
    return installs(kind)


def _tree_sha256(root: Path) -> str:
    """sha256 over every file under *root* (sorted relative paths + bytes)."""
    h = hashlib.sha256()
    for f in sorted(p for p in root.rglob("*") if p.is_file()):
        if "__pycache__" in f.parts:
            continue
        h.update(f.relative_to(root).as_posix().encode("utf-8"))
        h.update(f.read_bytes())
    return h.hexdigest()
