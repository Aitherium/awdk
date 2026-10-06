"""The llama.cpp installer's default orchestrator source is the v18 artifact.

`adk local-orchestrator install` used to fetch an upstream Nemotron-Orchestrator-8B
conversion from HuggingFace, whose default repo has no Q8_0 (it fell to Q6_K). The
owner's default is the v18 build at Q8_0 from the weights mirror, with the v18
Q4_K_M on devices that cannot fit it. These tests fail if the default install ever
reaches HuggingFace again, or if the fit-based quant stops mapping onto v18 files.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from adk import llamacpp_setup as lc
from adk.models import mirror

V18_Q8 = "aither-orchestrator-v18-Q8_0.gguf"
V18_Q4 = "aither-orchestrator-v18-Q4_K_M.gguf"
V18_Q4_BYTES = 5_027_783_520  # awrtifact orchestrator-v18-q4 total == mirror Content-Range


def test_mirror_catalogue_carries_both_v18_quants():
    assert mirror.V18_ORCHESTRATOR_FILES == {"Q8_0": V18_Q8, "Q4_K_M": V18_Q4}
    q4 = mirror.CATALOG[V18_Q4]
    assert q4.approx_size_bytes == V18_Q4_BYTES
    assert q4.quantization == "Q4_K_M"
    # The sha256 is the PUBLISHED artifact's value (landed with the mirror
    # resume/size work via the derived-artifact regen, #11875). Pin it exactly:
    # any change here must follow a re-published artifact, never invention.
    assert q4.sha256 == "8a99577cd97222294d88e5957c4151adc114f59937b84866a6f4e1e2ac0a2d52"
    assert q4.min_vram_gb < mirror.CATALOG[V18_Q8].min_vram_gb


@pytest.mark.parametrize(
    "quant,expected",
    [
        ("Q8_0", (V18_Q8, "Q8_0")),
        ("Q6_K", (V18_Q4, "Q4_K_M")),
        ("Q5_K_M", (V18_Q4, "Q4_K_M")),
        ("Q4_K_M", (V18_Q4, "Q4_K_M")),
        ("Q3_K_M", (V18_Q4, "Q4_K_M")),
    ],
)
def test_every_fit_quant_maps_onto_a_v18_file(quant, expected):
    assert lc.v18_file_for_quant(quant) == expected


def test_install_default_source_is_the_v18_artifact_not_huggingface():
    assert inspect.signature(lc.install).parameters["model_repo"].default is None


class _Recorder:
    def __init__(self):
        self.downloads = []


def _wire(monkeypatch, tmp_path: Path, vram_gb: float, ram_gb: float) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(lc, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(
        lc, "detect_accel",
        lambda: lc.AccelInfo(kind="cuda", name="test", vram_gb=vram_gb, ram_gb=ram_gb,
                             os_family="linux", arch="x64"),
    )
    monkeypatch.setattr(lc, "install_llamacpp", lambda accel, dry_run=False: tmp_path / "srv")
    monkeypatch.setattr(lc, "install_service", lambda *a, **k: True)
    monkeypatch.setattr(lc, "register_config", lambda *a, **k: None)

    def _no_hf(*a, **k):
        raise AssertionError("default install reached the HuggingFace downloader")

    monkeypatch.setattr(lc, "install_model", _no_hf)

    def _download(self, filename, dest_path, verify=True):
        rec.downloads.append((filename, self.rate_limit_bytes_per_sec))
        Path(dest_path).write_bytes(b"GGUF")
        return dest_path

    monkeypatch.setattr(mirror.MirrorClient, "download", _download)
    return rec


def test_install_on_a_big_gpu_downloads_v18_q8_from_the_mirror(monkeypatch, tmp_path):
    rec = _wire(monkeypatch, tmp_path, vram_gb=24.0, ram_gb=64.0)
    result = lc.install(service=False)
    assert result.success
    assert rec.downloads == [(V18_Q8, 0)]
    assert result.model == tmp_path / V18_Q8
    assert result.quant == "Q8_0"


def test_install_on_a_small_gpu_downloads_v18_q4(monkeypatch, tmp_path):
    rec = _wire(monkeypatch, tmp_path, vram_gb=8.0, ram_gb=16.0)
    result = lc.install(service=False)
    assert result.success
    assert [f for f, _ in rec.downloads] == [V18_Q4]
    assert result.quant == "Q4_K_M"


def test_existing_complete_v18_file_is_reused(monkeypatch, tmp_path):
    rec = _wire(monkeypatch, tmp_path, vram_gb=24.0, ram_gb=64.0)
    monkeypatch.setitem(
        mirror.CATALOG, V18_Q8,
        mirror.WeightCatalogEntry(V18_Q8, "t", "AitherOrchestrator", "Q8_0", 4, 12),
    )
    (tmp_path / V18_Q8).write_bytes(b"GGUF")
    path, quant = lc.install_v18_model("Q8_0")
    assert path == tmp_path / V18_Q8 and quant == "Q8_0"
    assert rec.downloads == []


def test_mirror_failure_fails_the_install_without_an_upstream_swap(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, vram_gb=24.0, ram_gb=64.0)

    def _boom(self, filename, dest_path, verify=True):
        raise mirror.MirrorVerificationError("size mismatch")

    monkeypatch.setattr(mirror.MirrorClient, "download", _boom)
    result = lc.install(service=False)
    assert not result.success
    assert result.error == "model download failed"


def test_cli_install_defaults_to_v18(monkeypatch):
    seen = {}

    def _install(**kwargs):
        seen.update(kwargs)
        return lc.InstallResult(success=True)

    monkeypatch.setattr(lc, "install", _install)
    assert lc.main(["install", "--dry-run"]) == 0
    assert seen["model_repo"] is None
