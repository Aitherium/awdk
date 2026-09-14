"""`--setup-model` must resolve a real llama.cpp build on every platform.

Measured 2026-08-22 in a clean Debian 13 container: `adk gobbonet --setup-model`
detected hardware, chose a quant, cloned the UI, and then died in 2 seconds on

    ERROR: no matching llama.cpp build for linux/cpu/x64

The step that turns "a UI" into "a model you can talk to" -- the exact step the
competing Go port's author says is "not automated" for them -- could not
complete on ANY platform. Four defects, each hidden behind the previous:

1. `/releases/latest` excludes prereleases; every llama.cpp build is one.
   `latest` returned `v0.2.0` with one asset, so no build list was ever seen.
2. The matcher required `.zip`; Linux and macOS builds are `.tar.gz`.
3. Linux CPU is `llama-bNNN-bin-ubuntu-x64.tar.gz` -- NO `-cpu-` token -- so
   the CPU fallback could never match it.
4. Extraction was `zipfile` only, so a correct tar.gz would have failed anyway.

The asset list below is a VERBATIM copy of release b10588 (27 assets), taken
the day the bug was found. Pinning it here, rather than hitting GitHub from a
test, means this suite says the same thing in CI with no network -- and if
llama.cpp renames its assets again, `test_live_release_still_matches` (which
DOES hit the network, and is skipped offline) is what goes red, naming the
real cause rather than a flaky test.
"""

from __future__ import annotations

import os
import urllib.request

import pytest
from adk.llamacpp_setup import (
    LLAMACPP_RELEASES_API,
    AccelInfo,
    _is_plain_cpu_build,
    _pick_release_asset,
)

# Exactly what GitHub returned for ggml-org/llama.cpp release b10588.
B10588 = [
    "cudart-llama-bin-win-cuda-12.4-x64.zip",
    "cudart-llama-bin-win-cuda-13.3-x64.zip",
    "cudart-llama-bin-win-cuda-13.4-arm64.zip",
    "llama-b10588-bin-android-arm64.tar.gz",
    "llama-b10588-bin-macos-arm64.tar.gz",
    "llama-b10588-bin-macos-x64.tar.gz",
    "llama-b10588-bin-ubuntu-arm64.tar.gz",
    "llama-b10588-bin-ubuntu-openvino-2026.3-x64.tar.gz",
    "llama-b10588-bin-ubuntu-rocm-7.14-x64.tar.gz",
    "llama-b10588-bin-ubuntu-s390x.tar.gz",
    "llama-b10588-bin-ubuntu-sycl-fp16-x64.tar.gz",
    "llama-b10588-bin-ubuntu-sycl-fp32-x64.tar.gz",
    "llama-b10588-bin-ubuntu-vulkan-arm64.tar.gz",
    "llama-b10588-bin-ubuntu-vulkan-x64.tar.gz",
    "llama-b10588-bin-ubuntu-x64.tar.gz",
    "llama-b10588-bin-win-cpu-arm64.zip",
    "llama-b10588-bin-win-cpu-x64.zip",
    "llama-b10588-bin-win-cuda-12.4-x64.zip",
    "llama-b10588-bin-win-cuda-13.3-x64.zip",
    "llama-b10588-bin-win-cuda-13.4-arm64.zip",
    "llama-b10588-bin-win-opencl-adreno-arm64.zip",
    "llama-b10588-bin-win-openvino-2026.3-x64.zip",
    "llama-b10588-bin-win-rocm-7.14-x64.zip",
    "llama-b10588-bin-win-sycl-x64.zip",
    "llama-b10588-bin-win-vulkan-x64.zip",
    "llama-b10588-ui.tar.gz",
    "llama-b10588-xcframework.zip",
]
ASSETS = [{"name": n, "browser_download_url": f"https://x/{n}"} for n in B10588]


def accel(os_family: str, kind: str, arch: str = "x64") -> AccelInfo:
    return AccelInfo(kind=kind, name=kind, vram_gb=0.0, ram_gb=16.0,
                     cuda_version="", os_family=os_family, arch=arch, notes=[])


def pick(os_family: str, kind: str, arch: str = "x64") -> str:
    url = _pick_release_asset(ASSETS, accel(os_family, kind, arch))
    assert url, f"no asset for {os_family}/{kind}/{arch}"
    return url.rsplit("/", 1)[-1]


# ── the case that was reported ───────────────────────────────────────────

def test_linux_cpu_x64_resolves_to_the_plain_build():
    # Bugs 2 and 3 together: tar.gz, and no -cpu- token.
    assert pick("linux", "cpu") == "llama-b10588-bin-ubuntu-x64.tar.gz"


def test_the_plain_build_is_recognised_by_the_absence_of_a_tag():
    assert _is_plain_cpu_build("llama-b10588-bin-ubuntu-x64.tar.gz")
    assert not _is_plain_cpu_build("llama-b10588-bin-ubuntu-vulkan-x64.tar.gz")
    assert not _is_plain_cpu_build("llama-b10588-bin-ubuntu-rocm-7.14-x64.tar.gz")


# ── every platform a self-hoster might be on ─────────────────────────────

@pytest.mark.parametrize("os_family,kind,arch,expected", [
    ("linux",   "cpu",    "x64",   "llama-b10588-bin-ubuntu-x64.tar.gz"),
    ("linux",   "vulkan", "x64",   "llama-b10588-bin-ubuntu-vulkan-x64.tar.gz"),
    ("linux",   "cpu",    "arm64", "llama-b10588-bin-ubuntu-arm64.tar.gz"),
    ("windows", "cpu",    "x64",   "llama-b10588-bin-win-cpu-x64.zip"),
    ("windows", "vulkan", "x64",   "llama-b10588-bin-win-vulkan-x64.zip"),
    ("macos",   "metal",  "arm64", "llama-b10588-bin-macos-arm64.tar.gz"),
    ("macos",   "metal",  "x64",   "llama-b10588-bin-macos-x64.tar.gz"),
])
def test_every_platform_gets_a_real_shipping_asset(os_family, kind, arch, expected):
    assert pick(os_family, kind, arch) == expected


def test_windows_cuda_prefers_a_cuda_build():
    assert "win-cuda" in pick("windows", "cuda")


def test_linux_cuda_falls_back_to_cpu_rather_than_failing():
    # llama.cpp ships no Linux CUDA binary. The fallback is correct -- a slow
    # model beats no model -- and the caller announces it. What must NOT happen
    # is the old behaviour: refuse outright.
    assert pick("linux", "cuda") == "llama-b10588-bin-ubuntu-x64.tar.gz"


# ── things that must never be picked ─────────────────────────────────────

def test_never_picks_the_cuda_runtime_zip():
    # cudart-*.zip is DLLs only; it has no llama-server in it.
    for os_family, kind in [("windows", "cuda"), ("windows", "cpu")]:
        assert not pick(os_family, kind).startswith("cudart")


def test_never_picks_the_ui_or_xcframework():
    for os_family, kind, arch in [("linux", "cpu", "x64"), ("macos", "metal", "arm64")]:
        got = pick(os_family, kind, arch)
        assert "-ui." not in got and "xcframework" not in got


def test_no_assets_means_none_not_a_crash():
    assert _pick_release_asset([], accel("linux", "cpu")) is None


# ── the endpoint (bug 1) ─────────────────────────────────────────────────

def test_does_not_query_releases_latest():
    """`latest` excludes prereleases, and every llama.cpp build is one.

    This is the bug that broke EVERY platform and reported itself as a
    matcher problem. A constant is the cheapest place to pin it.
    """
    assert "/releases/latest" not in LLAMACPP_RELEASES_API
    assert "/releases" in LLAMACPP_RELEASES_API


# ── and the live release, so a rename upstream is named, not flaky ───────

@pytest.mark.skipif(os.environ.get("ADK_OFFLINE") == "1", reason="network")
def test_live_release_still_matches():
    """If llama.cpp renames its assets again, THIS goes red with the reason.

    Every other test above is pinned to b10588 so CI is deterministic. This
    one asks GitHub, and is the only place a future rename surfaces.
    """
    import json
    req = urllib.request.Request(
        LLAMACPP_RELEASES_API,
        headers={"User-Agent": "AitherADK/1.0",
                 "Accept": "application/vnd.github+json"})
    # NO body-skip on network failure. A skip that fires after partial
    # execution makes a real failure read as 'skipped' -- so an unreachable
    # GitHub is a FAILURE here, named as such, and offline runs opt out via
    # ADK_OFFLINE=1 at decoration time where the decision belongs.
    with urllib.request.urlopen(req, timeout=20) as r:
        releases = json.load(r)
    release = next((r for r in releases if any(
        a["name"].startswith("llama-") and "-bin-" in a["name"]
        for a in r.get("assets", []))), None)
    assert release is not None, "no recent release carries a server build"
    live = release["assets"]
    for os_family, kind, arch in [("linux", "cpu", "x64"), ("windows", "cpu", "x64"),
                                  ("macos", "metal", "arm64")]:
        assert _pick_release_asset(live, accel(os_family, kind, arch)), (
            f"live release {release['tag_name']} has no pickable asset for "
            f"{os_family}/{kind}/{arch} -- llama.cpp may have renamed its builds")
