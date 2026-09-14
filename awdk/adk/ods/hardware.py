"""Host hardware classification — a port of ODS `scripts/classify-hardware.sh`.

The upstream script is bash wrapping a Python heredoc, so unlike
`select-model.py` it cannot simply be imported. It is vendored verbatim as
`_upstream_classify.sh` and this module is a faithful PORT of its embedded
Python. `tests/test_classify_differential.py` executes the vendored original as
the reference for every decision here, so drift fails a test rather than
shipping.

WHY A PORT AND NOT A FRESH IMPLEMENTATION
-----------------------------------------
The first version of this module was written from the JSON data files without
checking whether upstream already had a classifier. It did, and the two
disagreed in four ways that matter:

  1. First-pattern-wins instead of LONGEST-pattern-wins, which is upstream's
     explicit guard against `"RX 7900 XT"` matching an `"RX 7900 XTX"` host.
  2. No `device_id` matching at all (upstream tries device ids first, with
     VRAM-proximity as the tiebreak).
  3. GPU name only, ignoring the CPU name — which silently broke the single
     most important device in the database: `RYZEN AI MAX+ 395` is a *CPU*
     string, so a real Strix Halo host would have missed its known_gpus entry
     and been sized from a bogus discrete-VRAM reading.
  4. Compose overlays looked up from `hardware-classes.json` by
     platform+vendor+VRAM. Upstream keys them off the BACKEND
     (`OVERLAY_MAP`) with one macOS override; upstream's own contract test
     (`test-overlay-map-coherence.sh`) asserts hardware-classes.json is a
     declarative MIRROR of that map, not an independent source. The divergence
     was visible in practice: a Windows NVIDIA host got no overlays at all.

WHAT THE TIER IS FOR
--------------------
Before this module, `LLMFitClient.recommend_config()` passed `tier=None`, which
the resolver pins to `"1"`. Upstream's `arch_policy_model()` only fires the
Spark/GB10 substitution when `tier == NV_ULTRA and host_arch == arm64`, so that
branch was unreachable and a Grace-Blackwell host would have been handed the
coder-next model that emits all-`?` tokens on that backend.
"""

from __future__ import annotations

import logging
import platform as _platform
from dataclasses import dataclass, field
from typing import Any

from .data import load_gpu_database
from .model_types import OdsError

logger = logging.getLogger("adk.ods.hardware")

# Verbatim from the vendored script. Overlays are keyed on the resolved BACKEND,
# not on the platform/vendor probe — see divergence 4 in the module docstring.
OVERLAY_MAP: dict[str, tuple[str, ...]] = {
    "amd": ("docker-compose.base.yml", "docker-compose.amd.yml"),
    "nvidia": ("docker-compose.base.yml", "docker-compose.nvidia.yml"),
    "apple": ("docker-compose.base.yml", "docker-compose.apple.yml"),
    "cpu": ("docker-compose.base.yml", "docker-compose.cpu.yml"),
}
MACOS_APPLE_OVERLAYS = (
    "docker-compose.base.yml",
    "installers/macos/docker-compose.macos.yml",
)
FALLBACK_OVERLAYS = ("docker-compose.base.yml",)

# Upstream's unmatched-host answer: cpu / T1 / vram, class id "unknown".
DEFAULT_BACKEND = "cpu"
DEFAULT_TIER = "T1"


@dataclass(frozen=True)
class HostClass:
    """Resolved hardware classification — mirrors the script's output contract.

    Field names follow the script's JSON keys where they exist (`id`, `label`,
    `backend`, `tier`, `compose_overlays`, `bandwidth_gbps`, `memory_source`,
    `gpu_label`); `memory_type`, `vram_mb`, `source` and `matched_patterns` are
    additions this SDK needs to feed the resolver and to debug a match.
    """

    id: str
    """Class id from the vendored data (e.g. 'nvidia_pro'), or 'unknown'."""

    label: str
    """Human-readable device/class label."""

    backend: str
    """ODS backend key: nvidia | amd | apple | cpu."""

    tier: str
    """ODS tier token (T0..T4, NV_ULTRA, SH_LARGE, SH_COMPACT). Feeds the
    resolver's arch-policy guard — see module docstring."""

    memory_type: str
    """discrete | unified — decides which memory envelope upstream applies."""

    vram_mb: int
    """Device memory in MB as classified (known_gpus can CORRECT a bad probe)."""

    compose_overlays: tuple[str, ...] = ()
    """Compose overlay files, from OVERLAY_MAP keyed on `backend`."""

    bandwidth_gbps: int = 0
    """Memory bandwidth: device specs, then the name table, then the backend default."""

    memory_source: str = "vram"
    """vram | ram — where this class's memory figure comes from."""

    gpu_label: str = ""
    """known_gpus label, empty for a heuristic or unmatched host."""

    source: str = "unknown"
    """Which rung answered: known_gpu | heuristic_class | unknown."""

    matched_patterns: tuple[str, ...] = field(default=())
    """Name patterns that matched, when source == known_gpu (for debugging)."""


def current_platform_id() -> str:
    """Map the running platform to an ODS ``platform_id``.

    WSL reports ``Linux`` from ``platform.system()``, so it is detected from the
    kernel release string the way ODS's own installer does.
    """
    system = _platform.system().lower()
    if system == "linux":
        release = _platform.release().lower()
        return "wsl" if ("microsoft" in release or "wsl" in release) else "linux"
    if system == "darwin":
        return "macos"
    if system == "windows":
        return "windows"
    return "unknown"


def _match_known_gpu(
    db: dict[str, Any], device_id: str, combined_name: str, vram_mb: int
) -> dict[str, Any] | None:
    """Pass 1, ported verbatim from the vendored script.

    Device id and name may each match; when both do, the LONGEST matching name
    pattern wins (upstream's guard against `RX 7900 XT` matching an
    `RX 7900 XTX` host). A device-id-only match is tie-broken by VRAM proximity,
    preferring the SMALLEST card when VRAM is unknown — under-provisioning is
    safe, over-provisioning crashes the model loader.
    """
    selected: dict[str, Any] | None = None
    best_name_len = 0
    best_id_vram_diff: float | None = None

    for entry in db.get("known_gpus") or []:
        match = entry.get("match") or {}
        dev_ids = [str(d).lower() for d in match.get("device_ids") or []]
        id_matched = device_id.lower() in dev_ids if device_id else False

        patterns = match.get("name_patterns") or []
        matched_patterns = (
            [p for p in patterns if str(p).lower() in combined_name]
            if combined_name and patterns
            else []
        )
        name_matched = len(matched_patterns) > 0
        match_len = max((len(str(p)) for p in matched_patterns), default=0)

        if id_matched and name_matched:
            if match_len > best_name_len:
                selected = {**entry, "_hits": matched_patterns}
                best_name_len = match_len
        elif id_matched and best_name_len == 0:
            entry_vram = (entry.get("specs") or {}).get("memory_mb", 0) or 0
            if vram_mb > 0:
                diff: float = abs(entry_vram - vram_mb)
            else:
                diff = entry_vram if entry_vram > 0 else float("inf")
            if best_id_vram_diff is None or diff < best_id_vram_diff:
                selected = {**entry, "_hits": []}
                best_id_vram_diff = diff
        elif name_matched and not selected:
            selected = {**entry, "_hits": matched_patterns}
            best_name_len = match_len

    return selected


def _match_heuristic(
    db: dict[str, Any], vendor: str, memory_type: str, vram_mb: int, ram_mb: int
) -> dict[str, Any] | None:
    """Pass 2, ported verbatim: first satisfied `heuristic_classes` entry.

    An ABSENT key in `match` means "any" (upstream tests `if m_vendor and ...`),
    and comparisons are exact strings — no normalisation. File order is
    significant (largest band first) and is not re-sorted.
    """
    for entry in db.get("heuristic_classes") or []:
        match = entry.get("match") or {}

        m_vendor = match.get("vendor", "")
        if m_vendor and m_vendor != vendor:
            continue
        m_memtype = match.get("memory_type", "")
        if m_memtype and m_memtype != memory_type:
            continue
        min_vram = match.get("min_vram_mb", -1)
        if min_vram >= 0 and vram_mb < min_vram:
            continue
        max_vram = match.get("max_vram_mb", -1)
        if max_vram >= 0 and vram_mb > max_vram:
            continue
        min_ram = match.get("min_ram_mb", -1)
        if min_ram >= 0 and ram_mb < min_ram:
            continue
        return entry
    return None


def _bandwidth(
    db: dict[str, Any], selected: dict[str, Any] | None, vendor: str,
    gpu_name: str, cpu_name: str,
) -> int:
    """Device specs → `known_gpu_bandwidth` name table → backend default."""
    bandwidth = 0
    if selected and "specs" in selected:
        bandwidth = (selected["specs"] or {}).get("bandwidth_gbps", 0) or 0

    if not bandwidth and gpu_name:
        vendor_bw = (db.get("known_gpu_bandwidth") or {}).get(vendor) or {}
        for bw_name, bw_val in vendor_bw.items():
            name_key = str(bw_name).lower()
            if name_key in gpu_name.lower() or name_key in cpu_name.lower():
                bandwidth = bw_val
                break

    if not bandwidth:
        backend_key_map = {"nvidia": "cuda", "amd": "rocm", "apple": "metal"}
        key = backend_key_map.get(vendor, "cpu_x86")
        bandwidth = ((db.get("defaults") or {}).get("bandwidth_gbps") or {}).get(key, 0)
    return int(bandwidth or 0)


def classify_host(
    gpu_name: str | None = None,
    vendor: str | None = None,
    memory_type: str | None = None,
    vram_mb: int = 0,
    ram_gb: int = 0,
    platform_id: str | None = None,
    device_id: str | None = None,
    cpu_name: str | None = None,
    ram_mb: int | None = None,
    gpu_db: dict[str, Any] | None = None,
) -> HostClass:
    """Classify a host into an ODS class: tier, backend, overlays, bandwidth.

    Ported from `_upstream_classify.sh`. Two passes — exact device knowledge
    (`known_gpus`, matched on device id and on GPU **and CPU** name), then the
    `heuristic_classes` vendor+capacity ladder — falling back to `cpu`/`T1`.

    `vendor` and `memory_type` are passed to the heuristic ladder as exact
    strings, matching upstream. ADK's backend map yields ``cpu`` where ODS
    writes ``none``, so that one token is translated; nothing else is coerced.

    Never raises for an unrecognised host. Only a missing or corrupt
    `gpu-database.json` raises — that is a packaging fault, not a host we failed
    to place.

    Raises:
        OdsError: vendored GPU database missing or unparseable.
    """
    platform_id = platform_id or current_platform_id()
    db = gpu_db if gpu_db is not None else load_gpu_database()

    gpu_name = gpu_name or ""
    cpu_name = cpu_name or ""
    device_id = device_id or ""
    vram_mb = int(vram_mb or 0)
    ram_mb = int(ram_mb) if ram_mb is not None else int(ram_gb or 0) * 1024

    # ADK reports a GPU-less host as vendor "cpu"; ODS writes that as "none"
    # (the `cpu_only` heuristic). Without this the host matches nothing and
    # takes the unmatched default — the same answer by luck, wrong the moment
    # the CPU rung gains a distinct tier.
    vendor_key = (vendor or "unknown").strip().lower()
    if vendor_key == "cpu":
        vendor_key = "none"
    memory_key = (memory_type or "unknown").strip().lower()
    if vendor_key == "none" and memory_key in {"unknown", "discrete", ""}:
        # `cpu_only` is keyed (vendor=none, memory_type=none); a probed
        # "discrete" on a GPU-less host is noise, not a device fact.
        memory_key = "none"

    combined_name = f"{gpu_name} {cpu_name}".strip().lower()
    selected = _match_known_gpu(db, device_id, combined_name, vram_mb)
    source = "known_gpu" if selected else ""
    if selected is None:
        selected = _match_heuristic(db, vendor_key, memory_key, vram_mb, ram_mb)
        source = "heuristic_class" if selected else "unknown"

    bandwidth = _bandwidth(db, selected, vendor_key, gpu_name, cpu_name)

    if selected and "specs" in selected:
        specs = selected["specs"] or {}
        recommended = selected.get("recommended") or {}
        class_id = str(selected.get("id") or "unknown")
        label = str(specs.get("label") or selected.get("id") or "Unknown")
        backend = str(recommended.get("backend") or DEFAULT_BACKEND)
        tier = str(recommended.get("tier") or DEFAULT_TIER)
        memory_source = str(specs.get("memory_source") or "vram")
        gpu_label = str(specs.get("label") or "")
        # Beyond the script's contract, but the whole reason to consult
        # known_gpus: its specs are device FACTS and outrank a probe.
        resolved_memory_type = str(specs.get("memory_type") or memory_key)
        resolved_vram = int(specs.get("memory_mb") or vram_mb or 0)
    elif selected:
        recommended = selected.get("recommended") or {}
        class_id = str(selected.get("id") or "unknown")
        label = str(selected.get("id") or "Unknown").replace("_", " ").title()
        backend = str(recommended.get("backend") or DEFAULT_BACKEND)
        tier = str(recommended.get("tier") or DEFAULT_TIER)
        m_memtype = (selected.get("match") or {}).get("memory_type", "")
        memory_source = "ram" if backend == "cpu" or m_memtype == "unified" else "vram"
        gpu_label = ""
        resolved_memory_type = memory_key
        resolved_vram = vram_mb
    else:
        class_id, label = "unknown", "Unknown"
        backend, tier = DEFAULT_BACKEND, DEFAULT_TIER
        memory_source, gpu_label = "vram", ""
        resolved_memory_type = memory_key
        resolved_vram = vram_mb
        logger.debug(
            "No ODS hardware class matched (gpu=%r cpu=%r vendor=%r memory_type=%r "
            "vram_mb=%d) — using the %s/%s default.",
            gpu_name, cpu_name, vendor_key, memory_key, vram_mb,
            DEFAULT_BACKEND, DEFAULT_TIER,
        )

    overlays = OVERLAY_MAP.get(backend, FALLBACK_OVERLAYS)
    if backend == "apple" and platform_id == "macos":
        overlays = MACOS_APPLE_OVERLAYS

    # The resolver's memory envelope keys on `unified`; a class whose memory
    # comes from RAM is unified whatever the probe claimed.
    if memory_source == "ram" and backend != "cpu":
        resolved_memory_type = "unified"
    if resolved_memory_type in {"unknown", "", "none"}:
        resolved_memory_type = "discrete"

    return HostClass(
        id=class_id,
        label=label,
        backend=backend,
        tier=tier,
        memory_type=resolved_memory_type,
        vram_mb=resolved_vram,
        compose_overlays=overlays,
        bandwidth_gbps=bandwidth,
        memory_source=memory_source,
        gpu_label=gpu_label,
        source=source,
        matched_patterns=tuple(str(h) for h in (selected or {}).get("_hits") or ()),
    )


__all__ = [
    "HostClass",
    "classify_host",
    "current_platform_id",
    "OVERLAY_MAP",
    "MACOS_APPLE_OVERLAYS",
    "OdsError",
]
