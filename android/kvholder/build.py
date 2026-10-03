"""Build the Aither KV holder APK with the Android SDK's own tools: no Gradle, no AndroidX.

    python awdk/android/kvholder/build.py [--install SERIAL]

Needs a JDK (javac, keytool) and an Android SDK with ``platforms;android-35`` and
``build-tools;35.0.0`` (``sdkmanager``). ``holder.js`` is copied from
``awdk/adk/webui/kvholder/`` at build time, so the app runs the same engine as the browser page.
The APK is signed with the local debug key (``~/.android/debug.keystore``): it installs over
adb, it is not a store release.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parents[1] / "adk" / "webui" / "kvholder" / "holder.js"
API, TOOLS = "35", "35.0.0"


def sdk() -> Path:
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(env):
            return Path(os.environ[env])
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk"
    return local if local.exists() else Path.home() / "Android" / "Sdk"


def tool(bt: Path, name: str) -> str:
    for ext in ("", ".exe", ".bat"):
        p = bt / (name + ext)
        if p.exists():
            return str(p)
    raise SystemExit(f"build: {name} not in {bt} (sdkmanager 'build-tools;{TOOLS}')")


def run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"build: {Path(cmd[0]).name} failed:\n{r.stdout}\n{r.stderr}")


def debug_keystore() -> Path:
    ks = Path.home() / ".android" / "debug.keystore"
    if not ks.exists():
        ks.parent.mkdir(parents=True, exist_ok=True)
        run(
            [
                "keytool",
                "-genkeypair",
                "-keystore",
                str(ks),
                "-storepass",
                "android",
                "-alias",
                "androiddebugkey",
                "-keypass",
                "android",
                "-keyalg",
                "RSA",
                "-keysize",
                "2048",
                "-validity",
                "10000",
                "-dname",
                "CN=Android Debug,O=Android,C=US",
            ]
        )
    return ks


def build(out: Path) -> Path:
    root = sdk()
    jar = root / "platforms" / f"android-{API}" / "android.jar"
    bt = root / "build-tools" / TOOLS
    if not jar.exists():
        raise SystemExit(f"build: {jar} missing (sdkmanager 'platforms;android-{API}')")
    shutil.rmtree(out, ignore_errors=True)
    (out / "assets").mkdir(parents=True)
    for f in (HERE / "assets").iterdir():
        shutil.copy2(f, out / "assets" / f.name)
    shutil.copy2(ENGINE, out / "assets" / "holder.js")
    base = out / "base.apk"
    run(
        [
            tool(bt, "aapt2"),
            "link",
            "--manifest",
            str(HERE / "AndroidManifest.xml"),
            "-I",
            str(jar),
            "-A",
            str(out / "assets"),
            "-o",
            str(base),
        ]
    )
    srcs = [str(p) for p in (HERE / "src").rglob("*.java")]
    run(
        [
            "javac",
            "-source",
            "11",
            "-target",
            "11",
            "-Xlint:-options",
            "-encoding",
            "UTF-8",
            "-classpath",
            str(jar),
            "-d",
            str(out / "classes"),
            *srcs,
        ]
    )
    classes = [str(p) for p in (out / "classes").rglob("*.class")]
    (out / "dex").mkdir()
    run(
        [
            tool(bt, "d8"),
            "--lib",
            str(jar),
            "--min-api",
            "29",
            "--output",
            str(out / "dex"),
            *classes,
        ]
    )
    with zipfile.ZipFile(base, "a", zipfile.ZIP_DEFLATED) as z:
        z.write(out / "dex" / "classes.dex", "classes.dex")
    aligned = out / "aligned.apk"
    run([tool(bt, "zipalign"), "-f", "-p", "4", str(base), str(aligned)])
    apk = out / "aither-kvholder.apk"
    run(
        [
            tool(bt, "apksigner"),
            "sign",
            "--ks",
            str(debug_keystore()),
            "--ks-pass",
            "pass:android",
            "--out",
            str(apk),
            str(aligned),
        ]
    )
    run([tool(bt, "apksigner"), "verify", str(apk)])
    return apk


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--install", default="", metavar="SERIAL", help="adb install -r to this phone")
    a = ap.parse_args(argv)
    apk = build(Path(a.out))
    print(f"build: {apk} ({apk.stat().st_size // 1024} KB)")
    if a.install:
        run(["adb", "-s", a.install, "install", "-r", str(apk)])
        print(f"build: installed on {a.install}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
