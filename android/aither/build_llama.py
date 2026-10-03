"""Build llama.cpp's llama-server for arm64 Android with the NDK, for the Aither app.

    python awdk/android/aither/build_llama.py [--src DIR] [--jobs 4]

Clones ggml-org/llama.cpp at the pinned commit (Bonsai 1's Q1_0 is upstream), cross-compiles
it CPU-only (armv8.2-a dotprod + fp16: every phone this app targets has them), strips it and
puts it at jniLibs/arm64-v8a/libllamaserver.so. Android lets an app execute files only from
its native-library directory, so the server ships as a "library" and runs as a process.
Needs the SDK packages ndk;27.2.12479018 and cmake;3.31.6 (sdkmanager).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = "https://github.com/ggml-org/llama.cpp"
COMMIT = "836d57176dc699a726c55418e4f96b8ca628e1bf"
NDK, CMAKE = "27.2.12479018", "3.31.6"
FLAGS = "-march=armv8.2-a+dotprod+fp16"


def sdk() -> Path:
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(env):
            return Path(os.environ[env])
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Android" / "Sdk"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", default=str(HERE / "out" / "llama.cpp"))
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args(argv)
    root, src = sdk(), Path(a.src)
    ndk = root / "ndk" / NDK
    cm = root / "cmake" / CMAKE / "bin"
    exe = ".exe" if os.name == "nt" else ""
    if not ndk.exists() or not (cm / f"cmake{exe}").exists():
        print(
            f"build_llama: need sdkmanager 'ndk;{NDK}' 'cmake;{CMAKE}'", file=sys.stderr
        )
        return 2
    if not (src / ".git").exists():
        subprocess.run(
            ["git", "clone", "--filter=blob:none", REPO, str(src)], check=True
        )
    subprocess.run(
        ["git", "-C", str(src), "fetch", "--depth", "1", "origin", COMMIT], check=True
    )
    subprocess.run(["git", "-C", str(src), "checkout", "--detach", COMMIT], check=True)
    build = src / "build-android"
    subprocess.run(
        [
            str(cm / f"cmake{exe}"),
            "-S",
            str(src),
            "-B",
            str(build),
            "-G",
            "Ninja",
            f"-DCMAKE_MAKE_PROGRAM={cm / f'ninja{exe}'}",
            f"-DCMAKE_TOOLCHAIN_FILE={ndk / 'build' / 'cmake' / 'android.toolchain.cmake'}",
            "-DANDROID_ABI=arm64-v8a",
            "-DANDROID_PLATFORM=android-29",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DBUILD_SHARED_LIBS=OFF",
            "-DLLAMA_CURL=OFF",
            "-DGGML_OPENMP=OFF",
            "-DGGML_NATIVE=OFF",
            "-DLLAMA_BUILD_TESTS=OFF",
            "-DLLAMA_BUILD_EXAMPLES=OFF",
            "-DLLAMA_BUILD_SERVER=ON",
            f"-DCMAKE_C_FLAGS={FLAGS}",
            f"-DCMAKE_CXX_FLAGS={FLAGS}",
        ],
        check=True,
    )
    subprocess.run(
        [
            str(cm / f"cmake{exe}"),
            "--build",
            str(build),
            "--target",
            "llama-server",
            "-j",
            str(a.jobs),
        ],
        check=True,
    )
    host = (
        "windows-x86_64"
        if os.name == "nt"
        else ("darwin-x86_64" if sys.platform == "darwin" else "linux-x86_64")
    )
    strip = ndk / "toolchains" / "llvm" / "prebuilt" / host / "bin" / f"llvm-strip{exe}"
    dest = HERE / "jniLibs" / "arm64-v8a" / "libllamaserver.so"
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [str(strip), "-o", str(dest), str(build / "bin" / "llama-server")], check=True
    )
    print(
        f"build_llama: {dest} ({dest.stat().st_size >> 20} MB) from llama.cpp {COMMIT[:10]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
