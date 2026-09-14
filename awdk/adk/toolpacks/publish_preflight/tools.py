"""Publish preflight -- tool implementations.

Stdlib only, and no imports outside this pack: this ships to an index, so
anything it reaches for has to exist on a stranger's machine.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

#: Fallback floor when a project declares no `requires-python`. Chosen, not
#: guessed: it is the oldest interpreter still receiving security fixes at the
#: time of writing, so a package that says nothing is assumed to want at least
#: that. A project needing older says so, and this reads it.
_log = logging.getLogger("publish_preflight")

_DEFAULT_FLOOR = (3, 10)

_REQUIRES = re.compile(r'^\s*requires-python\s*=\s*["\']([^"\']+)', re.M)
_MIN = re.compile(r">=\s*(\d+)\.(\d+)")
_NAME = re.compile(r'^\s*name\s*=\s*["\']([^"\']+)', re.M)


def _run(cmd: List[str], cwd: Optional[str] = None, timeout: int = 300):
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8",
                              errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        class _Fail:
            returncode = 127
            stdout = ""
            stderr = str(exc)
        return _Fail()


def _pyproject(package_dir: str) -> Optional[str]:
    p = Path(package_dir or ".") / "pyproject.toml"
    if not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _floor(text: Optional[str]) -> tuple:
    """The (major, minor) an interpreter must meet.

    Read with a regex rather than a toml parser ON PURPOSE: the stdlib toml
    reader is 3.11+, and the whole point of this function is to run BEFORE we
    know the interpreter is new enough. Parsing the file to decide which parser
    we may use is the circularity this avoids.
    """
    if not text:
        return _DEFAULT_FLOOR
    m = _REQUIRES.search(text)
    if not m:
        return _DEFAULT_FLOOR
    lo = _MIN.search(m.group(1))
    if not lo:
        return _DEFAULT_FLOOR
    return (int(lo.group(1)), int(lo.group(2)))


def _dist_name(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = _NAME.search(text)
    return m.group(1) if m else None


def _version_of(exe: str) -> Optional[tuple]:
    r = _run([exe, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"], timeout=30)
    if r.returncode != 0:
        return None
    try:
        a, b = (r.stdout or "").strip().split(".")[:2]
        return (int(a), int(b))
    except ValueError:
        return None


def _candidates() -> List[str]:
    """Interpreters worth asking about, best-effort.

    The one running this code is included: an agent invoked by a new
    interpreter can still be asked to publish for an old one, and vice versa.
    """
    out = [sys.executable]
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            out.append(found)
    # Version-suffixed binaries are how most distributions ship more than one.
    for minor in range(20, 7, -1):
        found = shutil.which("python3.%d" % minor)
        if found:
            out.append(found)
    # A managed tool cache, where build images usually keep alternatives.
    cache = os.environ.get("RUNNER_TOOL_CACHE") or "/opt/hostedtoolcache"
    base = Path(cache) / "Python"
    if base.is_dir():
        try:
            for child in sorted(base.iterdir()):
                exe = child / "x64" / "bin" / "python3"
                if exe.is_file():
                    out.append(str(exe))
        except OSError as exc:
            # Not fatal -- the cache is one source among several -- but not
            # nothing either: the search was NARROWER than it looks, and a
            # later "no interpreter found" would otherwise be unexplained.
            _log.debug("tool cache %s unreadable: %s", base, exc)
    seen, uniq = set(), []
    for c in out:
        real = os.path.realpath(c)
        if real not in seen:
            seen.add(real)
            uniq.append(c)
    return uniq


def publish_select_python(package_dir: str = ".") -> str:
    """Pick an interpreter that satisfies the package's requires-python.

    Why this is a tool and not a line of shell: requires-python is enforced at
    INSTALL time, never at build time. An interpreter one minor version too old
    will build the wheel, run the tests, and fail at the last step with
    "requires a different Python" -- after everything that could have caught it
    has already reported success.

    Returns the chosen interpreter, every candidate considered with its
    version, and a REFUSAL when nothing qualifies. Refusing is the useful
    answer: a publish on an interpreter that cannot install its own artifact is
    worse than no publish.
    """
    text = _pyproject(package_dir)
    floor = _floor(text)
    considered: List[Dict[str, Any]] = []
    chosen: Optional[str] = None
    for exe in _candidates():
        ver = _version_of(exe)
        entry = {"path": exe, "version": ("%d.%d" % ver) if ver else "unknown"}
        entry["ok"] = bool(ver and ver >= floor)
        considered.append(entry)
        if entry["ok"] and chosen is None:
            chosen = exe
    declared = bool(text and _REQUIRES.search(text))
    result: Dict[str, Any] = {
        "floor": "%d.%d" % floor,
        "floor_source": "requires-python" if declared
                        else "default (no requires-python declared)",
        "considered": considered,
        "selected": chosen,
    }
    if chosen is None:
        result["status"] = "refused"
        result["fix"] = (
            "No interpreter >= %d.%d was found. Install one, or point "
            "RUNNER_TOOL_CACHE at a directory that has one. Publishing from an "
            "older interpreter will build and test successfully and then fail "
            "at install." % floor)
    else:
        result["status"] = "success"
    return json.dumps(result, indent=2)


def publish_verify_wheel(package_dir: str = ".", import_name: str = "") -> str:
    """Build the package, install the WHEEL, and import it by its dist name.

    This catches the failure nothing else can see. A package whose distribution
    name and import name differ -- a renamed distribution, a src layout pointing
    at an old module, a find-packages pattern that no longer matches -- passes
    every check that reads the SOURCE TREE:

        the tests pass         they import the on-disk name, from the tree
        the build succeeds     a wheel is a zip; it imports nothing
        the metadata is valid  the name is whatever you wrote
        the upload would work  the index does not import your code

    and then `pip install <dist>; import <dist>` raises ModuleNotFoundError on
    the first machine that is not yours.

    Pass import_name when the two legitimately differ; otherwise the
    distribution name is used, normalised the way an import would be.
    """
    src = Path(package_dir or ".")
    text = _pyproject(package_dir)
    if text is None:
        return json.dumps({"status": "not_configured",
                           "fix": "no pyproject.toml in %s" % src})
    dist = _dist_name(text)
    if not dist:
        return json.dumps({"status": "not_configured",
                           "fix": "pyproject.toml declares no [project] name"})
    want = import_name or dist.replace("-", "_")

    sel = json.loads(publish_select_python(package_dir))
    if sel["status"] != "success":
        return json.dumps({"status": "refused",
                           "reason": "no adequate interpreter",
                           "detail": sel}, indent=2)
    py = sel["selected"]

    tmp = tempfile.mkdtemp(prefix="publish_preflight_")
    try:
        b = _run([py, "-m", "build", "--wheel", "--outdir", tmp],
                 cwd=str(src), timeout=900)
        if b.returncode != 0:
            return json.dumps({
                "status": "build_failed",
                "interpreter": py,
                "stderr": (b.stderr or b.stdout or "")[-2000:],
                "fix": "The build failed. If it reports a missing module, "
                       "install the build backend for THIS interpreter -- not "
                       "for whichever one happens to be first on PATH.",
            }, indent=2)
        wheels = sorted(Path(tmp).glob("*.whl"))
        if not wheels:
            return json.dumps({
                "status": "build_failed",
                "fix": "the build reported success and produced no wheel; "
                       "treat that as a failure, never as a pass",
            }, indent=2)
        target = Path(tmp) / "site"
        i = _run([py, "-m", "pip", "install", "--quiet", "--target",
                  str(target), str(wheels[0])], timeout=900)
        if i.returncode != 0:
            return json.dumps({
                "status": "install_failed",
                "wheel": wheels[0].name,
                "interpreter": py,
                "stderr": (i.stderr or i.stdout or "")[-2000:],
                "fix": "The wheel BUILT and will not INSTALL. If this says "
                       "'requires a different Python', the build interpreter is "
                       "older than requires-python -- run publish_select_python.",
            }, indent=2)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(target)
        code = "import %s as m;print(getattr(m, '__file__', '(namespace)'))" % want
        try:
            c = subprocess.run([py, "-c", code], capture_output=True, text=True,
                               timeout=120, env=env, encoding="utf-8",
                               errors="replace")
        except (OSError, subprocess.SubprocessError) as exc:
            return json.dumps({"status": "import_failed", "stderr": str(exc)})
        if c.returncode != 0:
            top: List[str] = []
            if target.is_dir():
                try:
                    top = sorted({p.name for p in target.iterdir()
                                  if not p.name.endswith((".dist-info", ".data"))})
                except OSError:
                    top = []
            return json.dumps({
                "status": "import_failed",
                "distribution": dist,
                "tried_import": want,
                "installed_top_level": top,
                "stderr": (c.stderr or "")[-1200:],
                "fix": "The wheel installs and does not import under its own "
                       "name. Compare tried_import with installed_top_level: if "
                       "they differ, either rename the package directory to "
                       "match the distribution, or pass import_name= if they are "
                       "MEANT to differ. Tests cannot see this -- they import "
                       "from the source tree, never from the built artifact.",
            }, indent=2)
        return json.dumps({
            "status": "success",
            "distribution": dist,
            "imported": want,
            "wheel": wheels[0].name,
            "interpreter": py,
            "module_file": (c.stdout or "").strip(),
        }, indent=2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


#: Publish failures whose message names something other than the cause. Each
#: entry is (matcher, what it actually means, what to do). First match wins per
#: entry, so specific signatures come before general ones.
_DIAGNOSES = [
    (re.compile(r"too many new projects", re.I),
     "The index is refusing to CREATE a new project, not refusing your upload. "
     "The credential is fine and the artifact transferred -- the refusal lands "
     "after the bytes.",
     "This limit is keyed to the ACCOUNT, not the network. Measured: uploading "
     "from a different machine on a different network gets the identical "
     "answer, so relocating the job and retrying are both dead ends. It is "
     "triggered by creating several NEW projects close together, so space "
     "first-ever publishes out. A new VERSION of an existing project is "
     "unaffected."),
    (re.compile(r"invalid-publisher|trusted publish", re.I),
     "Trusted publishing is keyed to (project, workflow, repository). A project "
     "that has never been published has no publisher to match, so the very "
     "first release cannot use it.",
     "Publish the first version with a token; trusted publishing works for "
     "every release after it."),
    (re.compile(r"No module named ['\"]?tomllib", re.I),
     "The stdlib toml reader is 3.11+. The step reading your version is running "
     "on an older interpreter than you think.",
     "Guard the import (try it, fall back to a text read of the version line), "
     "or select an interpreter first -- see publish_select_python."),
    (re.compile(r"no such option: --break-system-packages", re.I),
     "That flag is pip 23.0+; this pip is older.",
     "With --target the flag is unnecessary -- the externally-managed refusal "
     "only covers installs into the system prefix. Drop it rather than "
     "version-guarding it."),
    (re.compile(r"requires a different Python", re.I),
     "The wheel BUILT and will not INSTALL: requires-python is enforced at "
     "install time, so an interpreter one minor too old passes every earlier "
     "step.",
     "Run publish_select_python and build with what it chooses."),
    (re.compile(r"File already exists", re.I),
     "That exact version is already on the index. Indexes are append-only; a "
     "version is never replaced.",
     "Bump the version. If this is a retry after a partial failure, check "
     "whether the first attempt actually succeeded before assuming it did not."),
    (re.compile(r"\b403\b|Forbidden|not allowed to upload", re.I),
     "The credential authenticated and is not permitted for THIS project -- "
     "usually a project-scoped token pointed at a different project.",
     "Check the token's scope. A project-scoped token cannot create a new "
     "project at all."),
]


def publish_diagnose_failure(error_text: str = "") -> str:
    """Translate a publish error into its cause and the action that follows.

    Publish failures are unusually bad at naming themselves: the message
    describes the symptom at whichever surface the tooling happened to hit,
    often several layers from the decision that caused it.

    Returns EVERY match rather than the first, because these stack -- fixing one
    routinely reveals the next underneath.
    """
    text = (error_text or "").strip()
    if not text:
        return json.dumps({"status": "not_configured",
                           "fix": "pass the publish step's error output"})
    hits = []
    for matcher, means, action in _DIAGNOSES:
        if matcher.search(text):
            hits.append({"means": means, "do": action})
    if not hits:
        return json.dumps({
            "status": "unknown",
            "note": "No known signature matched. Before assuming the error "
                    "names its own cause, check whether the step ran on the "
                    "machine you expect: on a mixed fleet the same lane at the "
                    "same commit fails differently depending on which host took "
                    "the job.",
        }, indent=2)
    return json.dumps({"status": "success", "diagnoses": hits}, indent=2)


def publish_preflight(package_dir: str = ".", import_name: str = "") -> str:
    """Run every check that can fail BEFORE anything is uploaded.

    An upload is not reversible: a version, once on an index, is never
    replaced. So everything knowable locally is settled first, and the
    irreversible step is deliberately left to the caller.
    """
    steps = []
    sel = json.loads(publish_select_python(package_dir))
    steps.append({"step": "interpreter satisfies requires-python",
                  "status": sel["status"],
                  "selected": sel.get("selected"),
                  "floor": sel.get("floor")})
    if sel["status"] != "success":
        return json.dumps({"status": "refused", "steps": steps,
                           "fix": sel.get("fix")}, indent=2)
    ver = json.loads(publish_verify_wheel(package_dir, import_name))
    steps.append({"step": "wheel builds, installs and imports",
                  "status": ver["status"],
                  "detail": ver.get("fix") or ver.get("imported")})
    ok = ver["status"] == "success"
    return json.dumps({
        "status": "success" if ok else "failed",
        "steps": steps,
        "next": ("Preflight clean. The upload itself is the only irreversible "
                 "step and is deliberately not performed here."
                 if ok else "Fix the failing step above; do not upload."),
    }, indent=2)


TOOLS = [
    publish_select_python,
    publish_verify_wheel,
    publish_diagnose_failure,
    publish_preflight,
]
