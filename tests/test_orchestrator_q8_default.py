"""The default local orchestrator is the v18 build at Q8_0; Q4_K_M stays selectable.

Owner decision 2026-10-05 (experiment run_e18eab0164e74e0b, v18 on llama.cpp vs the
f16 reference): Q8_0 passed parity (top-1 0.9976), Q4_K_M failed (top-1 0.9598).
Every surface that names a default orchestrator file must agree, and the small-device
Q4_K_M must still be reachable by name.
"""
from __future__ import annotations

from adk import llamacpp_setup
from adk.models import catalogue, mirror
from adk.packs.gobbonet import catalog as gobbonet_catalog

Q8 = "aither-orchestrator-v18-Q8_0.gguf"
Q8_BYTES = 8_709_518_176  # HEAD on weights.aitherium.com, 2026-10-04
Q4 = "aither-orchestrator-Q4_K_M.gguf"


def test_mirror_default_is_v18_q8_and_q4_still_listed():
    assert mirror.DEFAULT_ORCHESTRATOR_FILE == Q8
    q8 = mirror.CATALOG[Q8]
    assert q8.quantization == "Q8_0"
    assert q8.approx_size_bytes == Q8_BYTES
    q4 = mirror.CATALOG[mirror.SMALL_DEVICE_ORCHESTRATOR_FILE]
    assert q4.filename == Q4 and q4.quantization == "Q4_K_M"
    # Q4 must fit somewhere Q8 does not, or it is not a small-device option at all.
    assert q4.min_vram_gb < q8.min_vram_gb


def test_gobbonet_lists_both_and_q8_needs_more_memory():
    q8 = gobbonet_catalog.find(Q8)
    q4 = gobbonet_catalog.find(Q4)
    assert q8 is not None and q4 is not None
    assert q8.size_bytes == Q8_BYTES
    assert q8.min_ram_gb > q4.min_ram_gb
    assert q8.resolve_url() == gobbonet_catalog.MIRROR_BASE + Q8


def test_models_catalogue_orchestrator_is_q8_with_q4_variant():
    cat = catalogue.load()
    orch = catalogue.get(cat, "orchestrator")
    assert orch["file"] == Q8 and orch["quant"] == "Q8_0"
    assert orch["parts"] == 5 and len(orch["urls"]) == 5
    assert orch["licence"] and orch["licence"]["redistribution_ok"] is True
    small = catalogue.get(cat, "orchestrator-q4")
    assert small["file"] == Q4 and small["quant"] == "Q4_K_M"
    assert small["size_mb"] < orch["size_mb"]


def test_llamacpp_pick_quant_prefers_q8_and_falls_back_on_small_devices():
    assert llamacpp_setup.pick_quant(vram_gb=24.0, ram_gb=64.0) == "Q8_0"
    assert llamacpp_setup.pick_quant(vram_gb=12.0, ram_gb=32.0) == "Q8_0"
    small = llamacpp_setup.pick_quant(vram_gb=8.0, ram_gb=16.0)
    assert small != "Q8_0"
    assert "Q4_K_M" in llamacpp_setup.QUANTS, "Q4_K_M must stay selectable via --quant"
    assert llamacpp_setup.QUANTS["Q8_0"]["size_gb"] >= 8.7
