"""Differential: `classify_host()` vs the vendored upstream classifier.

`adk/ods/_upstream_classify.sh` is ODS `scripts/classify-hardware.sh` carried
byte-for-byte. It is bash wrapping a Python heredoc, so — unlike
`select-model.py` — it cannot be imported; `hardware.py` is a PORT of it. A port
without a differential is exactly how the resolver reimplementation came to
disagree with upstream on 16 of 20 envelopes while its own unit tests were
green, so the reference is executed here for real.

Bash is NOT required: the heredoc between `<<'PY'` and the closing `PY` is
extracted verbatim and run with the same argv the script would pass. That
extraction is the only transformation, it is asserted to be non-trivial, and
the file's byte-identity to upstream is separately pinned by
`ODS_VENDORED_SHA256` and checked by `validate_catalog.py --verify-vendored`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from adk.ods.hardware import MACOS_APPLE_OVERLAYS, OVERLAY_MAP, classify_host

_ODS_DIR = Path(__file__).resolve().parent.parent
_SCRIPT = _ODS_DIR / "_upstream_classify.sh"
_GPU_DB = _ODS_DIR / "gpu-database.json"
_HW_CLASSES = _ODS_DIR / "hardware-classes.json"


def _extract_reference_python() -> str:
    """The Python payload of the vendored script, verbatim."""
    text = _SCRIPT.read_text(encoding="utf-8")
    start = text.index("<<'PY'\n") + len("<<'PY'\n")
    end = text.index("\nPY\n", start)
    payload = text[start:end]
    assert "known_gpus" in payload and "heuristic_classes" in payload, (
        "extracted payload does not look like the classifier — the vendored "
        "script's heredoc markers changed"
    )
    return payload


def _upstream_classify(
    platform_id: str, vendor: str, memory_type: str, vram_mb: int,
    device_id: str, gpu_name: str, cpu_name: str, ram_mb: int,
) -> dict:
    """Run the vendored classifier exactly as classify-hardware.sh invokes it."""
    argv = [
        str(_GPU_DB), "false", platform_id, vendor, memory_type, str(vram_mb),
        device_id, gpu_name, cpu_name, str(ram_mb),
    ]
    proc = subprocess.run(
        [sys.executable, "-c", _extract_reference_python(), *argv],
        capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)


# (platform_id, vendor, memory_type, vram_mb, device_id, gpu_name, cpu_name, ram_mb)
CASES = [
    # Exact device knowledge, by GPU name
    ("linux", "nvidia", "discrete", 98304, "", "NVIDIA RTX PRO 6000 Blackwell", "", 262144),
    ("linux", "amd", "discrete", 512, "", "AMD Radeon 8060S Graphics", "", 131072),
    # ...and by CPU name — the case the first implementation missed entirely.
    ("linux", "amd", "discrete", 512, "", "AMD Radeon Graphics", "AMD Ryzen AI MAX+ 395", 131072),
    ("wsl", "amd", "unified", 65536, "", "", "AMD RYZEN AI MAX 390", 65536),
    # Longest-pattern-wins: an XTX host must not match the plain "RX 7900 XT".
    ("linux", "amd", "discrete", 24576, "", "AMD Radeon RX 7900 XTX", "", 65536),
    ("linux", "amd", "discrete", 20480, "", "AMD Radeon RX 7900 XT", "", 65536),
    ("linux", "amd", "discrete", 16384, "", "AMD Radeon RX 9070 XT", "", 32768),
    ("linux", "amd", "discrete", 16384, "", "AMD Radeon RX 9070", "", 32768),
    # device_id matching, including the shared 0x1586 across two Strix entries.
    ("linux", "amd", "unified", 98304, "0x1586", "", "", 131072),
    ("linux", "amd", "unified", 65536, "0x1586", "", "", 65536),
    ("linux", "amd", "unified", 0, "0x1586", "", "", 65536),
    # Grace Blackwell — the host whose tier unlocks upstream's Spark guard.
    ("linux", "nvidia", "unified", 122880, "", "NVIDIA GB10 Grace Blackwell", "", 131072),
    ("linux", "nvidia", "unified", 393216, "", "NVIDIA GB200", "", 1048576),
    # Heuristic ladder, NVIDIA discrete across every band edge.
    ("linux", "nvidia", "discrete", 92160, "", "", "", 131072),
    ("linux", "nvidia", "discrete", 92159, "", "", "", 131072),
    ("linux", "nvidia", "discrete", 40960, "", "", "", 65536),
    ("linux", "nvidia", "discrete", 32607, "", "NVIDIA GeForce RTX 5090", "", 131072),
    ("linux", "nvidia", "discrete", 20480, "", "", "", 65536),
    ("linux", "nvidia", "discrete", 12288, "", "", "", 32768),
    ("linux", "nvidia", "discrete", 8192, "", "", "", 32768),
    ("linux", "nvidia", "discrete", 4096, "", "", "", 16384),
    ("linux", "nvidia", "discrete", 4095, "", "", "", 16384),
    ("linux", "nvidia", "discrete", 0, "", "", "", 16384),
    # NVIDIA unified ladder (keyed on RAM, not VRAM).
    ("linux", "nvidia", "unified", 0, "", "", "", 92160),
    ("linux", "nvidia", "unified", 0, "", "", "", 49152),
    ("linux", "nvidia", "unified", 0, "", "", "", 20480),
    ("linux", "nvidia", "unified", 0, "", "", "", 8192),
    # AMD, Apple, CPU rungs.
    ("linux", "amd", "unified", 0, "", "", "", 98304),
    ("linux", "amd", "unified", 0, "", "", "", 32768),
    ("linux", "amd", "discrete", 20480, "", "", "", 65536),
    ("linux", "amd", "discrete", 12288, "", "", "", 32768),
    ("linux", "amd", "discrete", 4096, "", "", "", 16384),
    ("macos", "apple", "unified", 0, "", "Apple M3 Ultra", "", 131072),
    ("macos", "apple", "unified", 0, "", "", "", 65536),
    ("macos", "apple", "unified", 0, "", "", "", 32768),
    ("macos", "apple", "unified", 0, "", "", "", 8192),
    ("linux", "none", "none", 0, "", "", "", 16384),
    # Platform variations for the same GPU — overlays key on BACKEND, so these
    # must agree with upstream rather than varying by platform.
    ("windows", "nvidia", "discrete", 24576, "", "", "", 65536),
    ("wsl", "nvidia", "discrete", 24576, "", "", "", 65536),
    ("unknown", "nvidia", "discrete", 24576, "", "", "", 65536),
    ("linux", "apple", "unified", 0, "", "", "", 65536),  # apple backend, NOT macos
    # Unrecognisable hosts.
    ("unknown", "unknown", "unknown", 0, "", "Something Nobody Ships", "", 8192),
    ("windows", "intel", "discrete", 16384, "", "Intel Arc B580", "", 32768),
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c[1]}-{c[3]}-{c[5] or c[6] or c[4] or 'bare'}")
def test_port_agrees_with_vendored_classifier(case: tuple) -> None:
    platform_id, vendor, memory_type, vram_mb, device_id, gpu_name, cpu_name, ram_mb = case
    expected = _upstream_classify(*case)
    got = classify_host(
        gpu_name=gpu_name, vendor=vendor, memory_type=memory_type, vram_mb=vram_mb,
        platform_id=platform_id, device_id=device_id, cpu_name=cpu_name, ram_mb=ram_mb,
    )
    assert got.id == expected["id"], "class id"
    assert got.label == expected["label"], "label"
    assert got.backend == expected["recommended"]["backend"], "backend"
    assert got.tier == expected["recommended"]["tier"], "tier"
    assert list(got.compose_overlays) == expected["recommended"]["compose_overlays"], "overlays"
    assert got.bandwidth_gbps == expected["bandwidth_gbps"], "bandwidth"
    assert got.memory_source == expected["memory_source"], "memory_source"
    assert got.gpu_label == expected["gpu_label"], "gpu_label"


def test_case_matrix_covers_both_passes_and_the_unmatched_rung() -> None:
    """Guard against the matrix silently degenerating to one code path."""
    sources = {
        classify_host(
            gpu_name=c[5], vendor=c[1], memory_type=c[2], vram_mb=c[3],
            platform_id=c[0], device_id=c[4], cpu_name=c[6], ram_mb=c[7],
        ).source
        for c in CASES
    }
    assert sources == {"known_gpu", "heuristic_class", "unknown"}, sources


def test_cpu_name_alone_finds_strix_halo() -> None:
    """The concrete miss in the pre-port implementation, pinned as its own test.

    A real Strix Halo box reports a generic GPU string; the identifying name is
    the CPU. Missing it sizes the host from a bogus discrete-VRAM reading.
    """
    host = classify_host(
        gpu_name="AMD Radeon Graphics", cpu_name="AMD Ryzen AI MAX+ 395",
        vendor="amd", memory_type="discrete", vram_mb=512, ram_gb=128,
    )
    assert host.id == "strix_halo_395"
    assert host.tier == "SH_LARGE"
    assert host.memory_type == "unified"
    assert host.vram_mb == 98304


def test_overlay_map_mirrors_hardware_classes_json() -> None:
    """Upstream's `test-overlay-map-coherence.sh`, ported.

    This is what `hardware-classes.json` is actually FOR in this package: it is
    the declarative mirror of OVERLAY_MAP, and upstream ships a contract test
    asserting they agree. Porting that test gives the file a real job (catching
    a re-vendor that changes one without the other) instead of leaving it as a
    hash-pinned file nothing reads.
    """
    classes = json.loads(_HW_CLASSES.read_text(encoding="utf-8"))["classes"]
    assert classes, "hardware-classes.json has no classes"
    for cls in classes:
        backend = cls["recommended"]["backend"]
        actual = cls["recommended"]["compose_overlays"]
        platforms = (cls.get("match") or {}).get("platform_id") or []
        if backend == "apple" and "macos" in platforms:
            expected = list(MACOS_APPLE_OVERLAYS)
        else:
            assert backend in OVERLAY_MAP, f"{cls['id']}: backend {backend!r} not in OVERLAY_MAP"
            expected = list(OVERLAY_MAP[backend])
        assert actual == expected, f"{cls['id']} ({backend}): {actual} != {expected}"
