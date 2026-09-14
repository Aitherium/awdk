"""adk doctor -- system health checks for ADK environments."""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("adk.doctor")

# ANSI helpers (re-used from setup_cli pattern)
_COLOR = hasattr(sys.stdout, "isatty") and sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _c(code: str, t: str) -> str:
    return f"\033[{code}m{t}\033[0m" if _COLOR else t


def _ok(msg: str) -> None:
    print(f"  [{'OK' if not _COLOR else _c('92', 'OK')}] {msg}")


def _warn(msg: str) -> None:
    print(f"  [{'!!' if not _COLOR else _c('93', '!!')}] {msg}")


def _fail(msg: str) -> None:
    print(f"  [{'--' if not _COLOR else _c('91', '--')}] {msg}")


def _run(cmd: list[str], timeout: int = 10) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def check_python() -> bool:
    v = sys.version_info
    ok = v >= (3, 10)
    label = f"Python {v.major}.{v.minor}.{v.micro}"
    if ok:
        _ok(label)
    else:
        _fail(f"{label} (requires >= 3.10)")
    return ok


def check_ollama() -> tuple[bool, list[str]]:
    ollama = shutil.which("ollama")
    if not ollama:
        _fail("Ollama: not installed")
        return False, []

    import urllib.request
    import urllib.error

    try:
        req = urllib.request.Request("http://localhost:11434/api/tags")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            models = [m["name"] for m in data.get("models", [])]
            _ok(f"Ollama: {len(models)} model(s) loaded")
            for m in models[:8]:
                print(f"         - {m}")
            return True, models
    except Exception:
        _warn("Ollama: installed but not responding on :11434")
        return False, []


def check_vllm() -> tuple[bool, list[str]]:
    import urllib.request
    import urllib.error

    ports = [8000, 8201, 8202, 8203, 8209]   # not 8200: that's media-forge, not an LLM

    # Add user-configured ports
    extra = os.environ.get("AITHER_VLLM_PORTS", "")
    if extra:
        for p_str in extra.split(","):
            p_str = p_str.strip()
            if p_str.isdigit() and int(p_str) not in ports:
                ports.append(int(p_str))

    found = []
    for port in ports:
        try:
            req = urllib.request.Request(f"http://localhost:{port}/v1/models")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
                models = [m["id"] for m in data.get("data", [])]
                found.extend(models)
                _ok(f"vLLM :{port}: {', '.join(models) or 'ready'}")
        except Exception:
            pass

    if not found:
        _fail("vLLM: no instances found on ports 8000, 8201-8203, 8209")
    return bool(found), found


def check_dgx() -> tuple[bool, list[str]]:
    """Check DGX Spark / remote vLLM endpoints."""
    import urllib.request
    import urllib.error

    dgx_url = os.environ.get("AITHER_DGX_URL", "")
    found = []

    # Check explicit URL
    if dgx_url:
        base = dgx_url.rstrip("/")
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        try:
            req = urllib.request.Request(f"{base}/models")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
                models = [m["id"] for m in data.get("data", [])]
                found.extend(models)
                _ok(f"DGX/Remote: {dgx_url} — {', '.join(models) or 'ready'}")
                return True, found
        except Exception:
            _warn(f"DGX/Remote: {dgx_url} — unreachable")
            return False, []

    # Auto-scan common DGX Spark addresses
    for host in ("spark.local", "192.168.0.33"):
        for port in (8000, 8120, 8209):
            try:
                req = urllib.request.Request(f"http://{host}:{port}/v1/models")
                with urllib.request.urlopen(req, timeout=2) as resp:
                    data = json.loads(resp.read())
                    models = [m["id"] for m in data.get("data", [])]
                    if models:
                        found.extend(models)
                        _ok(f"DGX Spark: {host}:{port} — {', '.join(models)}")
                        return True, found
            except Exception:
                pass

    # Not configured and not found. This is a perfectly normal state, but it
    # must still SAY so: run_doctor counts 11 checks, and a check that returns
    # falsy while printing nothing is invisible — the user is told "8/11" with
    # only one visible problem and no way to find the other two. A diagnostic
    # whose own summary disagrees with its output teaches people to ignore it.
    _warn("DGX/Remote: none configured (set AITHER_DGX_URL if you have one)")
    return False, []


def check_cloud_keys() -> bool:
    """Check for cloud API keys (Anthropic, OpenAI, DeepSeek)."""
    keys_found = []
    for name, env in [("Anthropic", "ANTHROPIC_API_KEY"), ("OpenAI", "OPENAI_API_KEY"),
                      ("DeepSeek", "DEEPSEEK_API_KEY")]:
        if os.environ.get(env):
            keys_found.append(name)

    # Also check saved config
    try:
        config_path = Path.home() / ".aither" / "config.json"
        if config_path.exists():
            saved = json.loads(config_path.read_text())
            if saved.get("reasoning_backend"):
                keys_found.append(f"reasoning:{saved['reasoning_backend']}")
    except Exception:
        pass

    if keys_found:
        _ok(f"Cloud APIs: {', '.join(keys_found)}")
        return True
    # Same rule as check_dgx: a counted check must be visible. Silent absence
    # is what made the summary unreconcilable with the screen.
    _warn("Cloud APIs: no keys set (ANTHROPIC_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY)")
    return False


def check_docker() -> bool:
    docker = shutil.which("docker")
    if not docker:
        _fail("Docker: not installed")
        return False

    version = _run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if not version:
        _warn("Docker: installed but daemon not running")
        return False

    # Check GPU runtime
    gpu_runtime = _run(["docker", "info", "--format", "{{.Runtimes}}"])
    has_gpu = gpu_runtime and "nvidia" in gpu_runtime.lower() if gpu_runtime else False
    gpu_label = " + NVIDIA GPU runtime" if has_gpu else ""
    _ok(f"Docker: {version}{gpu_label}")
    return True


def check_gpu() -> bool:
    from adk.setup_cli import detect_gpu

    gpu = detect_gpu()
    if gpu.vendor == "none":
        _warn("GPU: none detected")
        return False

    vram = gpu.vram_mb / 1024 if gpu.vram_mb else 0
    extra = ""
    if gpu.cuda_version:
        extra = f", CUDA {gpu.cuda_version}"
    _ok(f"GPU: {gpu.name} ({vram:.0f}GB{extra})")
    return True


def check_api_key() -> bool:
    key = os.environ.get("AITHER_API_KEY", "")
    if not key:
        saved = {}
        config_path = Path.home() / ".aither" / "config.json"
        if config_path.exists():
            try:
                saved = json.loads(config_path.read_text())
            except Exception:
                pass
        key = saved.get("api_key", "")

    if not key:
        _warn("API Key: not set (local-only mode)")
        return False

    # Try to validate against gateway
    import urllib.request
    import urllib.error

    try:
        req = urllib.request.Request(
            "https://gateway.aitherium.com/v1/auth/me",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            email = data.get("email", "authenticated")
            _ok(f"API Key: valid ({email})")
            return True
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _fail("API Key: invalid or expired")
        else:
            _warn(f"API Key: set but gateway returned HTTP {e.code}")
        return False
    except Exception:
        _warn("API Key: set but gateway unreachable")
        return False


def check_disk() -> bool:
    aither_dir = Path.home() / ".aither"
    aither_size = 0
    if aither_dir.exists():
        try:
            count = 0
            for f in aither_dir.rglob("*"):
                if f.is_file():
                    try:
                        aither_size += f.stat().st_size
                    except OSError:
                        pass
                count += 1
                if count > 5000:
                    break  # Enough for an estimate
        except Exception:
            pass

    hf_cache = Path.home() / ".cache" / "huggingface"
    hf_size = 0
    if hf_cache.exists():
        try:
            # Just count top-level to avoid slow traversal
            hf_size = sum(
                f.stat().st_size
                for f in hf_cache.iterdir()
                if f.is_file()
            )
        except Exception:
            pass

    def _human(b: int) -> str:
        for u in ("B", "KB", "MB", "GB"):
            if b < 1024:
                return f"{b:.1f}{u}"
            b /= 1024
        return f"{b:.1f}TB"

    _ok(f"Disk: ~/.aither = {_human(aither_size)}, HF cache = {_human(hf_size)}")
    return True


def check_version() -> bool:
    from adk import __version__

    _ok(f"ADK version: {__version__}")

    # Check PyPI for latest
    import urllib.request

    try:
        req = urllib.request.Request("https://pypi.org/pypi/awdk/json")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            latest = data.get("info", {}).get("version", "")
            if latest and latest != __version__:
                _warn(f"Update available: {latest} (you have {__version__})")
                print(f"         pip install --upgrade awdk")
            elif latest:
                _ok("Up to date")
    except Exception:
        pass  # Offline is fine

    return True


def check_packs() -> bool:
    """Check installed tool packs and their health."""
    packs_dir = Path.home() / ".aitheros" / "packs"
    if not packs_dir.is_dir():
        _ok("Packs: none installed (directory does not exist)")
        return True

    installed = []
    broken = []
    for child in sorted(packs_dir.iterdir()):
        if not child.is_dir():
            continue
        manifest_path = child / ".toolpack.yaml"
        if not manifest_path.exists():
            broken.append(child.name)
            continue
        # Verify YAML parses
        try:
            import yaml
            with open(manifest_path, "r", encoding="utf-8") as f:
                yaml.safe_load(f)
            installed.append(child.name)
        except ImportError:
            # yaml not available, file exists so count it
            installed.append(child.name)
        except Exception:
            broken.append(child.name)

    # Check entitlement cache for licensed status
    licensed_count = 0
    try:
        ent_path = Path.home() / ".aitheros" / "entitlement.json"
        if ent_path.exists():
            ent_data = json.loads(ent_path.read_text())
            licensed_packs = set(ent_data.get("licensed_packs", []))
            licensed_count = len(licensed_packs & set(installed))
    except Exception:
        pass

    unlicensed_count = len(installed) - licensed_count

    if broken:
        for b in broken:
            _fail(f"Packs: {b} — missing or invalid .toolpack.yaml")

    if installed:
        parts = [f"{len(installed)} installed"]
        if licensed_count:
            parts.append(f"{licensed_count} licensed")
        if unlicensed_count:
            parts.append(f"{unlicensed_count} unlicensed")
        _ok(f"Packs: {', '.join(parts)}")
        for name in installed[:10]:
            suffix = ""
            try:
                ent_path = Path.home() / ".aitheros" / "entitlement.json"
                if ent_path.exists():
                    ent_data = json.loads(ent_path.read_text())
                    if name in ent_data.get("licensed_packs", []):
                        suffix = " (licensed)"
            except Exception:
                pass
            print(f"         - {name}{suffix}")
        if len(installed) > 10:
            print(f"         ... and {len(installed) - 10} more")
    elif not broken:
        _ok("Packs: none installed")

    return not broken


#: The checks `cmd_doctor` counts, as (label, callable).
#:
#: Module-level on purpose: the summary prints "<passed>/<len(DOCTOR_CHECKS)>",
#: so a check that returns falsy WITHOUT printing anything is arithmetic the
#: user cannot reconcile with the screen — measured 2026-08-07 as "8/11 checks
#: passed" above 10 visible lines, with two of the three failures invisible.
#: tests/test_doctor_summary_reconciles.py iterates THIS list and asserts each
#: entry says something; keeping it inline in cmd_doctor let the test drift.
DOCTOR_CHECKS: list[tuple[str, object]] = [
    ("Python", check_python),
    ("Version", check_version),
    ("GPU", check_gpu),
    ("Docker", check_docker),
    ("Ollama", lambda: check_ollama()[0]),
    ("vLLM", lambda: check_vllm()[0]),
    ("DGX/Remote", lambda: check_dgx()[0]),
    ("API Key", check_api_key),
    ("Cloud APIs", check_cloud_keys),
    ("Disk", check_disk),
    ("Packs", check_packs),
]


def cmd_doctor(_args=None) -> int:
    """Run all health checks."""
    from adk import __version__

    print()
    print(f"  ADK Doctor v{__version__}")
    print(f"  {'=' * 40}")
    print()

    checks = 0
    passed = 0

    for name, fn in DOCTOR_CHECKS:
        checks += 1
        try:
            if fn():
                passed += 1
        except Exception as e:
            _fail(f"{name}: error ({e})")

    print()
    print(f"  {passed}/{checks} checks passed")

    if passed < checks:
        print()
        print("  Run 'adk setup' to fix common issues.")

    print()
    return 0
