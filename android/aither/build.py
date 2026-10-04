"""Build the Aither Android app with the Android SDK's own tools: no Gradle, no AndroidX.

    python awdk/android/aither/build_llama.py         # once: the on-phone model engine (NDK)
    python awdk/android/aither/build.py [--install SERIAL]

Needs a JDK (javac, keytool) and an Android SDK with ``platforms;android-35`` and
``build-tools;35.0.0`` (``sdkmanager``). ``holder.js`` is copied from
``awdk/adk/webui/kvholder/`` at build time, so the app runs the same engine as the browser page.
Signed with the local debug key (``~/.android/debug.keystore``) unless ``--keystore`` and
``--storepass-file`` name the release key; a release build must carry the release certificate
(SHA-256 ``RELEASE_CERT_SHA256`` below) or the build fails. Not a store release.
"""

from __future__ import annotations

import argparse
import os
import re
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
    r = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
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


RELEASE_CERT_SHA256 = "A5:5F:DD:95:F1:4C:FA:BE:19:4C:AA:78:A2:98:76:95:61:DC:4A:C5:FE:5E:8D:BB:9E:73:47:27:74:E8:1D:56"


def pins() -> None:
    """The version and the release cert are written in two places each; refuse a drift."""
    src = HERE / "src" / "com" / "aitherium" / "aither"
    manifest = (HERE / "AndroidManifest.xml").read_text(encoding="utf-8")
    name = re.search(r'android:versionName="([^"]+)"', manifest)
    code = re.search(r'VERSION = "([^"]+)"', (src / "Config.java").read_text(encoding="utf-8"))
    if not name or not code or name.group(1) != code.group(1):
        raise SystemExit("build: AndroidManifest versionName and Config.VERSION differ")
    cert = re.search(r'RELEASE_CERT = "([0-9a-f]{64})"', (src / "Updater.java").read_text(encoding="utf-8"))
    if not cert or cert.group(1) != RELEASE_CERT_SHA256.replace(":", "").lower():
        raise SystemExit("build: Updater.RELEASE_CERT and RELEASE_CERT_SHA256 differ")


def build(out: Path, keystore: str = "", storepass_file: str = "") -> Path:
    pins()
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
    flat = out / "res.zip"
    run([tool(bt, "aapt2"), "compile", "--dir", str(HERE / "res"), "-o", str(flat)])
    run(
        [
            tool(bt, "aapt2"),
            "link",
            str(flat),
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
    engine = HERE / "jniLibs" / "arm64-v8a" / "libllamaserver.so"
    with zipfile.ZipFile(base, "a", zipfile.ZIP_DEFLATED) as z:
        z.write(out / "dex" / "classes.dex", "classes.dex")
        if (
            engine.exists()
        ):  # llama-server: Android runs app code only from the native-lib dir
            z.write(engine, "lib/arm64-v8a/libllamaserver.so")
        else:
            print(
                "build: no jniLibs/arm64-v8a/libllamaserver.so (run build_llama.py); "
                "AI on this phone will say the engine is missing",
                file=sys.stderr,
            )
    aligned = out / "aligned.apk"
    run([tool(bt, "zipalign"), "-f", "-p", "4", str(base), str(aligned)])
    apk = out / "aither.apk"
    if keystore:  # the release key: the password is read from a file, never an argument
        signer = ["--ks", keystore, "--ks-pass", f"file:{storepass_file}"]
    else:
        signer = ["--ks", str(debug_keystore()), "--ks-pass", "pass:android"]
    run([tool(bt, "apksigner"), "sign", *signer, "--out", str(apk), str(aligned)])
    run([tool(bt, "apksigner"), "verify", str(apk)])
    if keystore:
        r = subprocess.run(
            [tool(bt, "apksigner"), "verify", "--print-certs", str(apk)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        got = RELEASE_CERT_SHA256.replace(":", "").lower()
        if got not in r.stdout.replace(":", "").lower():
            raise SystemExit("build: the APK is not signed by the Aither release key")
    return apk


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument(
        "--install", default="", metavar="SERIAL", help="adb install -r to this phone"
    )
    ap.add_argument(
        "--keystore", default="", help="release keystore (PKCS12); default: debug key"
    )
    ap.add_argument(
        "--storepass-file", default="", help="file holding the keystore password"
    )
    a = ap.parse_args(argv)
    if bool(a.keystore) != bool(a.storepass_file):
        ap.error("--keystore and --storepass-file go together")
    apk = build(Path(a.out), a.keystore, a.storepass_file)
    print(f"build: {apk} ({apk.stat().st_size // 1024} KB)")
    if a.install:
        run(["adb", "-s", a.install, "install", "-r", str(apk)])
        print(f"build: installed on {a.install}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
