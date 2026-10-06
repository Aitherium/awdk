"""``adk awconnect install|status|path`` -- put the Awconnect browser extension on
this machine with one command, and say whether a browser actually loaded it.

Awconnect is a Chrome MV3 extension. Until it is on the Chrome Web Store (the only
true one-click install -- see ``awconnect/WEBSTORE_READINESS.md``) it is loaded
UNPACKED, and three facts bound what any installer can do:

* stable Chrome ignores ``--load-extension`` (removed for branded builds in 137);
* off-store force-install policy (``ExtensionInstallForcelist``) only works on a
  managed (domain-joined / MDM) machine;
* ``chrome://extensions`` cannot be driven by automation.

So the best guided flow is: keep a STABLE folder the browser loads from
(``~/.aither/awconnect/current``), open ``chrome://extensions`` in the browser the
owner has, put the folder path on the clipboard, name the two clicks that remain
(Developer mode, Load unpacked -> paste), then WATCH the browser's own profile
files until the extension shows up. ``status`` reads those files read-only, which
is also how "it silently disappeared from Chrome" becomes visible instead of a
mystery.

Layout::

    ~/.aither/awconnect/<version>/      one immutable copy per version (rollback)
    ~/.aither/awconnect/current/        what the browser loads; refreshed IN PLACE
    ~/.aither/awconnect/current/.awconnect-install.json   {version, source, sha256}

``current`` is a real directory, not a symlink: a Windows symlink needs admin or
Developer Mode, and a browser that loaded ``current`` keeps loading it after
``--update`` rewrites its contents -- the owner clicks the extension's reload
arrow and runs the new version.

Sources, in order: the latest ``connect-v*`` GitHub release's ``*-<variant>-v<ver>.zip`` asset
with its ``.sha256`` VERIFIED (a mismatch or a missing checksum is a refusal, never
a warning), else a monorepo checkout's ``awconnect/`` exported the way
``git archive`` would (tracked files at a ref, never the live shared tree), else an
explicit ``--from DIR``. When the checkout is strictly newer than the newest
release, the checkout wins -- installing an older release over a newer tree would
be a downgrade the owner did not ask for.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Where release metadata is read. The monorepo is private, so an unauthenticated
#: request answers 404 and the release source is skipped -- the fetch below prefers
#: the authenticated gh CLI when present (2026-10-05: without it the installer
#: silently staged the legacy awconnect/ checkout while the product shipped as
#: connect-v4.x releases), and degrades to urllib on public repos and in CI.
DEFAULT_RELEASES_API = "https://api.github.com/repos/Aitherium/AitherOS/releases?per_page=50"
RELEASE_TAG_PREFIX = "connect-v"
#: Name markers that identify the extension in a manifest (display name varies:
#: "awconnect — AI Chat & Knowledge Assistant", older "AitherConnect").
NAME_MARKERS = ("awconnect", "aitherconnect", "aither connect")
MARKER_FILE = ".awconnect-install.json"
#: Never shipped in the loadable folder (mirrors Build-Distributions.ps1's denylist).
EXCLUDE_NAMES = frozenset(
    {
        "web-ext-artifacts",
        "node_modules",
        ".git",
        ".gitignore",
        ".DS_Store",
        "tests",
        "spike",
        "docs",
        "_connect_temp",
    }
)
EXCLUDE_SUFFIXES = (".zip", ".crx", ".pem")
DEFAULT_WAIT_S = 120
POLL_EVERY_S = 3.0

FetchBytes = Callable[[str], bytes]


class AwconnectError(RuntimeError):
    """A refusal the CLI reports verbatim (exit 1)."""


class ChecksumMismatchError(AwconnectError):
    """A downloaded release asset does not hash to its published .sha256."""


# ── paths ────────────────────────────────────────────────────────────────────


def aither_home(env: Optional[Dict[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("AITHER_HOME")
    return Path(base) if base else Path.home() / ".aither"


def install_root(env: Optional[Dict[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    override = env.get("AITHER_AWCONNECT_HOME")
    return Path(override) if override else aither_home(env) / "awconnect"


def current_dir(env: Optional[Dict[str, str]] = None) -> Path:
    return install_root(env) / "current"


# Chrome's own manifest "version" rule: 1-4 dot-separated integers. Anything
# else is refused before it becomes a directory name under the install root --
# a manifest version of "../x" or an absolute path would otherwise make stage()
# rmtree and overwrite a directory outside it.
_VERSION_RE = re.compile(r"^\d{1,5}(\.\d{1,5}){0,3}$")


def safe_version_dir(root: Path, version: str) -> Path:
    """``root / version`` for a Chrome-valid version that stays inside ``root``."""
    v = str(version or "")
    if not _VERSION_RE.match(v):
        raise AwconnectError(
            f"refusing manifest version {v!r}: not a Chrome version (1-4 dotted integers)"
        )
    versioned = root / v
    if versioned.resolve().parent != root.resolve():
        raise AwconnectError(f"refusing manifest version {v!r}: resolves outside {root}")
    return versioned


def version_tuple(v: str) -> Tuple[int, ...]:
    """'3.8.0' -> (3, 8, 0). Non-numeric parts sort as 0 so junk never wins."""
    out: List[int] = []
    for part in str(v or "").strip().lstrip("v").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def read_marker(folder: Path) -> Dict[str, Any]:
    try:
        return json.loads((folder / MARKER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def manifest_of(folder: Path) -> Dict[str, Any]:
    try:
        return json.loads((folder / "manifest.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def _norm(p: str) -> str:
    """Comparable form of a path as a browser wrote it (case-folded on Windows)."""
    s = os.path.normpath(str(p or "")).rstrip("\\/")
    return s.lower() if os.name == "nt" or ":\\" in s else s


# ── sources ──────────────────────────────────────────────────────────────────


@dataclass
class Candidate:
    kind: str  # "release" | "checkout" | "dir"
    version: str
    location: str  # asset URL, repo root, or folder
    sha256_url: str = ""
    ref: str = "HEAD"
    meta: Dict[str, Any] = field(default_factory=dict)


_ASSET_URL_RE = re.compile(
    r"https://github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/(.+)$")


def _gh_fetch(url: str) -> Optional[bytes]:
    """Read a GitHub URL through the authenticated ``gh`` CLI, or None.

    None means "no gh, not authenticated, or it failed" -- every caller falls
    back to the anonymous urllib path, so public-repo and CI behaviour cannot
    change. WHY (measured 2026-10-05): this repo is private, so the anonymous
    releases probe answered 404, ``latest_release`` returned None (its designed
    "no release source"), and ``adk awconnect install`` silently staged the
    LEGACY ``awconnect/`` checkout while the product shipped as ``connect-v4.x``
    release assets -- v4 could only be installed by hand. gh reads both the API
    and the assets.
    """
    if not shutil.which("gh"):
        return None
    try:
        if url.startswith("https://api.github.com/"):
            out = subprocess.run(
                ["gh", "api", url[len("https://api.github.com/"):]],
                capture_output=True, timeout=60,
            )
            return out.stdout if out.returncode == 0 and out.stdout else None
        match = _ASSET_URL_RE.match(url)
        if match:
            owner, repo, tag, name = match.groups()
            jq = subprocess.run(
                ["gh", "api", f"repos/{owner}/{repo}/releases/tags/{tag}",
                 "--jq", f'.assets[] | select(.name == "{name}") | .id'],
                capture_output=True, text=True, encoding="utf-8", timeout=60,
            )
            asset_id = (jq.stdout or "").strip().splitlines()[:1]
            if jq.returncode != 0 or not asset_id:
                return None
            blob = subprocess.run(
                ["gh", "api", f"repos/{owner}/{repo}/releases/assets/{asset_id[0]}",
                 "-H", "Accept: application/octet-stream"],
                capture_output=True, timeout=120,
            )
            return blob.stdout if blob.returncode == 0 and blob.stdout else None
    except Exception:  # noqa: BLE001 -- any gh failure degrades to urllib
        return None
    return None


def _default_fetch(url: str) -> bytes:
    via_gh = _gh_fetch(url)
    if via_gh is not None:
        return via_gh
    import urllib.request

    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json, application/octet-stream;q=0.9, */*;q=0.5",
            "User-Agent": "awdk-awconnect-setup",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 -- https URLs we build
        return resp.read()


def latest_release(
    fetch: FetchBytes, api_url: str = DEFAULT_RELEASES_API, variant: str = "enterprise"
) -> Optional[Candidate]:
    """The newest ``connect-v*`` release that carries the zip AND its checksum.

    Returns None when the API cannot be read (private repo -> 404, offline) --
    that is "no release source", not an error.
    """
    try:
        releases = json.loads(fetch(api_url).decode("utf-8"))
    except Exception:  # noqa: BLE001 -- unreadable metadata == no release source
        return None
    if not isinstance(releases, list):
        return None
    best: Optional[Candidate] = None
    for rel in releases:
        if not isinstance(rel, dict) or rel.get("draft") or rel.get("prerelease"):
            continue
        tag = str(rel.get("tag_name") or "")
        if not tag.startswith(RELEASE_TAG_PREFIX):
            continue
        version = tag[len(RELEASE_TAG_PREFIX) :]
        assets = {
            str(a.get("name")): str(a.get("browser_download_url") or "")
            for a in rel.get("assets") or []
            if isinstance(a, dict)
        }
        for var in ((variant,) if variant == "public" else (variant, "unpacked", "public")):
            # "unpacked" before "public": the browser loads current/ as an
            # unpacked extension, so its id must come from the manifest "key"
            # (the pinned id both manifests share, already accepted by the
            # identity service and the local daemon). The PUBLIC zip is built
            # WITHOUT a key for the store to assign its own id -- staged here
            # it would get a PATH-derived id (measured 2026-10-06: 4.1.x was
            # staged keyless and sign-in answered "Invalid redirect_uri").
            # Matched by suffix, not a hardcoded stem: the build names the zip
            # after the product, and a rename must not silently drop the source.
            suffix = f"-{var}-v{version}.zip"
            name = next(
                (n for n in sorted(assets) if n.endswith(suffix) and f"{n}.sha256" in assets), ""
            )
            if name:
                cand = Candidate(
                    "release",
                    version,
                    assets[name],
                    sha256_url=assets[f"{name}.sha256"],
                    meta={"tag": tag, "asset": name},
                )
                if best is None or version_tuple(version) > version_tuple(best.version):
                    best = cand
                break
    return best


def find_checkout(
    env: Optional[Dict[str, str]] = None, start: Optional[Path] = None
) -> Optional[Path]:
    """A monorepo root holding ``awconnect/manifest.json``, or None."""
    env = os.environ if env is None else env
    seeds: List[Path] = []
    for key in ("AITHER_AWCONNECT_REPO", "AITHEROS_ROOT", "AITHER_REPO"):
        if env.get(key):
            seeds.append(Path(env[key]))
    if start is not None:
        seeds.append(start)
    seeds.append(Path(__file__).resolve().parent)
    for seed in seeds:
        for cand in [seed, *seed.parents]:
            if (cand / "awconnect" / "manifest.json").is_file():
                return cand
    return None


def checkout_candidate(root: Path, ref: str = "HEAD") -> Optional[Candidate]:
    ver = str(manifest_of(root / "awconnect").get("version") or "")
    if not ver:
        return None
    return Candidate("checkout", ver, str(root), ref=ref)


def choose_source(
    release: Optional[Candidate], checkout: Optional[Candidate], prefer: str = "auto"
) -> Optional[Candidate]:
    """Release first; a strictly newer checkout wins (never a silent downgrade)."""
    if prefer == "release":
        return release
    if prefer == "checkout":
        return checkout
    if release and checkout:
        if version_tuple(checkout.version) > version_tuple(release.version):
            return checkout
        return release
    return release or checkout


# ── staging ──────────────────────────────────────────────────────────────────


def _excluded(rel_parts: Iterable[str]) -> bool:
    parts = list(rel_parts)
    if any(p in EXCLUDE_NAMES for p in parts):
        return True
    return bool(parts) and parts[-1].lower().endswith(EXCLUDE_SUFFIXES)


def _safe_target(dest: Path, rel: str) -> Optional[Path]:
    """Refuse absolute paths and ``..`` escapes (zip-slip)."""
    rel = rel.replace("\\", "/").lstrip("/")
    if not rel or rel.endswith("/"):
        return None
    target = (dest / rel).resolve()
    if dest.resolve() not in target.parents:
        raise AwconnectError(f"refusing archive entry outside the target: {rel}")
    return target


def verify_sha256(blob: bytes, sha_text: str) -> str:
    """Return the digest, or raise ChecksumMismatchError. ``sha_text`` is ``<hex>  <name>``."""
    expected = (sha_text.strip().split() or [""])[0].lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise ChecksumMismatchError("the release's .sha256 file holds no SHA-256 digest")
    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected:
        raise ChecksumMismatchError(
            f"checksum mismatch: asset hashes to {actual[:16]}…, release says {expected[:16]}… "
            "-- refusing to install it"
        )
    return actual


def stage_release(cand: Candidate, dest: Path, fetch: FetchBytes) -> Dict[str, Any]:
    if not cand.sha256_url:
        raise ChecksumMismatchError(
            "release asset has no .sha256 -- refusing an unverifiable install"
        )
    sha_text = fetch(cand.sha256_url).decode("utf-8", "replace")
    blob = fetch(cand.location)
    digest = verify_sha256(blob, sha_text)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for info in zf.infolist():
            target = _safe_target(dest, info.filename)
            if target is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    return {"sha256": digest}


def stage_checkout(
    cand: Candidate, dest: Path, run: Callable[..., Any] = subprocess.run
) -> Dict[str, Any]:
    """Export ``awconnect/`` at ``cand.ref`` like ``git archive`` -- tracked files only.

    Falls back to a filtered copy of the folder when git is unavailable (a source
    tarball has no .git); the fallback still drops tests/, node_modules/ and keys.
    """
    root = Path(cand.location)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        proc = run(
            ["git", "-C", str(root), "archive", "--format=tar", cand.ref, "awconnect"],
            capture_output=True,
            timeout=120,
            check=False,
        )
        blob = proc.stdout if proc.returncode == 0 else b""
    except (OSError, subprocess.SubprocessError):
        blob = b""
    if blob:
        with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
            for member in tf.getmembers():
                if not member.isfile() or not member.name.startswith("awconnect/"):
                    continue
                rel = member.name[len("awconnect/") :]
                if _excluded(rel.split("/")):
                    continue
                target = _safe_target(dest, rel)
                src = tf.extractfile(member)
                if target is None or src is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
        return {"ref": cand.ref, "method": "git-archive"}
    src_root = root / "awconnect"
    for path in src_root.rglob("*"):
        rel_parts = path.relative_to(src_root).parts
        if path.is_dir() or _excluded(rel_parts):
            continue
        target = dest.joinpath(*rel_parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    return {"ref": "working-tree", "method": "copy"}


def _replace_contents(folder: Path, source: Path) -> None:
    """Make ``folder`` hold exactly ``source``'s files WITHOUT removing ``folder``
    itself -- the browser holds that path; deleting and recreating it is fine on
    POSIX but races an open handle on Windows."""
    folder.mkdir(parents=True, exist_ok=True)
    for child in folder.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in source.iterdir():
        if child.is_dir():
            shutil.copytree(child, folder / child.name)
        else:
            shutil.copy2(child, folder / child.name)


def stage(
    cand: Candidate,
    env: Optional[Dict[str, str]] = None,
    fetch: FetchBytes = _default_fetch,
    run: Callable[..., Any] = subprocess.run,
) -> Dict[str, Any]:
    """Materialize ``cand`` as ``<root>/<version>/`` and refresh ``current/`` in place."""
    root = install_root(env)
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".staging-", dir=str(root)))
    try:
        if cand.kind == "release":
            meta = stage_release(cand, tmp, fetch)
        elif cand.kind == "checkout":
            meta = stage_checkout(cand, tmp, run)
        elif cand.kind == "dir":
            src = Path(cand.location)
            for path in src.rglob("*"):
                rel_parts = path.relative_to(src).parts
                if path.is_file() and not _excluded(rel_parts):
                    target = tmp.joinpath(*rel_parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
            meta = {"method": "copy"}
        else:  # pragma: no cover -- programming error
            raise AwconnectError(f"unknown source kind {cand.kind!r}")
        man = manifest_of(tmp)
        if not man.get("manifest_version"):
            raise AwconnectError("the staged copy has no valid manifest.json -- refusing it")
        version = str(man.get("version") or cand.version)
        versioned = safe_version_dir(root, version)
        marker = {
            "version": version,
            "source": cand.kind,
            "location": cand.location,
            "installed_at": int(time.time()),
            **meta,
        }
        (tmp / MARKER_FILE).write_text(json.dumps(marker, indent=2), encoding="utf-8")
        if versioned.exists():
            shutil.rmtree(versioned)
        shutil.copytree(tmp, versioned)
        _replace_contents(root / "current", tmp)
        # The browser loads current/ unpacked, so its extension id is fixed by that
        # path; allow exactly that id on the local daemon's /identity/whoami.
        from adk.extension_id import allow_extension_id, unpacked_extension_id
        ext_id = unpacked_extension_id(root / "current")
        allow_extension_id(ext_id, aither_home(env) / "awconnect" / "allowed_extension_ids")
        return {
            "version": version,
            "path": str(root / "current"),
            "extension_id": ext_id,
            "versioned_path": str(versioned),
            **marker,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── browsers ─────────────────────────────────────────────────────────────────


@dataclass
class Browser:
    id: str
    name: str
    exe: Optional[str]
    user_data: Optional[Path]
    extensions_url: str = "chrome://extensions"


def _browser_table(
    platform: str, env: Dict[str, str], home: Path
) -> List[Tuple[str, str, List[str], List[Path], str]]:
    if platform == "win32":
        pf = [
            env.get("ProgramFiles", r"C:\Program Files"),
            env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        ]
        local = env.get("LOCALAPPDATA", str(home / "AppData" / "Local"))
        return [
            (
                "chrome",
                "Google Chrome",
                [str(Path(b) / "Google/Chrome/Application/chrome.exe") for b in (*pf, local)],
                [Path(local) / "Google/Chrome/User Data"],
                "chrome://extensions",
            ),
            (
                "edge",
                "Microsoft Edge",
                [str(Path(b) / "Microsoft/Edge/Application/msedge.exe") for b in (*pf, local)],
                [Path(local) / "Microsoft/Edge/User Data"],
                "edge://extensions",
            ),
            (
                "brave",
                "Brave",
                [
                    str(Path(b) / "BraveSoftware/Brave-Browser/Application/brave.exe")
                    for b in (*pf, local)
                ],
                [Path(local) / "BraveSoftware/Brave-Browser/User Data"],
                "brave://extensions",
            ),
        ]
    if platform == "darwin":
        sup = home / "Library/Application Support"
        return [
            (
                "chrome",
                "Google Chrome",
                [
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    str(home / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                ],
                [sup / "Google/Chrome"],
                "chrome://extensions",
            ),
            (
                "edge",
                "Microsoft Edge",
                ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
                [sup / "Microsoft Edge"],
                "edge://extensions",
            ),
            (
                "brave",
                "Brave",
                ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"],
                [sup / "BraveSoftware/Brave-Browser"],
                "brave://extensions",
            ),
        ]
    cfg = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    return [
        (
            "chrome",
            "Google Chrome",
            ["google-chrome", "google-chrome-stable"],
            [cfg / "google-chrome"],
            "chrome://extensions",
        ),
        (
            "chromium",
            "Chromium",
            ["chromium", "chromium-browser"],
            [cfg / "chromium"],
            "chrome://extensions",
        ),
        (
            "edge",
            "Microsoft Edge",
            ["microsoft-edge", "microsoft-edge-stable"],
            [cfg / "microsoft-edge"],
            "edge://extensions",
        ),
        (
            "brave",
            "Brave",
            ["brave-browser", "brave"],
            [cfg / "BraveSoftware/Brave-Browser"],
            "brave://extensions",
        ),
    ]


def detect_browsers(
    platform: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    home: Optional[Path] = None,
    exists: Callable[[str], bool] = os.path.exists,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> List[Browser]:
    """Installed Chromium-family browsers, preference order Chrome > Edge > Brave.

    A browser counts when its executable OR its profile directory exists -- the
    profile alone is enough for ``status``; ``install`` needs the executable.
    """
    platform = platform or sys.platform
    env = dict(os.environ) if env is None else env
    home = home or Path.home()
    found: List[Browser] = []
    for bid, name, exes, datas, ext_url in _browser_table(platform, env, home):
        exe = None
        for cand in exes:
            if os.sep in cand or "/" in cand or "\\" in cand:
                if exists(cand):
                    exe = cand
                    break
            else:
                hit = which(cand)
                if hit:
                    exe = hit
                    break
        data = next((d for d in datas if exists(str(d))), None)
        if exe or data:
            found.append(Browser(bid, name, exe, data, ext_url))
    return found


# ── status ───────────────────────────────────────────────────────────────────


def _profiles(user_data: Path) -> List[Path]:
    out: List[Path] = []
    try:
        for child in sorted(user_data.iterdir()):
            if child.is_dir() and (child.name == "Default" or child.name.startswith("Profile ")):
                out.append(child)
    except OSError as exc:
        # An unreadable profile dir is "no profiles here", said out loud.
        logger.debug("awconnect: cannot list %s: %s", user_data, exc)
    return out


def _ext_settings(profile: Path) -> Dict[str, Dict[str, Any]]:
    """extensions.settings merged from Preferences and Secure Preferences.

    Chrome moved unpacked extensions into Secure Preferences (MAC-protected); an
    older build or Edge may keep them in Preferences. Read both, never write.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    for fname in ("Preferences", "Secure Preferences"):
        try:
            data = json.loads((profile / fname).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        settings = (data.get("extensions") or {}).get("settings") or {}
        for ext_id, entry in settings.items():
            if isinstance(entry, dict):
                merged.setdefault(ext_id, {}).update(entry)
    return merged


def _is_enabled(entry: Dict[str, Any]) -> bool:
    reasons = entry.get("disable_reasons")
    if reasons not in (None, 0, [], {}):
        return False
    return entry.get("state", 1) != 0


def _entry_matches(
    entry: Dict[str, Any], known_paths: Iterable[str]
) -> Tuple[bool, Dict[str, Any]]:
    path = str(entry.get("path") or "")
    man = entry.get("manifest") if isinstance(entry.get("manifest"), dict) else {}
    if not man and path and os.path.isabs(path):
        man = manifest_of(Path(path))
    npath = _norm(path) if path else ""
    if npath and npath in {_norm(p) for p in known_paths}:
        return True, man
    name = str(man.get("name") or "").lower()
    if any(m in name for m in NAME_MARKERS):
        return True, man
    return False, man


def status(
    env: Optional[Dict[str, str]] = None, browsers: Optional[List[Browser]] = None
) -> Dict[str, Any]:
    """What every browser profile says about Awconnect. Read-only."""
    root = install_root(env)
    cur = root / "current"
    latest = read_marker(cur)
    latest_version = str(latest.get("version") or manifest_of(cur).get("version") or "")
    known = [str(cur)]
    try:
        known += [str(p) for p in root.iterdir() if p.is_dir()]
    except OSError as exc:
        # No install root yet: only a manifest-name match can find the extension.
        logger.debug("awconnect: no install root %s: %s", root, exc)
    browsers = detect_browsers(env=env) if browsers is None else browsers
    hits: List[Dict[str, Any]] = []
    for br in browsers:
        if not br.user_data:
            continue
        for prof in _profiles(br.user_data):
            for ext_id, entry in _ext_settings(prof).items():
                ok, man = _entry_matches(entry, known)
                if not ok:
                    continue
                path = str(entry.get("path") or "")
                ver = str(man.get("version") or "")
                stale = bool(
                    latest_version and ver and version_tuple(ver) < version_tuple(latest_version)
                )
                hits.append(
                    {
                        "browser": br.id,
                        "browser_name": br.name,
                        "profile": prof.name,
                        "extension_id": ext_id,
                        "version": ver,
                        "path": path,
                        "unpacked": entry.get("location") == 4,
                        "enabled": _is_enabled(entry),
                        "uses_current": bool(path) and _norm(path) == _norm(str(cur)),
                        "stale": stale,
                    }
                )
    enabled = [h for h in hits if h["enabled"]]
    if not hits:
        state = "not_installed"
    elif not enabled:
        state = "disabled"
    elif any(h["stale"] for h in enabled):
        state = "stale"
    else:
        state = "installed"
    return {
        "state": state,
        "installed": bool(enabled),
        "hits": hits,
        "latest": {
            "version": latest_version,
            "path": str(cur),
            "present": (cur / "manifest.json").is_file(),
            "source": latest.get("source", ""),
        },
        "browsers": [
            {
                "id": b.id,
                "name": b.name,
                "exe": b.exe,
                "user_data": str(b.user_data) if b.user_data else None,
            }
            for b in browsers
        ],
    }


def status_line(st: Dict[str, Any]) -> str:
    """One human line, shared by the CLI, awsh and the awdesk tray."""
    state = st.get("state")
    hits = st.get("hits") or []
    live = next((h for h in hits if h.get("enabled")), None) or (hits[0] if hits else None)
    where = f"{live['browser_name']} / {live['profile']}" if live else ""
    latest = (st.get("latest") or {}).get("version") or "?"
    if state == "installed":
        return f"Awconnect {live['version'] or ''} installed ({where})".replace("  ", " ")
    if state == "stale":
        return f"Awconnect {live['version']} is stale ({where}); {latest} is ready -- reload it"
    if state == "disabled":
        return f"Awconnect is installed but disabled ({where})"
    return "Awconnect is not installed in any browser profile"


# ── install ──────────────────────────────────────────────────────────────────


def copy_to_clipboard(
    text: str, platform: Optional[str] = None, run: Callable[..., Any] = subprocess.run
) -> bool:
    platform = platform or sys.platform
    if platform == "win32":
        cmds = [["clip"]]
    elif platform == "darwin":
        cmds = [["pbcopy"]]
    else:
        cmds = [
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ]
    for cmd in cmds:
        try:
            proc = run(cmd, input=text.encode("utf-8"), timeout=10, check=False)
            if getattr(proc, "returncode", 1) == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def open_extensions_page(br: Browser, popen: Callable[..., Any] = subprocess.Popen) -> bool:
    """Start the browser exe with its extensions URL.

    ``webbrowser.open`` / ``start`` refuse chrome:// URLs; the browser binary
    given the URL as an argument opens it (in the running instance if any).
    """
    if not br.exe:
        return False
    try:
        popen(
            [br.exe, br.extensions_url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **({"close_fds": True} if os.name != "nt" else {}),
        )
        return True
    except OSError:
        return False


def next_steps(br: Optional[Browser], folder: str, copied: bool) -> List[str]:
    page = br.extensions_url if br else "chrome://extensions"
    return [
        f"In {br.name if br else 'your browser'} ({page}): turn on Developer mode (top right).",
        "Click Load unpacked and paste the folder path"
        + (" (it is on your clipboard)" if copied else "")
        + f": {folder}",
    ]


def pick_browser(browsers: List[Browser], want: str = "") -> Optional[Browser]:
    usable = [b for b in browsers if b.exe]
    if want:
        return next((b for b in usable if b.id == want), None)
    return usable[0] if usable else None


def wait_for_install(
    env: Optional[Dict[str, str]],
    seconds: float,
    browsers: Optional[List[Browser]] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Dict[str, Any]:
    deadline = clock() + seconds
    st = status(env, browsers)
    while not (st["installed"] and any(h["uses_current"] for h in st["hits"] if h["enabled"])):
        if clock() >= deadline:
            break
        sleep(POLL_EVERY_S)
        st = status(env, browsers)
    return st


def install(
    *,
    env: Optional[Dict[str, str]] = None,
    prefer: str = "auto",
    from_dir: str = "",
    ref: str = "HEAD",
    variant: str = "enterprise",
    browser: str = "",
    wait: float = DEFAULT_WAIT_S,
    open_browser: bool = True,
    fetch: FetchBytes = _default_fetch,
    run: Callable[..., Any] = subprocess.run,
    popen: Callable[..., Any] = subprocess.Popen,
    browsers: Optional[List[Browser]] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = print,
    update_only: bool = False,
) -> Dict[str, Any]:
    env = dict(os.environ) if env is None else env
    if from_dir:
        src = Path(from_dir)
        man = manifest_of(src)
        if not man:
            raise AwconnectError(f"{from_dir} has no manifest.json")
        cand: Optional[Candidate] = Candidate("dir", str(man.get("version") or ""), str(src))
    else:
        api = env.get("AITHER_AWCONNECT_RELEASES_API", DEFAULT_RELEASES_API)
        rel = latest_release(fetch, api, variant) if prefer in ("auto", "release") else None
        root = find_checkout(env) if prefer in ("auto", "checkout") else None
        chk = checkout_candidate(root, ref) if root else None
        cand = choose_source(rel, chk, prefer)
    if cand is None:
        raise AwconnectError(
            "no Awconnect source: no readable connect-v* release (the repo may be private) "
            "and no monorepo checkout found -- pass --from <awconnect folder> or set AITHEROS_ROOT"
        )
    log(f"  source: {cand.kind} {cand.version} ({cand.location})")
    staged = stage(cand, env, fetch=fetch, run=run)
    log(
        f"  staged: {staged['path']}  (version {staged['version']}; "
        f"copy kept at {staged['versioned_path']})"
    )
    before = status(env, browsers)
    result: Dict[str, Any] = {"staged": staged, "source": cand.kind}
    already = [h for h in before["hits"] if h["enabled"] and h["uses_current"]]
    if already or update_only:
        if already:
            log(
                "  already loaded from this folder -- click the reload arrow on the Awconnect card"
                " in chrome://extensions (or restart the browser) to run the new copy."
            )
        result["status"] = before
        result["steps"] = []
        return result
    browsers_list = detect_browsers(env=env) if browsers is None else browsers
    br = pick_browser(browsers_list, browser)
    if browser and br is None:
        raise AwconnectError(f"browser {browser!r} is not installed here")
    copied = copy_to_clipboard(staged["path"], run=run)
    opened = open_extensions_page(br, popen) if (br and open_browser) else False
    steps = next_steps(br, staged["path"], copied)
    result.update(
        {"browser": br.id if br else None, "opened": opened, "clipboard": copied, "steps": steps}
    )
    if br is None:
        log(
            "  no Chrome, Edge or Brave executable found -- "
            "open your browser's extensions page yourself."
        )
    elif opened:
        log(f"  opened {br.extensions_url} in {br.name}.")
    log("  two clicks left:")
    for i, step in enumerate(steps, 1):
        log(f"    {i}. {step}")
    if wait > 0:
        log(f"  watching the browser profile for up to {int(wait)}s…")
        st = wait_for_install(env, wait, browsers, sleep=sleep, clock=clock)
        result["status"] = st
        log(
            "  "
            + (
                status_line(st)
                if st["installed"]
                else "not seen yet -- run `adk awconnect status` after you click Load unpacked."
            )
        )
    return result


# ── CLI ──────────────────────────────────────────────────────────────────────


def register_parser(sub: Any) -> None:
    p = sub.add_parser("awconnect", help="Install / check the Awconnect browser extension")
    asub = p.add_subparsers(dest="awconnect_action")
    ins = asub.add_parser(
        "install",
        help="Stage the extension, open the browser's extensions page, "
        "copy the folder path, and watch for the load",
    )
    ins.add_argument(
        "--update",
        action="store_true",
        help="Refresh the folder in place only (no browser, no wait)",
    )
    ins.add_argument("--source", choices=["auto", "release", "checkout"], default="auto")
    ins.add_argument(
        "--from", dest="from_dir", default="", help="Install from this awconnect folder"
    )
    ins.add_argument("--ref", default="HEAD", help="Git ref for a checkout source (default HEAD)")
    ins.add_argument(
        "--variant",
        choices=["enterprise", "unpacked", "public"],
        default="enterprise",
        help="Release asset variant; the default falls back unpacked -> public "
        "(public is keyless and would stage a path-derived extension id)",
    )
    ins.add_argument("--browser", default="", help="chrome | edge | brave | chromium")
    ins.add_argument(
        "--wait",
        type=float,
        default=DEFAULT_WAIT_S,
        help=f"Seconds to watch for the load (default {DEFAULT_WAIT_S}; 0 = do not wait)",
    )
    ins.add_argument("--no-open", action="store_true", help="Do not open the browser")
    ins.add_argument("--json", action="store_true")
    st = asub.add_parser("status", help="Is Awconnect loaded, enabled and current in any browser?")
    st.add_argument("--json", action="store_true")
    asub.add_parser("path", help="Print the folder to Load unpacked")
    pr = asub.add_parser(
        "pair",
        help="Pair the Awconnect extension with this machine's daemon: approve a "
             "code it shows, list pending requests, or revoke every paired token",
    )
    pr.add_argument("pair_action", nargs="?", default="pending",
                    choices=["pending", "approve", "revoke"],
                    help="pending (default), approve <code>, revoke")
    pr.add_argument("code", nargs="?", default="", help="the 6-digit code the extension shows")


def _pair_daemon_request(path: str, method: str = "GET",
                         body: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
    """Call the local daemon's extension-pair door AS THE OWNER.

    The owner credential is ``~/.aither/daemon-token`` (adk.local_auth) -- mode
    0600, readable only by the user the daemon runs as, which is exactly why the
    browser cannot approve its own pairing and this CLI can. Returns
    ``(status, payload)``; status 0 means the daemon was unreachable."""
    import urllib.error
    import urllib.request

    from adk import local_auth
    from adk.daemon_endpoint import resolve_daemon_url

    url = resolve_daemon_url() + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, data=data)
    token = local_auth.read_token()
    if token:
        req.add_header(local_auth.HEADER, token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        # A FastAPI refusal is {"detail": "..."}; anything else is shown as it came.
        try:
            parsed = json.loads(detail)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("detail"):
            detail = str(parsed["detail"])
        return exc.code, detail
    except urllib.error.URLError as exc:
        return 0, f"cannot reach the local daemon at {url}: {exc.reason}"


def cmd_awconnect_pair(args: Any) -> int:
    """`adk awconnect pair [pending|approve <code>|revoke]`."""
    from adk.extension_pair import PAIR_TTL_S

    action = getattr(args, "pair_action", "") or "pending"
    if action == "approve":
        code = (getattr(args, "code", "") or "").strip()
        if not code:
            print("usage: adk awconnect pair approve <code>", file=sys.stderr)
            return 2
        status, payload = _pair_daemon_request(
            "/local/extension-pair/approve", "POST", {"code": code})
        if status == 0:
            print(payload, file=sys.stderr)
            print("Start the daemon with:  adk serve", file=sys.stderr)
            return 2
        if status == 404:
            print(f"{payload} (codes expire after {int(PAIR_TTL_S)} s)", file=sys.stderr)
            return 1
        if status != 200:
            print(f"HTTP {status}: {payload}", file=sys.stderr)
            return 1
        print(f"approved the pairing requested by {payload.get('origin', '?')}")
        print("the extension picks up its token on its next poll")
        return 0
    if action == "revoke":
        status, payload = _pair_daemon_request("/local/extension-pair/revoke", "POST", {})
        if status == 0:
            print(payload, file=sys.stderr)
            print("Start the daemon with:  adk serve", file=sys.stderr)
            return 2
        if status != 200:
            print(f"HTTP {status}: {payload}", file=sys.stderr)
            return 1
        print(f"revoked {payload.get('revoked', 0)} paired extension token(s)")
        return 0
    status, payload = _pair_daemon_request("/local/extension-pair/pending")
    if status == 0:
        print(payload, file=sys.stderr)
        print("Start the daemon with:  adk serve", file=sys.stderr)
        return 2
    if status != 200:
        print(f"HTTP {status}: {payload}", file=sys.stderr)
        return 1
    pending = (payload or {}).get("pending") or []
    if not pending:
        print("no pending pairings")
        return 0
    for row in pending:
        origin = str(row.get("origin") or "?")
        if row.get("status") == "pending":
            print(f"  {row.get('code', '?')}  {origin}  ({row.get('expires_in', 0)}s left)"
                  "   approve: adk awconnect pair approve " + str(row.get("code", "")))
        else:
            print(f"  {row.get('code', '?')}  {origin}  ({row.get('status')})")
    print("Approve a code ONLY if you started that pairing in your own browser. An "
          "unexpected request may be another local user asking for your daemon.")
    return 0


def cmd_awconnect(args: Any) -> int:
    action = getattr(args, "awconnect_action", None) or "status"
    as_json = bool(getattr(args, "json", False))
    if action == "pair":
        return cmd_awconnect_pair(args)
    if action == "path":
        print(current_dir())
        return 0
    if action == "status":
        st = status()
        if as_json:
            print(json.dumps(st, indent=2))
        else:
            print(status_line(st))
            for h in st["hits"]:
                flag = "enabled" if h["enabled"] else "DISABLED"
                print(
                    f"  {h['browser_name']} / {h['profile']}: {h['version'] or '?'} {flag}"
                    f"{' (stale)' if h['stale'] else ''} -- {h['path'] or h['extension_id']}"
                )
            if not st["latest"]["present"]:
                print("  no staged copy yet -- run `adk awconnect install`")
        return 0 if st["installed"] else 1
    if action == "install":
        logs: List[str] = []
        log: Callable[[str], None] = logs.append if as_json else print
        try:
            res = install(
                prefer=args.source,
                from_dir=args.from_dir,
                ref=args.ref,
                variant=args.variant,
                browser=args.browser,
                wait=0 if args.update else args.wait,
                open_browser=not args.no_open,
                update_only=args.update,
                log=log,
            )
        except AwconnectError as exc:
            if as_json:
                print(json.dumps({"ok": False, "error": str(exc), "log": logs}))
            else:
                print(f"  [x] {exc}")
            return 1
        if as_json:
            print(json.dumps({"ok": True, **res, "log": logs}, indent=2, default=str))
        return 0
    print("Usage: adk awconnect [install|status|path|pair]")
    return 2
