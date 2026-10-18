"""Desk -- the one way to get it, the one sentence that says how to update it.

Desk is a PUBLIC build released at ``github.com/Aitherium/awdesk``. Every door that
installs it does the same thing the workspace setup page (``/setup-pc``) does:

  1. read ``SHA256SUMS.txt`` of the LATEST release and find this platform's asset
     (``Desk-<version>-windows-x64-setup.exe`` on Windows);
  2. download that asset from the release pinned to ``v<version>`` and check its SHA256
     against the sums file -- a mismatch, or a sums file that does not list the asset,
     REFUSES and the download is deleted. This proves integrity (no truncated or
     corrupted download), not provenance: the sums file ships in the same release;
  3. run the installer silently (``/S``, per user) on Windows;
  4. write ``~/.aither/desk-tenant.json`` so Desk opens the right workspace.

The tenant file follows Desk's own contract: ``host`` is a bare lowercase hostname, a
single label under ``aitherium.com``. Two shapes are written, never anything else:

  * an explicit workspace (``--tenant <label>``): the installer shape the setup page
    writes (``schema``, ``app``, ``name``, ``host``, ``url`` ...). It replaces any file.
  * the signed-in account's workspace: ``{"host", "source": "sign-in", "tenant_id"}``,
    the exact shape Desk's sign-in discovery writes, and only after the same check Desk
    makes (the host's public ``config.js`` declares this tenant). A file a person or an
    installer wrote is never touched, and Desk's next sign-in may replace this one.

No workspace known -> no file: Desk opens aitherium.com and finds the workspace itself
at sign-in. Nothing tenant-specific is baked in here and no secret is ever written.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

DESK_RELEASE_REPO = "Aitherium/awdesk"
DESK_RELEASES = f"https://github.com/{DESK_RELEASE_REPO}/releases"
APEX_HOST = "aitherium.com"
LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
TENANT_ID_RE = re.compile(r"^tnt_[a-z0-9_]{1,76}$")
_TENANT_DECL_RE = re.compile(r"window\.__AITHER_TENANT__\s*=\s*[\"']([a-z0-9-]{1,63})[\"']")
_SUMS_LINE_RE = re.compile(r"^\s*([0-9a-fA-F]{64})\s+\*?(\S+)\s*$")
_USER_AGENT = "awdk-desk-install/1"

#: Asset name pattern per platform key. The version group is always group 1.
ASSET_PATTERNS: dict[str, re.Pattern[str]] = {
    "windows-x64": re.compile(r"^Desk-(\d+\.\d+\.\d+)-windows-x64-setup\.exe$"),
    "macos-arm64": re.compile(r"^Desk-(\d+\.\d+\.\d+)-macos-arm64\.dmg$"),
    "macos-x64": re.compile(r"^Desk-(\d+\.\d+\.\d+)-macos-x64\.dmg$"),
    "linux-x64": re.compile(r"^Desk-(\d+\.\d+\.\d+)-linux-x86_64\.AppImage$"),
}

#: The uninstall-registry DisplayName of an installed app: ``Desk <version>`` (old),
#: ``Aither Desk`` (to 0.1.17) or ``Aither`` (since the product rename).
INSTALLED_DISPLAY_NAME_RE = re.compile(r"^(Aither( Desk)?|Desk)( \d|$)")


class DeskInstallError(RuntimeError):
    """A verification or installer failure -- the caller must exit non-zero."""


@dataclass(frozen=True)
class DeskAsset:
    """One verified-to-be-listed asset of the latest Desk release."""

    name: str
    version: str
    sha256: str

    @property
    def url(self) -> str:
        """The asset pinned to its release tag (never ``latest``)."""
        return f"{DESK_RELEASES}/download/v{self.version}/{self.name}"


# ---------------------------------------------------------------------------
# Tenant: which workspace is known, and the one update sentence
# ---------------------------------------------------------------------------


def tenant_config_path(homedir: Optional[Path] = None) -> Path:
    """``~/.aither/desk-tenant.json`` -- the file Desk reads on every platform."""
    return Path(homedir or Path.home()) / ".aither" / "desk-tenant.json"


def label_from_host(host: Any) -> Optional[str]:
    """``acme.aitherium.com`` -> ``acme``; anything that is not one label under the apex
    (the apex itself, a custom host, a port, a path) -> None."""
    if not isinstance(host, str):
        return None
    text = host.strip().lower()
    if text.startswith("https://"):
        text = text[len("https://"):].rstrip("/")
    suffix = f".{APEX_HOST}"
    if not text.endswith(suffix):
        return None
    label = text[: -len(suffix)]
    return label if LABEL_RE.match(label) else None


def label_from_tenant_id(tenant_id: Any) -> Optional[str]:
    """Identity's canonical id ``tnt_<label with - as _>`` -> the host label, or None."""
    if not isinstance(tenant_id, str) or not TENANT_ID_RE.match(tenant_id.strip().lower()):
        return None
    label = tenant_id.strip().lower()[4:].replace("_", "-")
    return label if LABEL_RE.match(label) else None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def account_tenant_id(homedir: Optional[Path] = None) -> Optional[str]:
    """The signed-in account's ``tenant_id`` from ``~/.aither/auth.json`` (active profile),
    or None. Read-only; never a token."""
    auth = _read_json(Path(homedir or Path.home()) / ".aither" / "auth.json")
    profiles = auth.get("profiles") if isinstance(auth.get("profiles"), dict) else {}
    prof = profiles.get(auth.get("active_profile") or "") or {}
    user = prof.get("user") if isinstance(prof, dict) else None
    if not isinstance(user, dict):
        return None
    tid = user.get("tenant_id")
    return tid.strip().lower() if isinstance(tid, str) and tid.strip() else None


def known_tenant_label(homedir: Optional[Path] = None) -> Optional[str]:
    """The workspace label this PC is known to belong to, or None.

    Desk's own tenant file first (it is what Desk opens), then the signed-in account's
    canonical tenant id. Used for WORDING only -- never to write a tenant file.
    """
    label = label_from_host(_read_json(tenant_config_path(homedir)).get("host"))
    if label:
        return label
    return label_from_tenant_id(account_tenant_id(homedir))


def setup_pc_url(label: Optional[str]) -> str:
    """``https://<label>.aitherium.com/setup-pc``; a placeholder host when unknown."""
    if label and LABEL_RE.match(label):
        return f"https://{label}.{APEX_HOST}/setup-pc"
    return f"https://<your-workspace>.{APEX_HOST}/setup-pc"


def desk_update_hint(label: Optional[str] = None, homedir: Optional[Path] = None) -> str:
    """The one sentence every surface prints when Desk is too old.

    ``label`` None -> looked up with :func:`known_tenant_label`.
    """
    if label is None:
        label = known_tenant_label(homedir)
    return ("update Aither from its own update notice, or re-run setup at "
            f"{setup_pc_url(label)}")


# ---------------------------------------------------------------------------
# Release: which asset, and is it the one we downloaded
# ---------------------------------------------------------------------------


def platform_key(system: Optional[str] = None, machine: Optional[str] = None) -> str:
    """``windows-x64`` | ``macos-arm64`` | ``macos-x64`` | ``linux-x64``."""
    import platform as _platform

    system = (system or _platform.system()).lower()
    machine = (machine or _platform.machine()).lower()
    if system.startswith("win") or (system == "" and sys.platform == "win32"):
        return "windows-x64"
    if system == "darwin":
        return "macos-arm64" if machine in ("arm64", "aarch64") else "macos-x64"
    return "linux-x64"


def parse_sums(text: str, key: str) -> Optional[DeskAsset]:
    """The first ``SHA256SUMS.txt`` line naming this platform's Desk asset, or None."""
    pattern = ASSET_PATTERNS.get(key)
    if pattern is None:
        return None
    for line in (text or "").splitlines():
        m = _SUMS_LINE_RE.match(line)
        if not m:
            continue
        hit = pattern.match(m.group(2))
        if hit:
            return DeskAsset(name=m.group(2), version=hit.group(1), sha256=m.group(1).lower())
    return None


def version_tuple(text: Any) -> Optional[tuple[int, int, int]]:
    """``0.1.15`` / ``v0.1.15`` / ``0.1.15.0`` -> (0, 1, 15); anything else -> None."""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", str(text or ""))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def sha256_file(path: Path) -> str:
    """Hex SHA256 of a file, streamed."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def http_get(url: str, timeout: float = 120.0) -> bytes:
    """GET a URL (redirects followed: GitHub release downloads redirect to a CDN)."""
    req = Request(url, headers={"User-Agent": _USER_AGENT})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        return None


def fetch_config_js(host: str, timeout: float = 8.0) -> Optional[str]:
    """A tenant host's PUBLIC ``config.js`` (no redirects, no credentials), or None."""
    opener = build_opener(_NoRedirect())
    req = Request(f"https://{host}/config.js", headers={"User-Agent": _USER_AGENT})
    try:
        with opener.open(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            body = resp.read(256 * 1024 + 1)
    except (HTTPError, URLError, OSError, ValueError):
        return None
    if len(body) > 256 * 1024:
        return None
    return body.decode("utf-8", errors="replace")


def host_declares_tenant(label: str, fetch: Callable[[str], Optional[str]] = fetch_config_js
                         ) -> bool:
    """True when ``https://<label>.aitherium.com/config.js`` declares tenant ``label``."""
    text = fetch(f"{label}.{APEX_HOST}")
    m = _TENANT_DECL_RE.search(text or "")
    return bool(m and m.group(1) == label)


# ---------------------------------------------------------------------------
# Windows: what is installed, is this process elevated
# ---------------------------------------------------------------------------


def installed_desk_windows() -> Optional[dict[str, Any]]:
    """``{"version": (x, y, z) | None, "exe": str}`` for an installed Desk, or None.

    The NSIS installer registers DisplayName ``Desk <version>``, ``Aither Desk`` (to 0.1.17)
    or ``Aither`` (the 2026-10-15 product rename) with
    an ``Uninstall Desk.exe`` uninstaller; BOTH are required so another product called
    Desk is not mistaken for it.
    """
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return None
    roots = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, sub in roots:
        try:
            root = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, name) as key:
                        vals = {}
                        for field in ("DisplayName", "UninstallString", "DisplayIcon",
                                      "DisplayVersion"):
                            try:
                                vals[field] = str(winreg.QueryValueEx(key, field)[0])
                            except OSError:
                                vals[field] = ""
                except OSError:
                    continue
                if not INSTALLED_DISPLAY_NAME_RE.match(vals["DisplayName"]):
                    continue
                if "Uninstall Desk.exe" not in vals["UninstallString"]:
                    continue
                exe = re.sub(r",\d+$", "", vals["DisplayIcon"]).replace('"', "")
                if not exe.lower().endswith("desk.exe"):
                    m = re.match(r'^"?(.*?)\\Uninstall Desk\.exe', vals["UninstallString"])
                    exe = str(Path(m.group(1)) / "Desk.exe") if m else ""
                return {"version": version_tuple(vals["DisplayVersion"]), "exe": exe}
    return None


def is_elevated() -> bool:
    """True when this Windows process holds an administrator token."""
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


def find_desk_exe(installed: Optional[dict[str, Any]]) -> str:
    """The installed Desk.exe, or ''."""
    if installed and installed.get("exe") and Path(installed["exe"]).is_file():
        return str(installed["exe"])
    local = os.environ.get("LOCALAPPDATA", "")
    default = Path(local) / "Programs" / "Desk" / "Desk.exe" if local else None
    return str(default) if default and default.is_file() else ""


def run_installer_windows(path: Path) -> int:
    """Run the NSIS installer silently, per user, and wait. Returns its exit code."""
    return subprocess.run([str(path), "/S"], check=False).returncode


# ---------------------------------------------------------------------------
# Tenant file writers
# ---------------------------------------------------------------------------


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_installer_tenant(label: str, name: str = "", homedir: Optional[Path] = None,
                           written_by: str = "adk deploy desktop") -> Path:
    """The setup page's shape for an explicit workspace. Replaces any existing file."""
    if not LABEL_RE.match(label or ""):
        raise ValueError(f"not a workspace label: {label!r}")
    clean = re.sub(r"\s+", " ", re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "", name or "")).strip(" .")
    host = f"{label}.{APEX_HOST}"
    data = {
        "schema": 1,
        "app": label,
        "name": clean or label.upper(),
        "host": host,
        "url": f"https://{host}",
        "api": "",
        "repo": "",
        "written_by": written_by,
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = tenant_config_path(homedir)
    _atomic_write_json(path, data)
    return path


def write_signin_tenant(label: str, tenant_id: str, homedir: Optional[Path] = None
                        ) -> Optional[Path]:
    """Desk's sign-in discovery shape. Never touches a file a person or installer wrote.

    Returns the path when written, None when an existing non-sign-in file was kept.
    """
    path = tenant_config_path(homedir)
    if path.exists() and _read_json(path).get("source") != "sign-in":
        return None
    _atomic_write_json(path, {"host": f"{label}.{APEX_HOST}", "source": "sign-in",
                              "tenant_id": tenant_id})
    return path


# ---------------------------------------------------------------------------
# The install
# ---------------------------------------------------------------------------


@dataclass
class DeskInstallResult:
    """What happened, for the caller to print."""

    asset: Optional[DeskAsset] = None
    installed: bool = False
    skipped_current: bool = False
    downloaded_to: Optional[Path] = None
    tenant_label: Optional[str] = None
    tenant_file: Optional[Path] = None
    tenant_note: str = ""
    started: bool = False


def resolve_and_write_tenant(explicit_label: Optional[str], homedir: Optional[Path] = None,
                             declares: Callable[[str], bool] = host_declares_tenant,
                             dry_run: bool = False) -> tuple[Optional[str], Optional[Path], str]:
    """Write the tenant file per the module contract. Returns (label, file, note)."""
    if explicit_label:
        label = explicit_label.strip().lower()
        if not LABEL_RE.match(label):
            raise DeskInstallError(f"--tenant must be one workspace label, got {explicit_label!r}")
        if dry_run:
            return label, tenant_config_path(homedir), "would write (explicit workspace)"
        return label, write_installer_tenant(label, homedir=homedir), "explicit workspace"
    tenant_id = account_tenant_id(homedir)
    label = label_from_tenant_id(tenant_id)
    if label and tenant_id and declares(label):
        if dry_run:
            return label, tenant_config_path(homedir), "would write (signed-in account)"
        wrote = write_signin_tenant(label, tenant_id, homedir=homedir)
        if wrote:
            return label, wrote, "signed-in account's workspace"
        return known_tenant_label(homedir), None, "existing tenant file kept"
    existing = known_tenant_label(homedir)
    if tenant_config_path(homedir).exists():
        return existing, None, "existing tenant file kept"
    return None, None, "no workspace known: Aither opens aitherium.com and finds it at sign-in"


def install_desk(*, explicit_label: Optional[str] = None, force: bool = False,
                 start: bool = True, dry_run: bool = False,
                 homedir: Optional[Path] = None, key: Optional[str] = None,
                 get: Callable[[str], bytes] = http_get,
                 installed_fn: Callable[[], Optional[dict[str, Any]]] = installed_desk_windows,
                 run_installer: Callable[[Path], int] = run_installer_windows,
                 declares: Callable[[str], bool] = host_declares_tenant,
                 elevated_fn: Callable[[], bool] = is_elevated,
                 launch: Callable[[str], None] = lambda exe: None,
                 log: Callable[[str], None] = print) -> DeskInstallResult:
    """Install (or update) Desk from its latest verified release. See the module doc.

    Raises:
        DeskInstallError: unreachable release, an unlisted asset, a checksum mismatch or
            a failed installer. Nothing is run after a failed verification.
    """
    key = key or platform_key()
    result = DeskInstallResult()
    try:
        sums = get(f"{DESK_RELEASES}/latest/download/SHA256SUMS.txt").decode("utf-8", "replace")
    except (HTTPError, URLError, OSError) as exc:
        raise DeskInstallError(
            f"could not read the Aither release ({DESK_RELEASES}): {exc}") from exc
    asset = parse_sums(sums, key)
    if asset is None:
        raise DeskInstallError(
            f"the latest Aither release lists no {key} asset with a SHA256 in SHA256SUMS.txt; "
            "refusing to install an unverified download")
    result.asset = asset
    windows = key.startswith("windows")

    current = installed_fn() if windows else None
    latest = version_tuple(asset.version)
    if (current and current.get("version") and latest and current["version"] >= latest
            and not force):
        result.skipped_current = True
        log(f"Aither {'.'.join(map(str, current['version']))} is already the latest; "
            "not reinstalled.")
    elif dry_run:
        log(f"[DRY RUN] download {asset.url}")
        log(f"[DRY RUN] verify SHA256 {asset.sha256}")
        if windows:
            log(f"[DRY RUN] install {asset.name} /S")
    else:
        work = Path(tempfile.mkdtemp(prefix="aither-desk-"))
        try:
            target = work / asset.name
            log(f"download {asset.url}")
            try:
                target.write_bytes(get(asset.url))
            except (HTTPError, URLError, OSError) as exc:
                raise DeskInstallError(f"could not download {asset.name}: {exc}") from exc
            actual = sha256_file(target)
            if actual != asset.sha256:
                target.unlink(missing_ok=True)
                raise DeskInstallError(
                    f"SHA256 mismatch for {asset.name} (expected {asset.sha256}, got {actual}); "
                    "the download was deleted and NOT run")
            log(f"verified SHA256 {actual}")
            if windows:
                log(f"install {asset.name} /S")
                code = run_installer(target)
                if code != 0:
                    raise DeskInstallError(f"the Aither installer exited with code {code}")
                result.installed = True
            else:
                keep = Path(homedir or Path.home()) / ".aither" / "desk" / asset.name
                keep.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(keep))
                if key.startswith("linux"):
                    keep.chmod(0o755)
                result.downloaded_to = keep
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # Tenant BEFORE the first start, so Desk opens the right workspace.
    label, file, note = resolve_and_write_tenant(explicit_label, homedir=homedir,
                                                 declares=declares, dry_run=dry_run)
    result.tenant_label, result.tenant_file, result.tenant_note = label, file, note

    if windows and start and not dry_run and result.installed:
        if elevated_fn():
            log("Running as administrator: Aither was installed but not started (it must not "
                "run with an administrator token). Start Aither from the Start menu.")
        else:
            exe = find_desk_exe(installed_fn())
            if exe:
                launch(exe)
                result.started = True
    return result
