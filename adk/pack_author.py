"""Pack authoring: scaffold, validate, load-test and bundle a tool pack.

The consumer half of packs (``adk pack install|list|update|remove``) has existed for
a long time; the AUTHOR half did not, so a community developer had to copy an
in-tree pack by hand and learn its manifest from the loader's source. This module
is that missing half, as four verbs:

    adk pack new <id>          scaffold a working pack (manifest, one tool, test, README)
    adk pack validate <dir>    static checks -- nothing is imported or executed
    adk pack dev <dir>         load it through the REAL loader into a REAL ToolRegistry
    adk pack build <dir>       a reproducible .tar.gz + .sha256 ready to publish

``validate`` never imports the pack: a manifest check that runs the code under test
is a check an author cannot safely run on someone else's pack. ``dev`` is the one
verb that executes it, and says so.

Every verb exits 0 clean, 1 on a finding, 2 when it could not judge.
"""
from __future__ import annotations

import ast
import gzip
import hashlib
import io
import os
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST = ".toolpack.yaml"

#: Lowercase, starts with a letter, 3-64 chars of [a-z0-9._-].
ID_RE = re.compile(r"^[a-z][a-z0-9._-]{2,63}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

#: First-party namespaces. A community pack claiming one would shadow or impersonate
#: a pack the platform ships, and discovery is first-dir-wins.
RESERVED_PREFIXES = ("aither", "aitherium", "adk", "awdk")

KNOWN_TIERS = ("community", "free", "builder", "professional", "enterprise")

#: Secret shapes that must never ship inside a pack. Matches are reported by file
#: and line only; the value is never echoed.
SECRET_RES = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{32,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:sk|pk)_live_[A-Za-z0-9]{16,}"),
    re.compile(r"\bxox[bpas]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)

#: Never bundled: build output, caches, VCS metadata, local env files.
EXCLUDE_DIRS = {"__pycache__", ".git", ".pytest_cache", ".ruff_cache", ".venv", "dist"}
EXCLUDE_SUFFIXES = (".pyc", ".pyo")
EXCLUDE_NAMES = {".env", ".DS_Store"}


@dataclass
class Finding:
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code} {self.message}"


@dataclass
class Report:
    pack_dir: Path
    manifest: dict = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    judged: bool = True

    @property
    def ok(self) -> bool:
        return self.judged and not self.findings

    @property
    def exit_code(self) -> int:
        if not self.judged:
            return 2
        return 1 if self.findings else 0

    def add(self, code: str, message: str) -> None:
        self.findings.append(Finding(code, message))


# ── validate ─────────────────────────────────────────────────────────────────────


def _load_yaml(path: Path) -> dict:
    import yaml

    data = yaml.safe_load(path.read_text("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest is not a mapping")
    return data


def _defines_register(init: Path) -> bool:
    """True when __init__.py defines a top-level ``register`` -- by AST, never import."""
    try:
        tree = ast.parse(init.read_text("utf-8"), filename=str(init))
    except (SyntaxError, UnicodeDecodeError):
        return False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "register":
            return True
        if isinstance(node, ast.ImportFrom):
            if any((a.asname or a.name) == "register" for a in node.names):
                return True
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "register" for t in node.targets):
                return True
    return False


def iter_pack_files(pack_dir: Path):
    """Every file that belongs in the bundle, sorted, relative to pack_dir."""
    out = []
    for root, dirs, files in os.walk(pack_dir):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS)
        for name in files:
            if name in EXCLUDE_NAMES or name.endswith(EXCLUDE_SUFFIXES):
                continue
            out.append(Path(root, name).relative_to(pack_dir))
    return sorted(out, key=lambda p: p.as_posix())


def validate(pack_dir: str | Path, *, community: bool = True) -> Report:
    """Static validation. Imports nothing, executes nothing.

    PKA001 manifest present and parses        PKA006 ui/persona files exist
    PKA002 id shape                          PKA007 no symlinks
    PKA003 id not in a reserved namespace    PKA008 no secret-shaped strings
    PKA004 version is semver, name+desc set  PKA009 tier is a known tier
    PKA005 an entry point that exposes register()
    """
    pack_dir = Path(pack_dir)
    rep = Report(pack_dir=pack_dir)
    if not pack_dir.is_dir():
        rep.judged = False
        rep.add("PKA000", f"not a directory: {pack_dir}")
        return rep

    mf = pack_dir / MANIFEST
    if not mf.is_file():
        rep.add("PKA001", f"{MANIFEST} not found in {pack_dir}")
        return rep
    try:
        data = _load_yaml(mf)
    except ImportError:
        rep.judged = False
        rep.add("PKA000", "PyYAML is not installed; cannot read the manifest")
        return rep
    except Exception as exc:  # noqa: BLE001 -- any parse failure is the finding
        rep.add("PKA001", f"{MANIFEST} does not parse: {type(exc).__name__}: {exc}")
        return rep
    rep.manifest = data

    pid = str(data.get("id") or "").strip()
    if not pid:
        rep.add("PKA002", "manifest has no `id`")
    elif not ID_RE.match(pid):
        rep.add("PKA002", f"id {pid!r} must match {ID_RE.pattern}")
    elif community and pid.split(".")[0].split("-")[0] in RESERVED_PREFIXES:
        rep.add("PKA003", f"id {pid!r} uses a reserved first-party namespace "
                          f"({', '.join(RESERVED_PREFIXES)}); prefix it with your handle, "
                          f"e.g. yourname.{pid.split('.')[-1]}")

    version = str(data.get("version") or "").strip()
    if not SEMVER_RE.match(version):
        rep.add("PKA004", f"version {version!r} is not semver (e.g. 0.1.0)")
    for key in ("name", "description"):
        if not str(data.get(key) or "").strip():
            rep.add("PKA004", f"manifest has no `{key}`")

    tier = str(data.get("tier") or "free").lower()
    if tier not in KNOWN_TIERS:
        rep.add("PKA009", f"tier {tier!r} is not one of {', '.join(KNOWN_TIERS)}")

    # Entry point: the loader file-loads __init__.py when tool_modules is empty, or
    # imports the dotted module(s). Either way something must expose register().
    modules = data.get("tool_modules") or []
    mcp_server = data.get("mcp_server") or {}
    init = pack_dir / "__init__.py"
    if not modules:
        if init.is_file():
            if not _defines_register(init):
                rep.add("PKA005", "__init__.py does not define register(registry) -> int")
        elif not mcp_server:
            rep.add("PKA005", "no entry point: add __init__.py with register(registry), "
                              "list tool_modules, or declare an mcp_server")

    for key in ("persona_fragments",):
        for rel in data.get(key) or []:
            rel = str(rel)
            if rel.endswith((".md", ".txt")) and not (pack_dir / rel).is_file():
                rep.add("PKA006", f"{key} names {rel!r}, which does not exist")
    ui = data.get("ui") or {}
    if isinstance(ui, dict) and ui.get("assets_dir"):
        if not (pack_dir / str(ui["assets_dir"])).is_dir():
            rep.add("PKA006", f"ui.assets_dir {ui['assets_dir']!r} does not exist")

    for root, dirs, files in os.walk(pack_dir):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for name in dirs + files:
            p = Path(root, name)
            if p.is_symlink():
                rep.add("PKA007", f"symlink not allowed in a pack: {p.relative_to(pack_dir)}")

    for rel in iter_pack_files(pack_dir):
        p = pack_dir / rel
        if p.is_symlink():
            continue
        try:
            text = p.read_text("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(r.search(line) for r in SECRET_RES):
                rep.add("PKA008", f"secret-shaped string at {rel.as_posix()}:{lineno} "
                                  f"(value not shown) -- move it to the vault or env")
    return rep


# ── new ──────────────────────────────────────────────────────────────────────────

_MANIFEST_TMPL = """\
id: {pid}
name: {title}
version: 0.1.0
description: >-
  {title} -- a community tool pack for awdk agents. Replace this line with one
  sentence that says what an agent can do once the pack is installed.
category: tool_packs
tier: community
author: {author}
tags: [community]

# Empty -> the loader file-loads __init__.py and calls register(registry).
tool_modules: []

# Tools that need a key read it at call time (env, ~/.aither/provider_keys.json or
# the vault) and fail soft with guidance. Never put a key in this folder.
entitlements: []
"""

_INIT_TMPL = '''\
"""{title} -- awdk tool pack.

The loader calls ``register(registry)`` and expects the number of tools registered.
One bad tool must never sink the pack, so each registration is guarded.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

TOOL_NAMES = ["{fn}"]


def register(registry) -> int:
    from . import tools

    n = 0
    for name in TOOL_NAMES:
        fn = getattr(tools, name, None)
        if not callable(fn):
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 -- one bad tool != a dead pack
            logger.debug("{pid}: skip %s: %s", name, exc)
    return n
'''

_TOOLS_TMPL = '''\
"""Tools exposed by {pid}. The docstring and type hints ARE the tool schema the
agent sees, so write them for the model: say what the tool returns and when to
use it."""
from __future__ import annotations


def {fn}(text: str) -> str:
    """Echo `text` back, reversed. Replace this with your first real tool."""
    return text[::-1]
'''

_TEST_TMPL = '''\
"""Runs the pack through the same loader and registry an agent uses."""
from pathlib import Path

from adk.pack_author import dev_load


def test_pack_registers_its_tools():
    res = dev_load(Path(__file__).resolve().parent.parent)
    assert res.ok, res.error
    assert "{fn}" in res.tool_names
'''

_README_TMPL = """\
# {title}

A community tool pack for [awdk](https://pypi.org/project/awdk/) agents.

```bash
adk pack validate .     # static checks -- imports nothing
adk pack dev .          # load through the real loader, list the tools
adk pack build .        # {pid}-0.1.0.tar.gz + .sha256
```

Install it on any awnix box or awdk install by copying the folder into
`~/.aitheros/packs/`, or point `AITHER_TOOLPACK_DIRS` at its parent while developing.

License: Apache-2.0 (change it if you prefer).
"""


def scaffold(pack_id: str, dest: str | Path = ".", *, author: str = "") -> Path:
    """Write a working pack to ``dest/<pack_id>``. Refuses to overwrite."""
    if not ID_RE.match(pack_id):
        raise ValueError(f"pack id {pack_id!r} must match {ID_RE.pattern}")
    if pack_id.split(".")[0].split("-")[0] in RESERVED_PREFIXES:
        raise ValueError(f"{pack_id!r} uses a reserved namespace; prefix it with your handle")
    target = Path(dest) / pack_id
    if target.exists():
        raise FileExistsError(f"{target} already exists")
    short = re.sub(r"[^a-z0-9]+", "_", pack_id.split(".")[-1]).strip("_") or "pack"
    fn = f"{short}_echo"
    title = pack_id.split(".")[-1].replace("-", " ").replace("_", " ").title()
    author = author or os.environ.get("USER") or os.environ.get("USERNAME") or "you"
    ctx = {"pid": pack_id, "title": title, "fn": fn, "author": author}
    (target / "tests").mkdir(parents=True)
    files = {
        MANIFEST: _MANIFEST_TMPL,
        "__init__.py": _INIT_TMPL,
        "tools.py": _TOOLS_TMPL,
        "tests/test_pack.py": _TEST_TMPL,
        "README.md": _README_TMPL,
    }
    for rel, tmpl in files.items():
        (target / rel).write_text(tmpl.format(**ctx), encoding="utf-8", newline="\n")
    return target


# ── dev ──────────────────────────────────────────────────────────────────────────


@dataclass
class DevResult:
    ok: bool
    pack_id: str = ""
    registered: int = 0
    tool_names: list[str] = field(default_factory=list)
    error: str = ""


def dev_load(pack_dir: str | Path) -> DevResult:
    """Load the pack exactly as an agent would: ToolPackLoader discovery over the
    pack's parent directory, then register() into a fresh ToolRegistry. This EXECUTES
    the pack's code."""
    from adk.tool_pack_loader import ToolPackLoader
    from adk.tools import ToolRegistry

    pack_dir = Path(pack_dir).resolve()
    rep = validate(pack_dir)
    if not rep.judged:
        return DevResult(False, error="; ".join(map(str, rep.findings)))
    pid = str(rep.manifest.get("id") or pack_dir.name)
    loader = ToolPackLoader(extra_dirs=[pack_dir.parent], enforce_entitlements=False)
    # Only the pack under test: other dirs may hold a same-id pack that would win.
    loader.dirs = [pack_dir.parent]
    manifests = loader.discover()
    m = manifests.get(pid)
    if m is None or m.path.resolve() != pack_dir:
        return DevResult(False, pid, error=f"loader did not discover {pid!r} at {pack_dir}")

    class _Agent:
        pass

    agent = _Agent()
    agent._tools = ToolRegistry()
    n = loader.register_on_adk_agent(m, agent)
    names = sorted(getattr(agent._tools, "_tools", {}).keys())
    if n <= 0:
        return DevResult(False, pid, n, names,
                         error="register() returned 0 tools (see the log for why)")
    return DevResult(True, pid, n, names)


# ── build ────────────────────────────────────────────────────────────────────────


@dataclass
class BuildResult:
    tarball: Path
    sha256: str
    files: int
    size: int


def build(pack_dir: str | Path, out_dir: str | Path | None = None) -> BuildResult:
    """A reproducible bundle: sorted entries, zeroed mtimes/owners, fixed gzip header.
    The same source tree yields the same bytes, so the published sha256 is checkable
    by anyone who rebuilds from the tag. Refuses a pack that does not validate."""
    pack_dir = Path(pack_dir).resolve()
    rep = validate(pack_dir)
    if not rep.ok:
        raise ValueError("pack does not validate:\n  " + "\n  ".join(map(str, rep.findings)))
    pid = str(rep.manifest["id"])
    version = str(rep.manifest["version"])
    out = Path(out_dir) if out_dir else pack_dir / "dist"
    out.mkdir(parents=True, exist_ok=True)
    name = f"{pid}-{version}"
    tar_path = out / f"{name}.tar.gz"

    raw = io.BytesIO()
    files = iter_pack_files(pack_dir)
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for rel in files:
            data = (pack_dir / rel).read_bytes()
            info = tarfile.TarInfo(f"{name}/{rel.as_posix()}")
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tf.addfile(info, io.BytesIO(data))
    gz = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=gz, mtime=0, compresslevel=9) as g:
        g.write(raw.getvalue())
    blob = gz.getvalue()
    tar_path.write_bytes(blob)
    digest = hashlib.sha256(blob).hexdigest()
    (out / f"{name}.tar.gz.sha256").write_text(f"{digest}  {tar_path.name}\n", encoding="utf-8",
                                               newline="\n")
    return BuildResult(tar_path, digest, len(files), len(blob))


# ── CLI glue (called from adk.cli._cmd_pack) ─────────────────────────────────────


def cli(sub: str, args) -> int:
    """Dispatch ``adk pack new|validate|dev|build``. Returns an exit code."""
    try:
        if sub == "new":
            path = scaffold(args.pack_id, getattr(args, "dest", ".") or ".",
                            author=getattr(args, "author", "") or "")
            print(f"created {path}")
            print(f"  next: adk pack validate {path}  &&  adk pack dev {path}")
            return 0
        if sub == "validate":
            rep = validate(args.pack_dir, community=not getattr(args, "first_party", False))
            for f in rep.findings:
                print(f"  {f}")
            if rep.ok:
                print(f"OK {rep.manifest.get('id')} {rep.manifest.get('version')}: "
                      f"manifest, entry point, files, secrets -- all clean")
            elif not rep.judged:
                print("NOT JUDGED")
            return rep.exit_code
        if sub == "dev":
            print("loading through the real ToolPackLoader (this runs the pack's code)")
            res = dev_load(args.pack_dir)
            if not res.ok:
                print(f"FAIL {res.pack_id}: {res.error}")
                return 1
            print(f"OK {res.pack_id}: {res.registered} tool(s) registered")
            for n in res.tool_names:
                print(f"  - {n}")
            return 0
        if sub == "build":
            res = build(args.pack_dir, getattr(args, "output", None))
            print(f"built {res.tarball} ({res.files} files, {res.size} bytes)")
            print(f"sha256 {res.sha256}")
            return 0
    except (ValueError, FileExistsError) as exc:
        print(f"error: {exc}")
        return 1
    except ImportError as exc:
        print(f"cannot judge: {exc}")
        return 2
    print(f"unknown pack verb: {sub}")
    return 2
