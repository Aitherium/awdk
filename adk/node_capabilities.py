"""What this node can LEND to the platform, probed rather than claimed.

A node can lend more than a GPU: it can host MCP tools, run a relay, run agent loops,
execute tools, hold files, run batch jobs, compute embeddings, serve a small LLM, or
hold KV pages for a remote engine. :data:`KINDS` is that vocabulary; it mirrors the
platform's node-capability vocabulary (``AitherOS/config/node_capabilities.yaml``),
and a checker keeps the two identical.

Two separate questions, answered separately:

* **can it?**  :func:`detect` turns measured :class:`HostFacts` into
  ``{kind: {"available": bool, "detail": {...}}}``. Pure: tests drive it with a fake
  host. :func:`gather_facts` does the measuring, cheaply, without importing heavy
  packages and without spawning console windows on Windows (GPU facts come from
  :func:`adk.hardware_probe.detect_system`, which already runs ``nvidia-smi`` with
  ``CREATE_NO_WINDOW``).
* **will it?** :func:`lend_set` reads the owner's opt-in (``lend: [...]`` in
  ``~/.aither/config.yaml``, or ``AITHER_LEND=a,b``). Lending is OPT-IN: the default
  is the empty set, so a freshly enrolled node lends nothing.

:func:`advertise` combines them into the enrollment payload: ``capabilities`` is the
kinds that are both available and opted in; ``capability_detail`` carries every
kind's probe verdict so the owner can see why something is not lent.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

log = logging.getLogger("adk.node_capabilities")

#: The vocabulary. Order is the display order. Mirrors node_capabilities.yaml.
KINDS = (
    "mcp_host",
    "relay",
    "agent_loop",
    "tool_exec",
    "storage",
    "jobs",
    "embed_cpu",
    "embed_gpu",
    "llm_small",
    "llm_large",
    "kv_holder",
    "inference_gpu",
)

# Thresholds (the yaml's `probe` lines say the same numbers in words).
STORAGE_MIN_FREE_GIB = 10.0
EMBED_GPU_MIN_VRAM_MB = 2048
LLM_SMALL_MIN_RAM_GIB = 8.0
LLM_SMALL_MIN_VRAM_MB = 4096
INFERENCE_GPU_MIN_VRAM_MB = 8000
LLM_LARGE_MIN_VRAM_MB = 24000

_EMBEDDER_PACKAGES = ("sentence_transformers", "fastembed")
_TRUE = ("1", "true", "yes", "on")


@dataclass
class HostFacts:
    """What was measured on this host. Every field has a safe 'nothing' default."""

    cpu_cores: int = 0
    ram_gib: float = 0.0
    gpu_vendor: str = "none"
    gpu_name: str = ""
    gpu_vram_mb: int = 0
    data_dir: str = ""
    disk_free_gib: float = 0.0
    embedder: str = ""          # installed embedder package name, "" when none
    llm_runtime: str = ""       # "llama-server" | "ollama" | inference kind | ""
    llm_backend: bool = False   # an LLM is reachable for agent loops
    numpy: bool = False
    mcp_server_enabled: bool = False
    relay_enabled: bool = False


def _entry(available: bool, **detail: Any) -> Dict[str, Any]:
    return {"available": bool(available), "detail": detail}


def detect(facts: HostFacts) -> Dict[str, Dict[str, Any]]:
    """Every kind's verdict for ``facts``. Pure; one entry per :data:`KINDS` member."""
    f = facts
    gpu = f.gpu_vendor not in ("", "none") and f.gpu_vram_mb > 0
    runtime = bool(f.llm_runtime)
    out: Dict[str, Dict[str, Any]] = {
        "mcp_host": _entry(f.mcp_server_enabled, enabled=f.mcp_server_enabled),
        "relay": _entry(f.relay_enabled, enabled=f.relay_enabled),
        "agent_loop": _entry(f.llm_backend and f.ram_gib >= 4,
                             llm_backend=f.llm_backend, ram_gib=round(f.ram_gib, 1)),
        "tool_exec": _entry(f.cpu_cores >= 2 and f.ram_gib >= 2,
                            cpu_cores=f.cpu_cores, ram_gib=round(f.ram_gib, 1)),
        "storage": _entry(f.disk_free_gib >= STORAGE_MIN_FREE_GIB,
                          free_gib=round(f.disk_free_gib, 1)),
        "jobs": _entry(f.cpu_cores >= 4 and f.ram_gib >= 8,
                       cpu_cores=f.cpu_cores, ram_gib=round(f.ram_gib, 1)),
        "embed_cpu": _entry(bool(f.embedder) and f.ram_gib >= 4, embedder=f.embedder),
        "embed_gpu": _entry(bool(f.embedder) and gpu and f.gpu_vram_mb >= EMBED_GPU_MIN_VRAM_MB,
                            embedder=f.embedder, gpu=f.gpu_name, vram_mb=f.gpu_vram_mb),
        "llm_small": _entry(
            runtime and (f.ram_gib >= LLM_SMALL_MIN_RAM_GIB
                         or f.gpu_vram_mb >= LLM_SMALL_MIN_VRAM_MB),
            runtime=f.llm_runtime, ram_gib=round(f.ram_gib, 1), vram_mb=f.gpu_vram_mb),
        "llm_large": _entry(runtime and gpu and f.gpu_vram_mb >= LLM_LARGE_MIN_VRAM_MB,
                            runtime=f.llm_runtime, gpu=f.gpu_name, vram_mb=f.gpu_vram_mb),
        "kv_holder": _entry(f.numpy and f.ram_gib >= 4, ram_gib=round(f.ram_gib, 1)),
        "inference_gpu": _entry(runtime and gpu and f.gpu_vram_mb >= INFERENCE_GPU_MIN_VRAM_MB,
                                runtime=f.llm_runtime, gpu=f.gpu_name, vram_mb=f.gpu_vram_mb),
    }
    return {k: out[k] for k in KINDS}


def _as_kinds(values: Iterable[Any], *, source: str) -> List[str]:
    kept: List[str] = []
    for v in values:
        s = str(v).strip()
        if not s:
            continue
        if s not in KINDS:
            log.warning("lend: unknown capability kind %r in %s ignored", s[:40], source)
            continue
        if s not in kept:
            kept.append(s)
    return kept


def lend_set(saved: Optional[Mapping[str, Any]] = None,
             env: Optional[Mapping[str, str]] = None) -> List[str]:
    """The kinds the owner opted in to lend. Default: nothing (lending is opt-in).

    ``AITHER_LEND`` (comma-separated) wins over config ``lend:``; ``AITHER_LEND=``
    (set, empty) means lend nothing even if the config file says otherwise.
    """
    env = os.environ if env is None else env
    if "AITHER_LEND" in env:
        return _as_kinds(str(env.get("AITHER_LEND", "")).split(","), source="AITHER_LEND")
    raw = (saved or {}).get("lend") or []
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple)):
        log.warning("lend: config `lend` is %s, expected a list; lending nothing",
                    type(raw).__name__)
        return []
    return _as_kinds(raw, source="config lend")


def _enabled(saved: Mapping[str, Any], key: str, env: Mapping[str, str], env_key: str) -> bool:
    if str(env.get(env_key, "")).strip().lower() in _TRUE:
        return True
    section = saved.get(key)
    if isinstance(section, Mapping):
        return bool(section.get("enabled"))
    return section is True


def _find_spec(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _llama_server(data_dir: Path) -> str:
    if shutil.which("llama-server"):
        return "llama-server"
    root = data_dir / "llamacpp"
    if root.is_dir():
        for name in ("llama-server.exe", "llama-server"):
            if next(root.rglob(name), None) is not None:
                return "llama-server"
    if shutil.which("ollama"):
        return "ollama"
    return ""


def _kvholder_relay_live() -> bool:
    try:
        from adk.lend_routes import read_state
        return read_state() is not None
    except Exception:  # noqa: BLE001 -- an absent relay is just "not live"
        return False


def gather_facts(
    *,
    sysinfo: Any = None,
    saved: Optional[Mapping[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
    data_dir: Optional[str] = None,
    inference_kind: str = "",
    inference_ready: bool = False,
) -> HostFacts:
    """Measure this host. Best-effort: a failed probe reads as 'not available'.

    ``sysinfo`` is an :class:`adk.hardware_probe.SystemInfo` the caller already has
    (enrollment does); passing it avoids a second GPU probe.
    """
    env = os.environ if env is None else env
    if saved is None:
        try:
            from adk.config import load_saved_config
            saved = load_saved_config()
        except Exception as e:  # noqa: BLE001
            log.debug("saved config unreadable: %s", e)
            saved = {}
    if sysinfo is None:
        try:
            from adk.hardware_probe import detect_system
            sysinfo = detect_system()
        except Exception as e:  # noqa: BLE001
            log.debug("hardware probe failed: %s", e)
    ddir = Path(data_dir or env.get("AITHER_DATA_DIR") or (Path.home() / ".aither"))
    probe_dir = ddir if ddir.exists() else ddir.parent
    try:
        free_gib = shutil.disk_usage(str(probe_dir)).free / (1024 ** 3)
    except OSError:
        free_gib = 0.0
    embedder = next((p for p in _EMBEDDER_PACKAGES if _find_spec(p)), "")
    runtime = _llama_server(ddir)
    if not runtime and inference_ready and inference_kind not in ("", "none"):
        runtime = inference_kind
    llm_backend = bool(inference_ready or saved.get("api_key") or env.get("AITHER_API_KEY"))
    return HostFacts(
        cpu_cores=int(getattr(sysinfo, "cpu_cores", 0) or os.cpu_count() or 0),
        ram_gib=float(getattr(sysinfo, "ram_gb", 0.0) or 0.0),
        gpu_vendor=str(getattr(sysinfo, "gpu_vendor", "none") or "none"),
        gpu_name=str(getattr(sysinfo, "gpu_name", "") or ""),
        gpu_vram_mb=int(getattr(sysinfo, "gpu_vram_mb", 0) or 0),
        data_dir=str(ddir),
        disk_free_gib=free_gib,
        embedder=embedder,
        llm_runtime=runtime,
        llm_backend=llm_backend,
        numpy=_find_spec("numpy"),
        mcp_server_enabled=_enabled(saved, "mcp_server", env, "AITHER_MCP_SERVER_ENABLED"),
        relay_enabled=(_enabled(saved, "relay", env, "AITHER_RELAY_ENABLED")
                       or _kvholder_relay_live()),
    )


def advertise(
    facts: HostFacts,
    *,
    saved: Optional[Mapping[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """The registration fields: what is lent, and every kind's verdict.

    ``capabilities`` = available AND opted in. A kind the owner opted in but the
    probe refused is NOT advertised; its detail says ``lent: false`` with the reason.
    """
    verdicts = detect(facts)
    opted = set(lend_set(saved, env))
    lent = [k for k in KINDS if k in opted and verdicts[k]["available"]]
    detail: Dict[str, Dict[str, Any]] = {}
    for k in KINDS:
        v = dict(verdicts[k])
        v["lent"] = k in lent
        if k in opted and k not in lent:
            v["reason"] = "opted in, probe says unavailable"
        detail[k] = v
    return {"capabilities": lent, "capability_detail": detail}


def advertise_this_node(**gather_kwargs: Any) -> Dict[str, Any]:
    """:func:`gather_facts` + :func:`advertise` with the same saved config."""
    saved = gather_kwargs.pop("saved", None)
    env = gather_kwargs.pop("env", None)
    if saved is None:
        try:
            from adk.config import load_saved_config
            saved = load_saved_config()
        except Exception:  # noqa: BLE001
            saved = {}
    facts = gather_facts(saved=saved, env=env, **gather_kwargs)
    return advertise(facts, saved=saved, env=env)
