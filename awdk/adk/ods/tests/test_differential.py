"""Differential test: OdsResolver must agree with the vendored upstream selector.

`adk/ods/_upstream_select.py` is ODS's `scripts/select-model.py` vendored
byte-for-byte, and it is still runnable as a CLI. This test drives that CLI as a
subprocess and compares its pick against `OdsResolver.resolve()` for the same
hardware envelope. Any divergence is wrapper drift.

This exists because the first implementation of the resolver re-derived the
selection algorithm from prose instead of calling upstream, and disagreed with
it in 16 of these 20 cases while its own unit tests were green. Unit tests that
assert against hand-written expectations cannot catch that class of error; only
a differential against the reference implementation can.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from adk.ods import OdsResolver

CATALOG = Path(__file__).resolve().parent.parent / "model-library.json"

# (backend, memory_type, vram_mb, ram_gb, profile, tier, host_arch)
CASES = [
    ("cpu", None, 0, 8, "qwen", "0", "x86_64"),
    ("cpu", None, 0, 16, "qwen", "0", "x86_64"),
    ("cpu", None, 0, 4, "qwen", "0", "x86_64"),
    ("nvidia", "discrete", 8192, 32, "qwen", "1", "x86_64"),
    ("nvidia", "discrete", 12288, 32, "qwen", "2", "x86_64"),
    ("nvidia", "discrete", 24576, 64, "qwen", "3", "x86_64"),
    ("nvidia", "discrete", 49152, 128, "qwen", "4", "x86_64"),
    ("nvidia", "discrete", 98304, 256, "qwen", "NV_ULTRA", "x86_64"),
    ("nvidia", "unified", 131072, 128, "qwen", "NV_ULTRA", "arm64"),
    ("amd", "unified", 65536, 64, "qwen", "SH_COMPACT", "x86_64"),
    ("amd", "unified", 98304, 96, "qwen", "SH_LARGE", "x86_64"),
    ("apple", "unified", 8192, 8, "qwen", "0", "arm64"),
    ("apple", "unified", 16384, 16, "qwen", "1", "arm64"),
    ("apple", "unified", 32768, 32, "qwen", "2", "arm64"),
    ("apple", "unified", 65536, 64, "qwen", "4", "arm64"),
    ("intel", "discrete", 6144, 16, "qwen", "ARC_LITE", "x86_64"),
    ("intel", "discrete", 16384, 32, "qwen", "ARC", "x86_64"),
    ("nvidia", "discrete", 24576, 64, "gemma4", "3", "x86_64"),
    ("apple", "unified", 65536, 64, "gemma4", "4", "arm64"),
    ("nvidia", "discrete", 24576, 64, "auto", "3", "x86_64"),
]


def _upstream_pick(case) -> dict:
    """Run the vendored selector as its own CLI and return its JSON payload."""
    backend, mem_type, vram, ram, profile, tier, arch = case
    cmd = [
        sys.executable, "-m", "adk.ods._upstream_select",
        "--catalog", str(CATALOG),
        "--backend", backend,
        "--vram-mb", str(vram),
        "--ram-gb", str(ram),
        "--profile", profile,
        "--tier", tier,
        "--host-arch", arch,
    ]
    if mem_type:
        cmd += ["--memory-type", mem_type]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"vendored selector failed: {proc.stderr}"
    return json.loads(proc.stdout)


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c[0]}-{c[2]}mb-{c[3]}gb-{c[4]}-t{c[5]}")
def test_resolver_matches_vendored_upstream(case):
    """Our wrapper picks exactly what the vendored reference implementation picks."""
    backend, mem_type, vram, ram, profile, tier, arch = case

    expected = _upstream_pick(case)
    expected_id = expected["selected"]["id"]

    result = OdsResolver(catalog_path=str(CATALOG)).resolve(
        backend=backend, memory_type=mem_type, vram_mb=vram, ram_gb=ram,
        profile=profile, tier=tier, host_arch=arch,
    )

    # POSITIVE assertion: a real, specific model id — not merely "something".
    assert result.selected.id == expected_id
    assert result.profile == expected["profile"]
    assert result.memory_capacity_gb == pytest.approx(expected["memory_capacity_gb"])
    assert result.policy == expected["policy"]


def test_differential_matrix_covers_every_backend():
    """Guard against the matrix silently shrinking to a trivially-passing set."""
    backends = {c[0] for c in CASES}
    assert backends == {"cpu", "nvidia", "amd", "apple", "intel"}
    assert len(CASES) >= 20
