"""llama.cpp is installed from a PINNED release and verified before extraction.

llama.cpp went semver at v1.0.0 (2026-11): a MAJOR breaks the C API and server
flags (v1 removed --defrag-thold and 35 deprecated functions, and tightened mmproj
loading). `adk` used to install "the newest build that has binaries", so every
first run could land on a breaking MAJOR nobody here had tried, unverified.

Now: tag b12359 (== v1.0.0, same commit; the v1.0.0 tag itself has no binaries),
a recorded sha256 per asset, and `gh attestation verify` when gh is installed.
The asset names below are VERBATIM from release b12359.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from adk import llamacpp_setup as ls
from adk.llamacpp_setup import AccelInfo

B12359 = [
    "cudart-llama-b12359-bin-ubuntu-cuda-12.8-x64.tar.gz",
    "cudart-llama-b12359-bin-ubuntu-cuda-13.5-arm64.tar.gz",
    "cudart-llama-b12359-bin-ubuntu-cuda-13.5-x64.tar.gz",
    "cudart-llama-bin-win-cuda-12.4-x64.zip",
    "cudart-llama-bin-win-cuda-13.5-arm64.zip",
    "cudart-llama-bin-win-cuda-13.5-x64.zip",
    "llama-b12359-bin-android-arm64-snapdragon.tar.gz",
    "llama-b12359-bin-android-arm64.tar.gz",
    "llama-b12359-bin-linux-arm64-snapdragon.tar.gz",
    "llama-b12359-bin-macos-arm64.tar.gz",
    "llama-b12359-bin-macos-x64.tar.gz",
    "llama-b12359-bin-ubuntu-26.04-s390x.tar.gz",
    "llama-b12359-bin-ubuntu-arm64.tar.gz",
    "llama-b12359-bin-ubuntu-cuda-12.8-x64.tar.gz",
    "llama-b12359-bin-ubuntu-cuda-13.5-arm64.tar.gz",
    "llama-b12359-bin-ubuntu-cuda-13.5-x64.tar.gz",
    "llama-b12359-bin-ubuntu-openvino-2026.4.1-x64.tar.gz",
    "llama-b12359-bin-ubuntu-rocm-10.0-x64.tar.gz",
    "llama-b12359-bin-ubuntu-s390x.tar.gz",
    "llama-b12359-bin-ubuntu-sycl-fp16-x64.tar.gz",
    "llama-b12359-bin-ubuntu-sycl-fp32-x64.tar.gz",
    "llama-b12359-bin-ubuntu-vulkan-arm64.tar.gz",
    "llama-b12359-bin-ubuntu-vulkan-x64.tar.gz",
    "llama-b12359-bin-ubuntu-x64.tar.gz",
    "llama-b12359-bin-ubuntu-zdnn-s390x.tar.gz",
    "llama-b12359-bin-win-cpu-arm64.zip",
    "llama-b12359-bin-win-cpu-x64.zip",
    "llama-b12359-bin-win-cuda-12.4-x64.zip",
    "llama-b12359-bin-win-cuda-13.5-arm64.zip",
    "llama-b12359-bin-win-cuda-13.5-x64.zip",
    "llama-b12359-bin-win-opencl-adreno-arm64.zip",
    "llama-b12359-bin-win-openvino-2026.4.1-x64.zip",
    "llama-b12359-bin-win-rocm-10.0-x64.zip",
    "llama-b12359-bin-win-sycl-x64.zip",
    "llama-b12359-bin-win-vulkan-arm64.zip",
    "llama-b12359-bin-win-vulkan-x64.zip",
    "llama-b12359-ui.tar.gz",
    "llama-b12359-xcframework.zip",
]
ASSETS = [{"name": n, "browser_download_url": f"https://x/{n}"} for n in B12359]


def accel(os_family, kind, arch="x64", cuda=""):
    return AccelInfo(kind=kind, name=kind, vram_gb=0.0, ram_gb=16.0,
                     cuda_version=cuda, os_family=os_family, arch=arch, notes=[])


def pick(*a, **kw) -> str:
    url = ls._pick_release_asset(ASSETS, accel(*a, **kw))
    assert url
    return url.rsplit("/", 1)[-1]


# -- the pin ---------------------------------------------------------------

def test_install_queries_the_pinned_tag_not_a_moving_list():
    assert ls.LLAMACPP_PINNED_TAG == "b12359"
    assert ls.LLAMACPP_RELEASE_NAME == "v1.0.0"
    assert ls.LLAMACPP_RELEASES_API.endswith("/releases/tags/b12359")
    assert "per_page" not in ls.LLAMACPP_RELEASES_API


@pytest.mark.parametrize("os_family,kind,arch,cuda", [
    ("windows", "cpu", "x64", ""), ("windows", "cuda", "x64", ""),
    ("windows", "cuda", "x64", "13.6"), ("windows", "vulkan", "x64", ""),
    ("windows", "cpu", "arm64", ""), ("linux", "cpu", "x64", ""),
    ("linux", "cuda", "x64", ""), ("linux", "cuda", "x64", "13.5"),
    ("linux", "vulkan", "x64", ""), ("linux", "cpu", "arm64", ""),
    ("macos", "metal", "arm64", ""), ("macos", "cpu", "x64", ""),
])
def test_every_asset_we_can_pick_has_a_recorded_sha256(os_family, kind, arch, cuda):
    name = pick(os_family, kind, arch, cuda=cuda)
    assert len(ls.LLAMACPP_ASSET_SHA256.get(name, "")) == 64, name


def test_every_recorded_pin_names_a_real_b12359_asset():
    assert set(ls.LLAMACPP_ASSET_SHA256) <= set(B12359)


# -- CUDA toolkit vs driver --------------------------------------------------

def test_linux_cuda_gets_a_cuda_build_now_that_v1_ships_one():
    assert pick("linux", "cuda") == "llama-b12359-bin-ubuntu-cuda-12.8-x64.tar.gz"


def test_unknown_driver_prefers_the_oldest_toolkit():
    assert pick("windows", "cuda") == "llama-b12359-bin-win-cuda-12.4-x64.zip"


def test_driver_too_old_for_cuda13_never_gets_it():
    assert pick("windows", "cuda", cuda="12.9") == "llama-b12359-bin-win-cuda-12.4-x64.zip"


def test_new_driver_gets_the_newest_toolkit_it_supports():
    assert pick("windows", "cuda", cuda="13.6") == "llama-b12359-bin-win-cuda-13.5-x64.zip"
    assert pick("linux", "cuda", cuda="13.5") == "llama-b12359-bin-ubuntu-cuda-13.5-x64.tar.gz"


@pytest.mark.parametrize("os_family,server,cudart", [
    ("windows", "llama-b12359-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip"),
    ("windows", "llama-b12359-bin-win-cuda-13.5-x64.zip", "cudart-llama-bin-win-cuda-13.5-x64.zip"),
    ("linux", "llama-b12359-bin-ubuntu-cuda-12.8-x64.tar.gz",
     "cudart-llama-b12359-bin-ubuntu-cuda-12.8-x64.tar.gz"),
])
def test_cudart_matches_os_arch_and_toolkit(os_family, server, cudart):
    got = ls._pick_cudart_asset(ASSETS, accel(os_family, "cuda"), server)
    assert got and got["name"] == cudart


def test_cudart_never_crosses_arch():
    # A release whose only Windows cudart is arm64: an x64 box must get none.
    arm64_cudart_only = [a for a in ASSETS
                         if not ("cudart" in a["name"] and "-x64." in a["name"])]
    assert ls._pick_cudart_asset(arm64_cudart_only, accel("windows", "cuda"),
                                 "llama-b12359-bin-win-cuda-12.4-x64.zip") is None


# -- verification -------------------------------------------------------------

NAME = "llama-b12359-bin-win-cpu-x64.zip"


def _file(tmp_path: Path, data: bytes) -> Path:
    p = tmp_path / NAME
    p.write_bytes(data)
    return p


def test_pin_match_without_gh_is_accepted(tmp_path, monkeypatch):
    import hashlib
    data = b"pinned"
    p = _file(tmp_path, data)
    monkeypatch.setitem(ls.LLAMACPP_ASSET_SHA256, NAME, hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(ls, "_gh_attestation_verify", lambda path: None)
    assert ls.verify_llamacpp_asset(p, NAME, tag="b12359")


def test_pin_mismatch_is_refused_and_deleted_even_if_attested(tmp_path, monkeypatch):
    p = _file(tmp_path, b"substituted")
    monkeypatch.setattr(ls, "_gh_attestation_verify", lambda path: True)
    assert not ls.verify_llamacpp_asset(p, NAME, tag="b12359")
    assert not p.exists()


def test_unpinned_tag_requires_an_attestation(tmp_path, monkeypatch):
    p = _file(tmp_path, b"override")
    monkeypatch.setattr(ls, "_gh_attestation_verify", lambda path: None)
    assert not ls.verify_llamacpp_asset(p, NAME, tag="b99999")
    assert not p.exists()


def test_unpinned_tag_with_a_passing_attestation_is_accepted(tmp_path, monkeypatch):
    p = _file(tmp_path, b"override")
    monkeypatch.setattr(ls, "_gh_attestation_verify", lambda path: True)
    assert ls.verify_llamacpp_asset(p, NAME, tag="b99999")


def test_unpinned_tag_with_a_failing_attestation_is_refused(tmp_path, monkeypatch):
    p = _file(tmp_path, b"override")
    monkeypatch.setattr(ls, "_gh_attestation_verify", lambda path: False)
    assert not ls.verify_llamacpp_asset(p, NAME, tag="b99999")


def test_gh_absent_reports_none(monkeypatch, tmp_path):
    monkeypatch.setattr(ls.shutil, "which", lambda _: None)
    assert ls._gh_attestation_verify(tmp_path / "x") is None


# -- extraction ---------------------------------------------------------------

def test_flatten_puts_cudart_libs_beside_the_server(tmp_path):
    archive = tmp_path / "cudart-llama-b12359-bin-ubuntu-cuda-12.8-x64.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        for n in ("cudart-dir/libcudart.so.12", "cudart-dir/libcublas.so.12"):
            info = tarfile.TarInfo(n)
            info.size = 1
            tf.addfile(info, io.BytesIO(b"x"))
    out = tmp_path / "llama-b12359"
    ls._extract(archive, out, flatten=True)
    assert sorted(p.name for p in out.iterdir()) == ["libcublas.so.12", "libcudart.so.12"]


def test_existing_install_from_another_build_is_named(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ls, "LLAMACPP_DIR", tmp_path)
    (tmp_path / "llama-server").write_text("")
    (tmp_path / ls._RELEASE_MARKER).write_text("b10588\n")
    got = ls.install_llamacpp(accel("linux", "cpu"))
    assert got == tmp_path / "llama-server"
    out = capsys.readouterr().out
    assert "b10588" in out and "b12359" in out
