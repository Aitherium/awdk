"""FreeLLMAPI as awdk's NON-LOCAL route -- "Free cloud (your own free-tier keys)".

FreeLLMAPI (https://github.com/tashfeenahmed/freellmapi, MIT, (c) Tashfeen Ahmed)
is a self-hosted proxy that puts the free tiers of many hosted model providers
behind ONE OpenAI-compatible ``/v1`` endpoint, with routing, failover and per-key
quota tracking. awdk does not vendor it: it detects one the user runs, and
``adk free-cloud up`` starts the upstream's published image at a pinned digest.

Bring your own keys, always. Each user runs their own FreeLLMAPI with their own
provider accounts, and those providers' terms apply to traffic proxied through it
(upstream: "for personal experimentation and learning, not production"). So:

* it is OFF until the user turns it on (``adk free-cloud enable`` or
  ``ADK_FREELLMAPI=1``) -- detection alone never routes a turn to it;
* routing order is local model first, this second, paid / BYO API last;
* provider keys never pass through awdk at all. They live in FreeLLMAPI's own
  encrypted store (its dashboard). awdk holds ONE credential: the proxy's unified
  key, read from ``FREELLMAPI_API_KEY`` or ``adk keys set freellmapi`` -- a local
  file, never a synced setting and never logged.

Settings (enabled flag, URL, preferred models) live in ``~/.aither/mods.json`` under
``freellmapi`` -- the file awsettings' ``mods`` domain already syncs across a
user's machines. Env vars override the file.

The unified key is only ever sent to a LOOPBACK FreeLLMAPI (127.0.0.0/8, ::1,
localhost). A URL can arrive from another machine through that sync, so a
non-loopback URL is ignored unless ``allow_remote`` is set on THIS machine
(``adk free-cloud enable --allow-remote`` -> ``~/.aither/freellmapi/local.json``, or
``ADK_FREELLMAPI_ALLOW_REMOTE=1``). ``allow_remote`` is never read from the synced file.
"""

from __future__ import annotations

import json
import logging
import os
import ipaddress
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

logger = logging.getLogger("adk.freellmapi")

LABEL = "Free cloud (your own free-tier keys)"
UPSTREAM = "https://github.com/tashfeenahmed/freellmapi"
TOS_URL = (UPSTREAM + "/blob/main/docs/en/architecture/00-high-level-index.md"
           "#terms-of-service-review")

#: The published image, pinned. The tag is a label for humans; the DIGEST is the
#: pin (a tag can be re-pointed upstream, a digest cannot).
IMAGE = "ghcr.io/tashfeenahmed/freellmapi"
IMAGE_VERSION = "v0.16.2"
IMAGE_DIGEST = "sha256:8ced615ff842bc5772666f9a00de56cecc5cd5bf2b6d0df22d75a7a5a8a955e9"
PINNED_IMAGE = f"{IMAGE}@{IMAGE_DIGEST}"

#: 3001 = the Docker image / server install; 31415 = the desktop app.
DEFAULT_PORTS: tuple[int, ...] = (3001, 31415)
CONTAINER_NAME = "aither-freellmapi"
#: The upstream compose file's volume name, so a user who also tried upstream's
#: compose sees the same data. ``down`` never removes it: it holds their keys.
VOLUME = "freellmapi-data"
DATA_MOUNT = "/app/server/data"

ENV_ENABLED = "ADK_FREELLMAPI"
ENV_URL = "ADK_FREELLMAPI_URL"
ENV_MODELS = "ADK_FREELLMAPI_MODELS"
ENV_KEY = "FREELLMAPI_API_KEY"
ENV_ALLOW_REMOTE = "ADK_FREELLMAPI_ALLOW_REMOTE"
#: Tests only: the mods file awdk reads its settings from.
ENV_MODS_FILE = "ADK_MODS_FILE"
SETTINGS_KEY = "freellmapi"
KEY_STORE_NAME = "freellmapi"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass
class FreeLLMAPISettings:
    """What the user chose. ``enabled`` is the only thing that makes it route."""

    enabled: bool = False
    url: str = ""
    preferred_models: list[str] = field(default_factory=list)
    #: May the unified key go to a NON-loopback URL? Machine-local only.
    allow_remote: bool = False


def local_settings_path() -> Path:
    """Machine-local settings (never synced): ``~/.aither/freellmapi/local.json``."""
    return Path.home() / ".aither" / "freellmapi" / "local.json"


def _local_allow_remote() -> bool:
    try:
        data = json.loads(local_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("allow_remote") is True


def set_allow_remote(allowed: bool) -> Path:
    """Write ``allow_remote`` to THIS machine's local file (never the synced one)."""
    p = local_settings_path()
    data: dict = {}
    try:
        loaded = json.loads(p.read_text(encoding="utf-8"))
        data = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        data = {}
    data["allow_remote"] = bool(allowed)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


def is_loopback(url: str) -> bool:
    """127.0.0.0/8, ::1 or ``localhost`` -- the only hosts the unified key goes to by default."""
    try:
        host = (urlsplit(url if "://" in url else "http://" + url).hostname or "").lower()
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def mods_path() -> Path:
    """``~/.aither/mods.json`` (``ADK_MODS_FILE`` overrides, for tests)."""
    override = (os.environ.get(ENV_MODS_FILE) or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".aither" / "mods.json"


def _read_mods(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.debug("mods file %s unreadable: %s", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def _as_models(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        return []
    return [str(m).strip() for m in value if str(m).strip()]


def load_settings(env: Optional[dict] = None, path: Optional[Path] = None) -> FreeLLMAPISettings:
    """Settings from ``mods.json`` ``freellmapi``, each field overridable by env."""
    env = os.environ if env is None else env
    section = _read_mods(path or mods_path()).get(SETTINGS_KEY)
    section = section if isinstance(section, dict) else {}
    s = FreeLLMAPISettings(
        enabled=section.get("enabled") is True,
        url=str(section.get("url") or "").strip(),
        preferred_models=_as_models(section.get("preferred_models")),
    )
    flag = (env.get(ENV_ENABLED) or "").strip().lower()
    if flag in _TRUE:
        s.enabled = True
    elif flag in _FALSE:
        s.enabled = False
    if (env.get(ENV_URL) or "").strip():
        s.url = env[ENV_URL].strip()
    if (env.get(ENV_MODELS) or "").strip():
        s.preferred_models = _as_models(env[ENV_MODELS])
    # Deliberately NOT from `section`: the synced file must not be able to widen
    # where this machine's key is sent.
    s.allow_remote = ((env.get(ENV_ALLOW_REMOTE) or "").strip().lower() in _TRUE
                      or _local_allow_remote())
    return s


def save_settings(path: Optional[Path] = None, **changes: Any) -> Path:
    """Write ``freellmapi.<field>`` into mods.json, keeping every other key.

    Atomic (temp file + replace). Refuses to overwrite a file that is not JSON --
    a malformed mods.json is the user's to repair, not ours to replace.
    """
    target = path or mods_path()
    allowed = {"enabled", "url", "preferred_models"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"not a freellmapi setting: {', '.join(sorted(unknown))}")
    data: dict = {}
    if target.exists():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ValueError(f"{target} is not valid JSON; fix it first ({e})") from e
        if not isinstance(data, dict):
            raise ValueError(f"{target} is not a JSON object")
    section = data.get(SETTINGS_KEY)
    section = dict(section) if isinstance(section, dict) else {}
    section.update(changes)
    data[SETTINGS_KEY] = section
    data.setdefault("version", 1)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target


def resolve_api_key(env: Optional[dict] = None, keys_file: Optional[Path] = None) -> str:
    """The proxy's unified key: ``FREELLMAPI_API_KEY``, else ``adk keys set freellmapi``.

    Local only. Never returned in any listing, never logged, never synced.
    """
    env = os.environ if env is None else env
    key = (env.get(ENV_KEY) or "").strip()
    if key:
        return key
    kf = keys_file or (Path.home() / ".aither" / "provider_keys.json")
    try:
        data = json.loads(kf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get(KEY_STORE_NAME) or "").strip() if isinstance(data, dict) else ""


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


@dataclass
class Detection:
    """What a probe found. ``found`` = a FreeLLMAPI answered; ``authorized`` = our key works."""

    found: bool
    url: str = ""
    source: str = "none"  # configured | port | none
    authorized: bool = False
    models: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def base_url(self) -> str:
        """The OpenAI-compatible base (``<url>/v1``)."""
        return v1_url(self.url) if self.url else ""


def v1_url(url: str) -> str:
    """``http://h:3001`` or ``http://h:3001/v1`` -> ``http://h:3001/v1``."""
    u = url.rstrip("/")
    return u if u.endswith("/v1") else u + "/v1"


def origin_url(url: str) -> str:
    """``http://h:3001/v1`` -> ``http://h:3001``."""
    u = url.rstrip("/")
    return u[:-3] if u.endswith("/v1") else u


def ignored_urls(settings: FreeLLMAPISettings) -> list[str]:
    """Why a configured URL is NOT probed (a status line each), or ``[]``."""
    if settings.url and not settings.allow_remote and not is_loopback(settings.url):
        return [f"ignored {origin_url(settings.url)}: not loopback, so the unified key is "
                "not sent there; allow it on THIS machine with "
                "`adk free-cloud enable --allow-remote` (allow_remote is never synced)"]
    return []


def candidate_urls(settings: FreeLLMAPISettings) -> list[tuple[str, str]]:
    """``[(origin, source)]``: the configured URL first, then the default loopback ports.

    A non-loopback configured URL is dropped unless ``allow_remote`` (see ignored_urls).
    """
    out: list[tuple[str, str]] = []
    if settings.url and not ignored_urls(settings):
        out.append((origin_url(settings.url), "configured"))
    for port in DEFAULT_PORTS:
        u = f"http://127.0.0.1:{port}"
        if all(u != o for o, _ in out):
            out.append((u, "port"))
    return out


def _model_ids(payload: Any) -> list[str]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    ids = []
    for m in data:
        mid = m.get("id") if isinstance(m, dict) else m
        if isinstance(mid, str) and mid:
            ids.append(mid)
    return ids


def _is_ping(resp: Any) -> bool:
    """FreeLLMAPI's ``GET /api/ping`` answers ``{"status": "ok", "timestamp": ...}``."""
    try:
        body = resp.json()
    except ValueError:
        return False
    return (resp.status_code == 200 and isinstance(body, dict)
            and body.get("status") == "ok" and "timestamp" in body)


async def probe(origin: str, api_key: str = "", timeout: float = 2.0,
                available_only: bool = False, client: Any = None,
                allow_remote: bool = False) -> Detection:
    """Probe ONE origin. Identifies FreeLLMAPI by its ping shape AND a /v1/models
    that answers 200 or an OpenAI-shaped 401 -- a bare 200 on :3001 is what every
    dev web server answers, and adopting one of those would fail every chat."""
    if not allow_remote and not is_loopback(origin):
        return Detection(False, origin, detail="not loopback and allow_remote is off")
    try:
        import httpx
    except ImportError:
        return Detection(False, origin, detail="httpx not installed")
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=timeout)
    try:
        try:
            ping = await client.get(f"{origin}/api/ping")
        except Exception as e:  # noqa: BLE001 -- unreachable is a normal answer
            return Detection(False, origin, detail=f"unreachable ({type(e).__name__})")
        if not _is_ping(ping):
            return Detection(False, origin,
                             detail=f"/api/ping is not FreeLLMAPI's ({ping.status_code})")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        url = f"{origin}/v1/models" + ("?available=true" if available_only else "")
        try:
            resp = await client.get(url, headers=headers)
        except Exception as e:  # noqa: BLE001
            return Detection(False, origin, detail=f"/v1/models unreachable ({type(e).__name__})")
        if resp.status_code == 200:
            ids = _model_ids(resp.json())
            return Detection(True, origin, authorized=True, models=ids,
                             detail=f"{len(ids)} models")
        if resp.status_code in (401, 403):
            why = "no unified key set" if not api_key else "unified key rejected"
            return Detection(True, origin, authorized=False,
                             detail=f"{why} (/v1/models {resp.status_code})")
        return Detection(False, origin, detail=f"/v1/models answered {resp.status_code}")
    finally:
        if own:
            await client.aclose()


async def detect(settings: Optional[FreeLLMAPISettings] = None, api_key: Optional[str] = None,
                 timeout: float = 2.0, available_only: bool = False,
                 client: Any = None) -> Detection:
    """First FreeLLMAPI that answers: configured URL, then :3001, then :31415."""
    settings = settings or load_settings()
    key = resolve_api_key() if api_key is None else api_key
    tried = []
    for origin, source in candidate_urls(settings):
        d = await probe(origin, key, timeout=timeout, available_only=available_only,
                        client=client, allow_remote=settings.allow_remote)
        if d.found:
            d.source = source
            return d
        tried.append(f"{origin}: {d.detail}")
    return Detection(False, detail="; ".join(ignored_urls(settings) + tried))


def detect_sync(**kw: Any) -> Detection:
    """``detect`` for synchronous callers (the CLI)."""
    import asyncio
    return asyncio.run(detect(**kw))


#: Router entries FreeLLMAPI always lists, keyed or not. They are not models.
ROUTER_ENTRIES = frozenset({"auto", "fusion"})


def callable_models(models: list[str]) -> list[str]:
    """Catalog models only -- ``auto``, ``fusion`` and ``auto:<profile>`` removed.

    Against ``/v1/models?available=true`` this is "what a connected provider key can
    serve now". Empty means the user has added no working provider key yet, and a
    router that adopted the proxy anyway would fail every turn.
    """
    return [m for m in models if m not in ROUTER_ENTRIES and not m.startswith("auto:")]


def pick_model(models: list[str], preferred: list[str]) -> str:
    """The first preferred model the proxy serves, else ``auto`` (its own router)."""
    served = set(models)
    for m in preferred:
        if m in served:
            return m
    return "auto"


# ---------------------------------------------------------------------------
# Container lifecycle (``adk free-cloud up|down``) -- the pack's install path
# ---------------------------------------------------------------------------

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def container_runtime() -> Optional[str]:
    """``docker``, else ``podman``, else None."""
    for exe in ("docker", "podman"):
        if shutil.which(exe):
            return exe
    return None


def env_file_path() -> Path:
    """Where the proxy's ENCRYPTION_KEY lives (mode 600). Created once, never rotated.

    Losing it makes every stored provider key undecryptable, so ``down`` never
    touches it and ``up`` never regenerates an existing one.
    """
    return Path.home() / ".aither" / "freellmapi" / "freellmapi.env"


def ensure_env_file(path: Optional[Path] = None) -> tuple[Path, bool]:
    """Create the env file if absent. Returns (path, created)."""
    p = path or env_file_path()
    if p.exists():
        return p, False
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"ENCRYPTION_KEY={secrets.token_hex(32)}\nPORT=3001\n", encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(p, 0o600)
    return p, True


def _run(run: Runner, args: list[str]) -> "subprocess.CompletedProcess[str]":
    return run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
               timeout=600)


def container_state(runtime: str, run: Runner = subprocess.run) -> Optional[dict]:
    """``{"running": bool, "image": str}`` for our container, or None when absent."""
    r = _run(run, [runtime, "inspect", "--format",
                   "{{.State.Running}}|{{.Config.Image}}", CONTAINER_NAME])
    if r.returncode != 0:
        return None
    running, _, image = (r.stdout or "").strip().partition("|")
    return {"running": running.strip().lower() == "true", "image": image.strip()}


def up(port: int = 3001, runtime: Optional[str] = None, replace: bool = False,
       env_file: Optional[Path] = None, run: Runner = subprocess.run) -> tuple[int, str]:
    """Start the pinned image. Idempotent: running -> no-op, stopped -> start.

    ``replace`` recreates the container (e.g. after a pin bump); the named volume,
    and so the user's keys, survive it.
    """
    rt = runtime or container_runtime()
    if not rt:
        return 1, ("Neither docker nor podman found. Install one, or install the "
                   f"FreeLLMAPI desktop app ({UPSTREAM}/releases) -- awdk detects it on :31415.")
    state = container_state(rt, run)
    if state and not replace:
        if state["running"]:
            note = "" if state["image"] == PINNED_IMAGE else (
                f" (image {state['image']}; `adk free-cloud up --replace` moves it to "
                f"{IMAGE_VERSION})")
            return 0, f"{CONTAINER_NAME} already running{note}"
        r = _run(run, [rt, "start", CONTAINER_NAME])
        if r.returncode != 0:
            return 1, f"{rt} start {CONTAINER_NAME} failed: {(r.stderr or '').strip()[:300]}"
        return 0, f"started existing {CONTAINER_NAME}"
    if state and replace:
        r = _run(run, [rt, "rm", "-f", CONTAINER_NAME])
        if r.returncode != 0:
            return 1, f"could not remove old {CONTAINER_NAME}: {(r.stderr or '').strip()[:300]}"
    envf, _created = ensure_env_file(env_file)
    args = [
        rt, "run", "-d", "--name", CONTAINER_NAME, "--restart", "unless-stopped",
        # Loopback only, as upstream's compose does: the proxy is single-user and
        # guarded only by its unified key.
        "-p", f"127.0.0.1:{int(port)}:3001",
        "-v", f"{VOLUME}:{DATA_MOUNT}",
        "--env-file", str(envf),
        "-e", "NODE_ENV=production",
        "--label", "aither.pack=free-cloud",
        PINNED_IMAGE,
    ]
    r = _run(run, args)
    if r.returncode != 0:
        return 1, f"{rt} run failed: {(r.stderr or '').strip()[:400]}"
    return 0, (f"started {CONTAINER_NAME} ({IMAGE}:{IMAGE_VERSION}) on "
               f"http://127.0.0.1:{int(port)}")


def down(runtime: Optional[str] = None, run: Runner = subprocess.run) -> tuple[int, str]:
    """Remove the container. NEVER the volume (the user's keys) or the env file."""
    rt = runtime or container_runtime()
    if not rt:
        return 1, "neither docker nor podman found"
    if container_state(rt, run) is None:
        return 0, f"{CONTAINER_NAME} is not installed"
    r = _run(run, [rt, "rm", "-f", CONTAINER_NAME])
    if r.returncode != 0:
        return 1, f"{rt} rm failed: {(r.stderr or '').strip()[:300]}"
    return 0, (f"removed {CONTAINER_NAME}; your keys stay in volume {VOLUME!r} and "
               f"{env_file_path()} (delete both yourself only if you mean to lose them)")
