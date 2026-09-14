"""Hardware-aware model fitting for GGUF inference.

Given a machine, classify which catalogued models can actually run, ranked from
best-fit to worst-fit. Accounts for GPU VRAM, system RAM, and disk space.

Does NOT import from AitherOS — keeps mirror and fit independent so either can
be tested alone.

Usage:
    from adk.models.fit import fit_models
    from adk.models.mirror import CATALOG
    from adk.hardware_probe import detect_system

    system_info = detect_system()
    fits = fit_models(system_info, CATALOG)
    for fit in fits:
        print(f"{fit.model_name}: {fit.classification} ({fit.reason})")
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional
from pathlib import Path


@dataclass
class ModelFit:
    """Result of fitting a model to the current hardware."""

    model_name: str
    """Human-readable model name (e.g., 'Gemma 4 12B')"""

    filename: str
    """GGUF filename from the catalog"""

    classification: str
    """One of: 'runs_comfortably', 'runs_tight', 'will_not_run'"""

    reason: str
    """Plain-language reason (e.g., 'needs ~20 GB VRAM, you have 8 GB GPU + 16 GB RAM')"""

    vram_needed_gb: float
    """GPU VRAM needed for this model (minimum advertised)"""

    disk_needed_gb: float
    """Disk space needed for the weight file"""

    can_run_on_cpu: bool
    """True if model can run on CPU+RAM when VRAM insufficient"""

    estimated_tokens_per_sec: Optional[float] = None
    """Rough speed estimate in tokens/sec for reference (None if unknown)"""


def _get_free_disk_gb(target_path: Optional[Path] = None) -> float:
    """Get available disk space in GB.

    Args:
        target_path: Path where the model would be stored. If None, uses home dir.

    Returns:
        Free disk space in GB. Returns 0 on error.
    """
    if target_path is None:
        target_path = Path.home() / ".models"
    else:
        target_path = Path(target_path)

    try:
        # Resolve to a real path (might be a symlink or relative)
        real_path = target_path.resolve()
        # Find the parent that actually exists
        search_path = real_path
        while not search_path.exists():
            search_path = search_path.parent
            if search_path == search_path.parent:  # Hit root
                search_path = Path.home()
                break

        # st_blocks * 512 = actual used; st_size = file size (for regular files)
        # For a mount point, we want the free space of the filesystem.
        # On POSIX: use statvfs(). On Windows: use ctypes if needed.
        # Python 3.3+: shutil.disk_usage() is cross-platform.
        try:
            import shutil
            _, _, free_bytes = shutil.disk_usage(search_path)
            return free_bytes / (1024**3)
        except Exception:
            # Fallback if shutil.disk_usage fails
            return 0.0
    except Exception:
        return 0.0


def fit_models(
    system_info,  # from hardware_probe.SystemInfo
    catalog,  # dict[str, WeightCatalogEntry] from mirror.CATALOG
    target_path: Optional[Path] = None,
) -> List[ModelFit]:
    """
    Classify which models can run on this system, ranked by fit quality.

    Models are classified as:
      - 'runs_comfortably': GPU VRAM sufficient for efficient inference
      - 'runs_tight': GPU VRAM marginal, or CPU fallback with high latency
      - 'will_not_run': Not enough VRAM or RAM to run at all

    CPU fallback rule: A GGUF model can run on CPU if system RAM >= model size.
    This is slow (~0.1 tokens/sec) but works for small models.

    Args:
        system_info: SystemInfo object from detect_system()
        catalog: Dict[filename -> WeightCatalogEntry] from mirror.CATALOG
        target_path: Where models would be downloaded (for disk check). Defaults to ~/.models.

    Returns:
        List of ModelFit objects, sorted from best fit to worst fit.
        Models that won't fit at all still appear (so user knows why).
    """
    gpu_vram_gb = system_info.gpu_vram_mb / 1024.0
    system_ram_gb = system_info.ram_gb
    free_disk_gb = _get_free_disk_gb(target_path)

    results: List[ModelFit] = []

    for filename, entry in catalog.items():
        model_size_gb = entry.approx_size_bytes / (1024**3)
        min_vram_needed = entry.min_vram_gb

        # Determine if it can run at all, and how
        can_use_gpu = gpu_vram_gb >= min_vram_needed
        can_use_cpu = system_ram_gb >= model_size_gb
        can_fit_on_disk = free_disk_gb >= model_size_gb

        if can_use_gpu:
            # GPU path: check if comfortable or tight
            if gpu_vram_gb >= min_vram_needed * 1.5:
                classification = "runs_comfortably"
                reason = (
                    f"GPU: {system_info.gpu_name} with {gpu_vram_gb:.1f} GB VRAM. "
                    f"Model needs ~{min_vram_needed} GB; comfortable margin."
                )
            else:
                classification = "runs_tight"
                reason = (
                    f"GPU: {system_info.gpu_name} with {gpu_vram_gb:.1f} GB VRAM. "
                    f"Model needs ~{min_vram_needed} GB; tight but workable."
                )

            # Can still run, so always include
            if can_fit_on_disk:
                results.append(
                    ModelFit(
                        model_name=entry.human_name,
                        filename=filename,
                        classification=classification,
                        reason=reason,
                        vram_needed_gb=float(min_vram_needed),
                        disk_needed_gb=model_size_gb,
                        can_run_on_cpu=False,
                    )
                )
            else:
                results.append(
                    ModelFit(
                        model_name=entry.human_name,
                        filename=filename,
                        classification="will_not_run",
                        reason=f"Not enough disk space: {free_disk_gb:.1f} GB free, need {model_size_gb:.1f} GB.",
                        vram_needed_gb=float(min_vram_needed),
                        disk_needed_gb=model_size_gb,
                        can_run_on_cpu=False,
                    )
                )

        elif can_use_cpu:
            # CPU fallback: very slow but possible
            classification = "runs_tight"
            reason = (
                f"GPU insufficient ({gpu_vram_gb:.1f} GB), but CPU+RAM works. "
                f"System RAM: {system_ram_gb:.1f} GB. "
                f"Expect ~0.1 tokens/sec (very slow). "
                f"Local inference recommended only for testing."
            )
            if can_fit_on_disk:
                results.append(
                    ModelFit(
                        model_name=entry.human_name,
                        filename=filename,
                        classification=classification,
                        reason=reason,
                        vram_needed_gb=float(min_vram_needed),
                        disk_needed_gb=model_size_gb,
                        can_run_on_cpu=True,
                        estimated_tokens_per_sec=0.1,
                    )
                )
            else:
                results.append(
                    ModelFit(
                        model_name=entry.human_name,
                        filename=filename,
                        classification="will_not_run",
                        reason=f"Not enough disk space: {free_disk_gb:.1f} GB free, need {model_size_gb:.1f} GB.",
                        vram_needed_gb=float(min_vram_needed),
                        disk_needed_gb=model_size_gb,
                        can_run_on_cpu=True,
                    )
                )

        else:
            # Cannot run: not enough VRAM or RAM
            reason = (
                f"Insufficient resources. Model: {model_size_gb:.1f} GB. "
                f"GPU VRAM: {gpu_vram_gb:.1f} GB (needs {min_vram_needed} GB). "
                f"System RAM: {system_ram_gb:.1f} GB. "
                f"Cannot run even on CPU."
            )
            results.append(
                ModelFit(
                    model_name=entry.human_name,
                    filename=filename,
                    classification="will_not_run",
                    reason=reason,
                    vram_needed_gb=float(min_vram_needed),
                    disk_needed_gb=model_size_gb,
                    can_run_on_cpu=False,
                )
            )

    # Sort: runs_comfortably first, then runs_tight, then will_not_run.
    # Within each tier, sort by model size (smaller first = faster to download/try).
    order = {"runs_comfortably": 0, "runs_tight": 1, "will_not_run": 2}
    results.sort(key=lambda r: (order[r.classification], r.disk_needed_gb))

    return results


if __name__ == "__main__":
    # Test harness: run on this machine
    from adk.hardware_probe import detect_system
    from adk.models.mirror import CATALOG

    print("Detecting system hardware...")
    sys_info = detect_system()
    print(f"  RAM: {sys_info.ram_gb:.1f} GB")
    print(f"  CPU: {sys_info.cpu_cores} cores")
    print(f"  GPU: {sys_info.gpu_vendor.upper()} ({sys_info.gpu_name})")
    print(f"  GPU VRAM: {sys_info.gpu_vram_mb / 1024:.1f} GB")
    print()

    print("Fitting models...")
    fits = fit_models(sys_info, CATALOG)
    print()

    # Group by classification
    by_class = {}
    for fit in fits:
        if fit.classification not in by_class:
            by_class[fit.classification] = []
        by_class[fit.classification].append(fit)

    if by_class.get("runs_comfortably"):
        print("✓ RUNS COMFORTABLY:")
        for fit in by_class["runs_comfortably"]:
            print(f"  {fit.model_name}")
            print(f"    {fit.reason}")
            print()

    if by_class.get("runs_tight"):
        print("~ RUNS TIGHT (marginal resources):")
        for fit in by_class["runs_tight"]:
            print(f"  {fit.model_name}")
            print(f"    {fit.reason}")
            print()

    if by_class.get("will_not_run"):
        print("✗ WILL NOT RUN:")
        for fit in by_class["will_not_run"]:
            print(f"  {fit.model_name}")
            print(f"    {fit.reason}")
            print()
