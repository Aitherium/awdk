"""
Measure node memory bandwidth per tier: VRAM, system RAM, NVMe.

The placement solver needs actual measured bandwidth to make shard placement
decisions. Node registration today advertises only CAPACITY (memory_gb,
gpus[].vram_mb); guessing bandwidth from a GPU name is a fiction that leads
to pathological placement.

Public API:
  measure_all() -> dict with tier -> {gbps, method, measured_at, is_fallback}
  __main__ prints JSON for registration payloads.

All measurements include a fallback profile; a failed tier returns the static
profile and declares is_fallback=True. A fabricated number is worse than a
declared fallback.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Static fallback profiles (conservative estimates from public GPU specs)
FALLBACK_PROFILES = {
    "vram": {"gbps": 120.0, "method": "fallback_generic_gpu"},
    "sysram": {"gbps": 32.0, "method": "fallback_generic_ddr5"},
    "nvme": {"gbps": 5.0, "method": "fallback_generic_nvme"},
}


@dataclass
class BandwidthResult:
    """Single tier measurement result."""

    tier: str
    gbps: float
    method: str
    measured_at: str
    is_fallback: bool


def measure_vram_bandwidth() -> BandwidthResult:
    """Measure GPU VRAM bandwidth via tensor ops or fallback."""
    try:
        import torch

        device = "cuda" if torch.cuda.is_available() else None
        if device is None:
            raise ImportError("CUDA not available")

        # Warmup
        _ = torch.zeros(100, 1000000, device=device).sum()

        samples = []
        for _ in range(3):
            x = torch.zeros(1000, 1000000, device=device)
            start = time.perf_counter()
            _ = x.sum(dim=0)
            elapsed = time.perf_counter() - start
            bytes_accessed = 1000 * 1000000 * 4  # float32
            gbps = (bytes_accessed / 1e9) / elapsed
            samples.append(gbps)

        gbps = sorted(samples)[len(samples) // 2]
        return BandwidthResult(
            tier="vram",
            gbps=gbps,
            method="torch_cuda_tensor_ops",
            measured_at=datetime.utcnow().isoformat(),
            is_fallback=False,
        )

    except (ImportError, RuntimeError) as e:
        logger.warning(f"VRAM measurement failed ({type(e).__name__}); "
                       f"using fallback")
        fallback = FALLBACK_PROFILES["vram"]
        return BandwidthResult(
            tier="vram",
            gbps=fallback["gbps"],
            method=fallback["method"],
            measured_at=datetime.utcnow().isoformat(),
            is_fallback=True,
        )


def measure_sysram_bandwidth() -> BandwidthResult:
    """Measure system RAM bandwidth via numpy or ctypes memcpy."""
    try:
        import numpy as np

        arr_mb = 256
        data = np.zeros((arr_mb * 1024 * 1024 // 8,), dtype=np.float64)

        samples = []
        for _ in range(3):
            start = time.perf_counter()
            _ = data.copy()
            elapsed = time.perf_counter() - start
            bytes_accessed = arr_mb * 1024 * 1024 * 2  # read + write
            gbps = (bytes_accessed / 1e9) / elapsed
            samples.append(gbps)

        gbps = sorted(samples)[len(samples) // 2]
        return BandwidthResult(
            tier="sysram",
            gbps=gbps,
            method="numpy_memcpy",
            measured_at=datetime.utcnow().isoformat(),
            is_fallback=False,
        )

    except (ImportError, MemoryError) as e:
        logger.warning(f"System RAM measurement failed ({type(e).__name__}); "
                       f"using fallback")
        fallback = FALLBACK_PROFILES["sysram"]
        return BandwidthResult(
            tier="sysram",
            gbps=fallback["gbps"],
            method=fallback["method"],
            measured_at=datetime.utcnow().isoformat(),
            is_fallback=True,
        )


def measure_nvme_bandwidth() -> BandwidthResult:
    """Measure NVMe bandwidth via fio or direct file I/O."""
    try:
        # Try fio first
        test_file = Path("/tmp/adk-bandwidth-test-nvme.bin")
        if not test_file.exists():
            test_file = Path.home() / ".adk" / "bandwidth-test-nvme.bin"

        try:
            result = subprocess.run(
                ["fio", "--name=seq_read", "--ioengine=libaio",
                 "--direct=1", "--bs=1m", "--numjobs=4",
                 "--iodepth=32", "--size=1g", "--runtime=5",
                 "--filename=/tmp/fio-test", "--output-format=normal",
                 "--rw=read"],
                capture_output=True,
                text=True,
                timeout=15,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode == 0:
                for line in result.stdout.split("\n"):
                    if "read:" in line and "MB/s" in line:
                        parts = line.split()
                        for i, p in enumerate(parts):
                            if "MB/s" in p and i > 0:
                                try:
                                    mbps = float(parts[i - 1])
                                    gbps = mbps / 1024
                                    return BandwidthResult(
                                        tier="nvme",
                                        gbps=gbps,
                                        method="fio_libaio_read",
                                        measured_at=(
                                            datetime.utcnow().isoformat()
                                        ),
                                        is_fallback=False,
                                    )
                                except (ValueError, IndexError):
                                    pass
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Fallback: direct file I/O
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_bytes(b"\x00" * (256 * 1024 * 1024))

        samples = []
        for _ in range(3):
            start = time.perf_counter()
            with open(test_file, "rb") as f:
                _ = f.read(256 * 1024 * 1024)
            elapsed = time.perf_counter() - start
            bytes_read = 256 * 1024 * 1024
            gbps = (bytes_read / 1e9) / elapsed
            samples.append(gbps)

        test_file.unlink(missing_ok=True)

        gbps = sorted(samples)[len(samples) // 2]
        return BandwidthResult(
            tier="nvme",
            gbps=gbps,
            method="direct_file_io",
            measured_at=datetime.utcnow().isoformat(),
            is_fallback=False,
        )

    except (OSError, MemoryError, subprocess.TimeoutExpired) as e:
        logger.warning(f"NVMe measurement failed ({type(e).__name__}); "
                       f"using fallback")
        fallback = FALLBACK_PROFILES["nvme"]
        return BandwidthResult(
            tier="nvme",
            gbps=fallback["gbps"],
            method=fallback["method"],
            measured_at=datetime.utcnow().isoformat(),
            is_fallback=True,
        )


def measure_all() -> dict[str, dict[str, Any]]:
    """Measure all three tiers and return results."""
    results = {}

    for measure_fn in [
        measure_vram_bandwidth,
        measure_sysram_bandwidth,
        measure_nvme_bandwidth,
    ]:
        try:
            result = measure_fn()
            results[result.tier] = asdict(result)
        except Exception as e:
            logger.error(f"{measure_fn.__name__} raised {type(e).__name__}: "
                         f"{e}")
            tier = measure_fn.__name__.split("_")[1]
            fallback = FALLBACK_PROFILES[tier]
            results[tier] = {
                "tier": tier,
                "gbps": fallback["gbps"],
                "method": fallback["method"],
                "measured_at": datetime.utcnow().isoformat(),
                "is_fallback": True,
            }

    return results


def test_measure_all_works_without_gpu() -> bool:
    """Verify measure_all() returns valid output even without GPU."""
    try:
        result = measure_all()
        if not isinstance(result, dict):
            print(f"FAIL measure_all() returned {type(result)}, "
                  f"expected dict")
            return False

        required_tiers = {"vram", "sysram", "nvme"}
        if set(result.keys()) != required_tiers:
            print(f"FAIL missing tiers: {required_tiers - set(result.keys())}")
            return False

        for tier, data in result.items():
            if not isinstance(data, dict):
                print(f"FAIL result['{tier}'] is not a dict")
                return False
            required_keys = {"tier", "gbps", "method", "measured_at",
                             "is_fallback"}
            if set(data.keys()) != required_keys:
                print(f"FAIL tier '{tier}' missing keys: "
                      f"{required_keys - set(data.keys())}")
                return False
            if not (isinstance(data["gbps"], (int, float)) and
                    data["gbps"] > 0):
                print(f"FAIL tier '{tier}' gbps={data['gbps']} invalid")
                return False
            if not isinstance(data["is_fallback"], bool):
                print(f"FAIL tier '{tier}' is_fallback not bool")
                return False

        print("PASS measure_all() works (all tiers present, valid shapes)")
        return True

    except Exception as e:
        print(f"FAIL measure_all() raised {type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        passed = test_measure_all_works_without_gpu()
        sys.exit(0 if passed else 1)

    result = measure_all()
    print(json.dumps(result, indent=2, default=str))
