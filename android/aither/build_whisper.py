"""Build whisper.cpp's whisper-server for arm64 Android with the NDK, for the Aither app.

    python awdk/android/aither/build_whisper.py [--src DIR] [--jobs 4]

The phone side of the Family AI Pool's stt kind. Same lane as build_llama.py: clones
ggml-org/whisper.cpp at the pinned release, cross-compiles it CPU-only, strips it and puts
it at jniLibs/arm64-v8a/libwhisperserver.so (Android executes files only from the
native-library directory). The server takes 16 kHz mono WAV; the pool converts first.

Gate (2026-10-04, dev 40-clip kid set, tts voices Ava + Ana): base.en WER 5.5%, worst clip
40%, vs the fleet's 3.6% -> within 5 points and no clip over 50%: base.en may serve.
tiny.en (6.8%, worst 60%) may not. Not wired into the app yet: no household product path
calls STT (Veil's /api/voice/transcribe carries no household scope), so a phone offering
stt would never be asked. Wire FamilyShare's stt kind when that caller exists.
Needs the SDK packages ndk;27.2.12479018 and cmake;3.31.6 (sdkmanager).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = "https://github.com/ggml-org/whisper.cpp"
COMMIT = "927cfce34f31707e17f2bff35c349632fb9e2c3a"  # v1.9.4
NDK, CMAKE = "27.2.12479018", "3.31.6"
FLAGS = "-march=armv8.2-a+dotprod+fp16"


def sdk() -> Path:
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(env):
            return Path(os.environ[env])
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Android" / "Sdk"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", default=str(HERE / "out" / "whisper.cpp"))
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args(argv)
    root, src = sdk(), Path(a.src)
    ndk = root / "ndk" / NDK
    cm = root / "cmake" / CMAKE / "bin"
    exe = ".exe" if os.name == "nt" else ""
    if not ndk.exists() or not (cm / f"cmake{exe}").exists():
        print(
            f"build_whisper: need sdkmanager 'ndk;{NDK}' 'cmake;{CMAKE}'", file=sys.stderr
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
            # Google Play refuses native code whose LOAD segments are not 16 KB aligned
            # (apps targeting 35+); build.py checks the result (PAGE_ALIGN)
            "-DANDROID_SUPPORT_FLEXIBLE_PAGE_SIZES=ON",
            "-DCMAKE_EXE_LINKER_FLAGS=-Wl,-z,max-page-size=16384",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DBUILD_SHARED_LIBS=OFF",
            "-DWHISPER_CURL=OFF",
            "-DWHISPER_SDL2=OFF",
            "-DGGML_OPENMP=OFF",
            "-DGGML_NATIVE=OFF",
            "-DWHISPER_BUILD_TESTS=OFF",
            "-DWHISPER_BUILD_EXAMPLES=ON",
            "-DWHISPER_BUILD_SERVER=ON",
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
            "whisper-server",
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
    dest = HERE / "jniLibs" / "arm64-v8a" / "libwhisperserver.so"
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [str(strip), "-o", str(dest), str(build / "bin" / "whisper-server")], check=True
    )
    print(
        f"build_whisper: {dest} ({dest.stat().st_size >> 20} MB) from whisper.cpp {COMMIT[:10]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
