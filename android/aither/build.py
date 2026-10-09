"""Build the Aither Android app with the Android SDK's own tools: no Gradle, no AndroidX.

    python awdk/android/aither/build_llama.py         # once: the on-phone model engine (NDK)
    python awdk/android/aither/build.py [--install SERIAL]
    python awdk/android/aither/build.py --store --aab     # the Google Play bundle
    python awdk/android/aither/build.py --wear [--out DIR] [--install WATCH]  # the watch app
    python awdk/android/aither/build.py --watchface [--install WATCH]  # the watch face

Needs a JDK (javac, keytool, jarsigner) and an Android SDK with ``platforms;android-36`` and
``build-tools;36.0.0`` (``sdkmanager``); ``--aab`` fetches bundletool (pinned by SHA-256).
``holder.js`` is copied from ``awdk/adk/webui/kvholder/`` at build time, so the app runs the
same engine as the browser page.
Signed with the local debug key (``~/.android/debug.keystore``) unless ``--keystore`` and
``--storepass-file`` name the release key; a release build must carry the release certificate
(SHA-256 ``RELEASE_CERT_SHA256`` below) or the build fails.

``--store`` is the Google Play build: Flavor.STORE = true (no self-update; Play updates it) and
the manifest without the permissions Play restricts (STORE_DROPS). ``--aab`` writes the App
Bundle Play requires, signed with the same key: that key is the Play UPLOAD key, and Play App
Signing should be given the same key as the app signing key (exported with Google's pepk)
so Play installs and GitHub installs carry one certificate and update each
other. Native code must be 16 KB page aligned (Play, target 35+); PAGE_ALIGN is checked here.
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
API, TOOLS = "36", "36.0.0"
BUNDLETOOL = (
    "1.18.3",
    "a099cfa1543f55593bc2ed16a70a7c67fe54b1747bb7301f37fdfd6d91028e29",
)
PAGE_ALIGN = 16384
# permissions Google Play restricts and the store build does not need (see Flavor.java)
STORE_DROPS = (
    "android.permission.REQUEST_INSTALL_PACKAGES",
    "android.permission.UPDATE_PACKAGES_WITHOUT_USER_ACTION",
    "android.permission.ENFORCE_UPDATE_OWNERSHIP",
    "android.permission.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS",
)


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


def page_align(so: Path) -> int:
    """The smallest LOAD segment alignment of a 64-bit little-endian ELF file."""
    import struct

    b = so.read_bytes()
    if b[:4] != b"\x7fELF" or b[4] != 2:
        raise SystemExit(f"build: {so.name} is not a 64-bit ELF")
    phoff = struct.unpack_from("<Q", b, 0x20)[0]
    size, num = struct.unpack_from("<HH", b, 0x36)
    aligns = [
        struct.unpack_from("<IIQQQQQQ", b, phoff + i * size)[7]
        for i in range(num)
        if struct.unpack_from("<I", b, phoff + i * size)[0] == 1
    ]
    return min(aligns) if aligns else 0


def store_manifest(out: Path) -> Path:
    """The manifest without STORE_DROPS; refuses a drop that is not there to drop."""
    text = (HERE / "AndroidManifest.xml").read_text(encoding="utf-8")
    for perm in STORE_DROPS:
        line = re.compile(
            r'[ \t]*<uses-permission android:name="%s" />\r?\n' % re.escape(perm)
        )
        text, n = line.subn("", text)
        if n != 1:
            raise SystemExit(
                f"build: --store expected one {perm} in the manifest, found {n}"
            )
    path = out / "AndroidManifest.xml"
    path.write_text(text, encoding="utf-8")
    return path


def store_sources(out: Path, srcs: list[str]) -> list[str]:
    """Flavor.java compiled with STORE = true in place of the checked-in one."""
    flavor = HERE / "src" / "com" / "aitherium" / "aither" / "Flavor.java"
    text = flavor.read_text(encoding="utf-8")
    if text.count("STORE = false;") != 1:
        raise SystemExit("build: Flavor.java has no single 'STORE = false;'")
    gen = out / "gen" / "Flavor.java"
    gen.parent.mkdir(parents=True)
    gen.write_text(text.replace("STORE = false;", "STORE = true;"), encoding="utf-8")
    return [s for s in srcs if Path(s).resolve() != flavor.resolve()] + [str(gen)]


def bundletool() -> Path:
    """bundletool-all-<v>.jar in ~/.android/cache, downloaded once and checked by SHA-256."""
    import hashlib
    import urllib.request

    ver, sha = BUNDLETOOL
    jar = Path.home() / ".android" / "cache" / f"bundletool-all-{ver}.jar"
    if not jar.exists():
        jar.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://github.com/google/bundletool/releases/download/{ver}/{jar.name}"
        with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310 - fixed https URL
            jar.write_bytes(r.read())
    if hashlib.sha256(jar.read_bytes()).hexdigest() != sha:
        jar.unlink()
        raise SystemExit(f"build: {jar.name} does not match its pinned SHA-256; removed")
    return jar


def key_alias(keystore: str, storepass_file: str) -> str:
    r = subprocess.run(
        [
            "keytool",
            "-list",
            "-keystore",
            keystore,
            "-storetype",
            "PKCS12",
            "-storepass:file",
            storepass_file,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    # "<alias>, <date with its own comma>, PrivateKeyEntry,"
    m = re.search(r"^([^,\n]+), .*, PrivateKeyEntry", r.stdout, re.M)
    if r.returncode != 0 or not m:
        raise SystemExit("build: keytool could not list the release keystore")
    return m.group(1)


def bundle(
    out: Path, proto: Path, engine: Path, keystore: str, storepass_file: str
) -> Path:
    """An Android App Bundle from aapt2's proto-format APK, the dex and the engine, signed."""
    module = out / "base.zip"
    with zipfile.ZipFile(proto) as src, zipfile.ZipFile(
        module, "w", zipfile.ZIP_DEFLATED
    ) as z:
        for name in src.namelist():
            if name == "AndroidManifest.xml":
                z.writestr("manifest/AndroidManifest.xml", src.read(name))
            elif name == "resources.pb" or name.startswith(("res/", "assets/")):
                z.writestr(name, src.read(name))
            else:
                z.writestr(f"root/{name}", src.read(name))
        z.write(out / "dex" / "classes.dex", "dex/classes.dex")
        z.write(engine, "lib/arm64-v8a/libllamaserver.so")
    config = out / "BundleConfig.json"
    # llama-server is run as a program from nativeLibraryDir, so it must be extracted on
    # install (extractNativeLibs=true): keep native libraries compressed in the bundle
    config.write_text(
        '{"optimizations": {"uncompressNativeLibraries": {"enabled": false}}}',
        encoding="utf-8",
    )
    aab = out / "aither.aab"
    bt = ["java", "-jar", str(bundletool())]
    run(
        [
            *bt,
            "build-bundle",
            "--modules",
            str(module),
            "--config",
            str(config),
            "--output",
            str(aab),
            "--overwrite",
        ]
    )
    if keystore:
        alias = key_alias(keystore, storepass_file)
        signer = [
            "-keystore",
            keystore,
            "-storetype",
            "PKCS12",
            "-storepass:file",
            storepass_file,
        ]
    else:
        alias = "androiddebugkey"
        signer = ["-keystore", str(debug_keystore()), "-storepass", "android"]
    run(
        [
            "jarsigner",
            "-sigalg",
            "SHA256withRSA",
            "-digestalg",
            "SHA-256",
            *signer,
            str(aab),
            alias,
        ]
    )
    run([*bt, "validate", "--bundle", str(aab)])
    if keystore:
        r = subprocess.run(
            ["keytool", "-printcert", "-jarfile", str(aab)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if RELEASE_CERT_SHA256 not in r.stdout:
            raise SystemExit("build: the bundle is not signed by the Aither release key")
    return aab


def build(
    out: Path,
    keystore: str = "",
    storepass_file: str = "",
    store: bool = False,
    aab: bool = False,
) -> Path:
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
    manifest = store_manifest(out) if store else HERE / "AndroidManifest.xml"
    engine = HERE / "jniLibs" / "arm64-v8a" / "libllamaserver.so"
    if keystore and not engine.exists():
        # a RELEASE without the engine shipped 0.3.18-0.3.24 with no on-phone AI at all
        # (measured 2026-10-08: "this build has no local model engine" on the tablet); the
        # warning below was the only signal. No silent fallback: a release needs it.
        raise SystemExit(
            "build: release refused: no jniLibs/arm64-v8a/libllamaserver.so "
            "(run build_llama.py first)")
    if engine.exists() and page_align(engine) < PAGE_ALIGN:
        raise SystemExit(
            f"build: {engine.name} LOAD segments are aligned to {page_align(engine)}, "
            f"Play needs {PAGE_ALIGN}: rebuild it with build_llama.py"
        )
    if (store or aab) and not engine.exists():
        raise SystemExit("build: a store build ships the engine: run build_llama.py first")
    base = out / "base.apk"
    flat = out / "res.zip"
    run([tool(bt, "aapt2"), "compile", "--dir", str(HERE / "res"), "-o", str(flat)])
    link = [tool(bt, "aapt2"), "link", str(flat), "--manifest", str(manifest)]
    if aab:  # a bundle carries the proto-format manifest and resource table
        run(
            [
                *link,
                "--proto-format",
                "-I",
                str(jar),
                "-A",
                str(out / "assets"),
                "-o",
                str(out / "proto.apk"),
            ]
        )
    run(
        [
            *link,
            "-I",
            str(jar),
            "-A",
            str(out / "assets"),
            # R.java, so code names a resource (R.mipmap.ic_shortcut_sprite) and a missing
            # one fails the build instead of a lookup at run time
            "--java",
            str(out / "rgen"),
            "-o",
            str(base),
        ]
    )
    srcs = [str(p) for p in (HERE / "src").rglob("*.java")]
    srcs += [str(p) for p in (out / "rgen").rglob("*.java")]
    if store:
        srcs = store_sources(out, srcs)
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
    # one jar, not every .class on the command line: d8 is a .bat on Windows and cmd.exe
    # refuses a line over 8191 characters (hit at ~53 classes under a worktree path)
    classes_jar = out / "classes.jar"
    with zipfile.ZipFile(classes_jar, "w", zipfile.ZIP_STORED) as z:
        for p in sorted((out / "classes").rglob("*.class")):
            z.write(p, p.relative_to(out / "classes").as_posix())
    classes = [str(classes_jar)]
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
    if aab:
        return bundle(out, out / "proto.apk", engine, keystore, storepass_file)
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
    return sign(bt, base, out / "aither.apk", keystore, storepass_file)


def sign(bt: Path, base: Path, apk: Path, keystore: str, storepass_file: str) -> Path:
    """zipalign + apksigner; a release signature must carry RELEASE_CERT_SHA256."""
    aligned = apk.parent / "aligned.apk"
    run([tool(bt, "zipalign"), "-f", "-p", "4", str(base), str(aligned)])
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


# The watch app (../aither-wear) compiles these phone sources too: one card binding
# (ApprovalCard.decideBody), one listening rule (Talk), one brand kit (Ui), and the
# device-link code + its QR (DeviceLink, Qr), so the watch shows what the phone parses.
WEAR = HERE.parent / "aither-wear"
WEAR_SHARED = ("ApprovalCard.java", "Talk.java", "Speakable.java", "Ui.java", "DeviceLink.java", "Qr.java",
               "BlePair.java", "BleCandidate.java", "Hear.java", "ServerEar.java")


# The watch's Tile (WearTile) needs the Jetpack Tiles library: the system binds a tile
# provider over the androidx.wear.tiles AIDL and reads protolayout protos, which nothing in
# the platform SDK speaks. Classes only (none of these carry resources), fetched from
# Google's Maven (Guava's listenablefuture: Maven Central) once and pinned by SHA-256, so a
# changed artifact fails the build.
MAVEN = ("https://dl.google.com/android/maven2/", "https://repo1.maven.org/maven2/")
WEAR_LIBS = (
    ("androidx/wear/tiles/tiles/1.4.1/tiles-1.4.1.aar",
     "c0e7d86f4227edab5ac69b09c48849325d6ea645a26d5d04a7f417383c80ff5c"),
    ("androidx/wear/tiles/tiles-proto/1.4.1/tiles-proto-1.4.1.jar",
     "f60550dfbfa86f8d7e81d45cc694bc3a073dd84f08fba3d7d5daa5011734c288"),
    ("androidx/wear/protolayout/protolayout/1.2.1/protolayout-1.2.1.aar",
     "2baca490363b891c16718fbbc7db9ea3cc648b51909c4ff2285d2525c30e537a"),
    ("androidx/wear/protolayout/protolayout-expression/1.2.1/protolayout-expression-1.2.1.aar",
     "bf152fbce301a66118405b41920c0733a3fa008bda1af46696c5d871020b95d3"),
    ("androidx/wear/protolayout/protolayout-proto/1.2.1/protolayout-proto-1.2.1.jar",
     "9b51b7738a9cc41deaf9a81dcbce7583f55a6abef7a4c1ad72b3852c839fd006"),
    ("androidx/wear/protolayout/protolayout-external-protobuf/1.2.1/protolayout-external-protobuf-1.2.1.jar",
     "3a06ed2dc85ed0edb06579c0fd7bc708717793946b8b810b8b1d062ecc14abf3"),
    ("androidx/collection/collection/1.2.0/collection-1.2.0.jar",
     "16d77e8c443fa55fe9a6074d00445d520ca5c9f913cefdbf4828356255214e42"),
    ("androidx/concurrent/concurrent-futures/1.1.0/concurrent-futures-1.1.0.jar",
     "0ce067c514a0d1049d1bebdf709e344ed3266fe9744275682937cdcb13334e9e"),
    ("com/google/guava/listenablefuture/1.0/listenablefuture-1.0.jar",
     "e4ad7607e5c0477c6f890ef26a49cb8d1bb4dffb650bab4502afee64644e3069"),
    ("androidx/annotation/annotation/1.2.0/annotation-1.2.0.jar",
     "9029262bddce116e6d02be499e4afdba21f24c239087b76b3b57d7e98b490a36"),
)


def wear_libs(out: Path) -> list[Path]:
    """The pinned WEAR_LIBS as class jars (an AAR's classes.jar), cached between builds."""
    import hashlib
    import urllib.request

    default = Path.home() / ".cache" / "aither-android" / "m2"
    cache = Path(os.environ.get("AITHER_ANDROID_M2") or default)
    cache.mkdir(parents=True, exist_ok=True)
    jars = []
    for rel, sha in WEAR_LIBS:
        f = cache / Path(rel).name
        if not f.exists() or hashlib.sha256(f.read_bytes()).hexdigest() != sha:
            data = b""
            for base in MAVEN:
                try:
                    with urllib.request.urlopen(base + rel, timeout=60) as r:
                        data = r.read()
                    break
                except OSError:
                    continue
            if hashlib.sha256(data).hexdigest() != sha:
                raise SystemExit(f"build: {rel} does not match its pinned SHA-256")
            f.write_bytes(data)
        if f.suffix == ".aar":
            jar = out / "libs" / (f.stem + ".jar")
            jar.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(f) as z:
                jar.write_bytes(z.read("classes.jar"))
            f = jar
        jars.append(f)
    return jars


def build_wear(out: Path, keystore: str = "", storepass_file: str = "") -> Path:
    """The Wear OS app: its own manifest and sources, the phone's launcher icons and the
    WEAR_SHARED sources, signed exactly like the phone app (same key, same cert check)."""
    root = sdk()
    jar = root / "platforms" / f"android-{API}" / "android.jar"
    bt = root / "build-tools" / TOOLS
    if not jar.exists():
        raise SystemExit(f"build: {jar} missing (sdkmanager 'platforms;android-{API}')")
    shutil.rmtree(out, ignore_errors=True)
    res = out / "res"
    for d in (HERE / "res").glob("mipmap-*"):  # the launcher icon, not the phone's shortcuts
        for f in d.iterdir():
            if f.name.startswith("ic_launcher"):
                (res / d.name).mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, res / d.name / f.name)
    base = out / "base.apk"
    flat = out / "res.zip"
    run([tool(bt, "aapt2"), "compile", "--dir", str(res), "-o", str(flat)])
    run(
        [
            tool(bt, "aapt2"),
            "link",
            str(flat),
            "--manifest",
            str(WEAR / "AndroidManifest.xml"),
            "-I",
            str(jar),
            "--java",
            str(out / "rgen"),
            "-o",
            str(base),
        ]
    )
    src = HERE / "src" / "com" / "aitherium" / "aither"
    srcs = [str(p) for p in (WEAR / "src").rglob("*.java")]
    srcs += [str(src / name) for name in WEAR_SHARED]
    srcs += [str(p) for p in (out / "rgen").rglob("*.java")]
    libs = wear_libs(out)
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
            os.pathsep.join([str(jar), *map(str, libs)]),
            "-d",
            str(out / "classes"),
            *srcs,
        ]
    )
    classes_jar = out / "classes.jar"
    with zipfile.ZipFile(classes_jar, "w", zipfile.ZIP_STORED) as z:
        for p in sorted((out / "classes").rglob("*.class")):
            z.write(p, p.relative_to(out / "classes").as_posix())
    (out / "dex").mkdir()
    run(
        [
            tool(bt, "d8"),
            "--lib",
            str(jar),
            "--min-api",
            "30",
            "--output",
            str(out / "dex"),
            str(classes_jar),
            *map(str, libs),
        ]
    )
    with zipfile.ZipFile(base, "a", zipfile.ZIP_DEFLATED) as z:
        # the Tiles protos can need a second dex
        for dex in sorted((out / "dex").glob("classes*.dex")):
            z.write(dex, dex.name)
    return sign(bt, base, out / "aither-wear.apk", keystore, storepass_file)


# The watch face (../aither-watchface): Watch Face Format, resources only. Wear OS draws
# res/raw/watchface.xml itself, so there is no javac/d8 step and the APK has no dex.
WATCHFACE = HERE.parent / "aither-watchface"


def build_watchface(out: Path, keystore: str = "", storepass_file: str = "") -> Path:
    """The Aither watch face: aapt2 compile + link of its res, then the same signing (and
    release-certificate check) as the phone and watch apps."""
    root = sdk()
    jar = root / "platforms" / f"android-{API}" / "android.jar"
    bt = root / "build-tools" / TOOLS
    if not jar.exists():
        raise SystemExit(f"build: {jar} missing (sdkmanager 'platforms;android-{API}')")
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    base = out / "base.apk"
    flat = out / "res.zip"
    run([tool(bt, "aapt2"), "compile", "--dir", str(WATCHFACE / "res"), "-o", str(flat)])
    run(
        [
            tool(bt, "aapt2"),
            "link",
            str(flat),
            "--manifest",
            str(WATCHFACE / "AndroidManifest.xml"),
            "-I",
            str(jar),
            "-o",
            str(base),
        ]
    )
    with zipfile.ZipFile(base) as z:
        if any(n.endswith(".dex") for n in z.namelist()):
            raise SystemExit("build: a Watch Face Format APK must not carry code")
    return sign(bt, base, out / "aither-watchface.apk", keystore, storepass_file)


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
    ap.add_argument(
        "--store",
        action="store_true",
        help="the Google Play build (Flavor.STORE, STORE_DROPS)",
    )
    ap.add_argument("--aab", action="store_true", help="write an App Bundle, not an APK")
    ap.add_argument(
        "--wear",
        action="store_true",
        help="the Wear OS app (../aither-wear), same signing; --install takes the watch's adb serial",
    )
    ap.add_argument(
        "--watchface",
        action="store_true",
        help="the watch face (../aither-watchface), same signing; --install takes the watch's adb serial",
    )
    a = ap.parse_args(argv)
    if bool(a.keystore) != bool(a.storepass_file):
        ap.error("--keystore and --storepass-file go together")
    if a.aab and a.install:
        ap.error("--install takes an APK; a bundle is uploaded to Google Play")
    if a.wear and a.watchface:
        ap.error("--wear and --watchface are separate APKs; build one at a time")
    if a.watchface:
        if a.store or a.aab:
            ap.error("--watchface builds the sideload APK; --store/--aab are the phone's")
        out = Path(a.out) if a.out != str(HERE / "out") else WATCHFACE / "out"
        apk = build_watchface(out, a.keystore, a.storepass_file)
    elif a.wear:
        if a.store or a.aab:
            ap.error("--wear builds the sideload APK; --store/--aab are the phone's")
        out = Path(a.out) if a.out != str(HERE / "out") else WEAR / "out"
        apk = build_wear(out, a.keystore, a.storepass_file)
    else:
        apk = build(Path(a.out), a.keystore, a.storepass_file, a.store, a.aab)
    print(f"build: {apk} ({apk.stat().st_size // 1024} KB)")
    if a.install:
        run(["adb", "-s", a.install, "install", "-r", str(apk)])
        print(f"build: installed on {a.install}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
