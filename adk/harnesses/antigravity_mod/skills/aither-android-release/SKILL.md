---
name: aither-android-release
description: >-
  Develop, test, sign, and publish OTA updates for the native Aither Android phone app
  (awdk/android/aither), Wear OS watch app (awdk/android/aither-wear), and watch face
  (awdk/android/aither-watchface). Use whenever editing awdk/android/, bumping Android
  versions, building APKs from WSL2, or shipping aither-android-v* OTA releases.
---

# Aither Android & Wear OS — Build, Test & OTA Release

## 1. Non-Negotiable Architecture & Invariants
- **No Gradle, no AndroidX, no Play Services:** Plain Android Views compiled directly by `awdk/android/aither/build.py` against `platforms;android-36` and `build-tools;36.0.0` with `javac -source 11 -target 11`.
- **Shared phone/watch sources:** Pure-Java files shared between phone and watch live in `awdk/android/aither/src/com/aitherium/aither/` and MUST be listed in `WEAR_SHARED` inside `awdk/android/aither/build.py`.
- **Strict Aither-only voice rule (NO FALLBACKS):** Never import or call Android `TextToSpeech` (`android.speech.tts`) or Google's cloud `createSpeechRecognizer` in either app. Only Aither's voice (`AitherSay` / `WearVoice` / `local:aither` audio) and on-device or Aither STT (`/api/voice/hear`).
- **Component declarations (`ACM001–ACM003`):** Every new `Activity`, `Service`, `JobService`, `BroadcastReceiver`, or `ContentProvider` MUST be declared in `AndroidManifest.xml` (verified by `check_android_components.py`).
- **Version parity (`build.py pins()`):**
  - Phone `awdk/android/aither/AndroidManifest.xml` `android:versionName` == `Config.java` `VERSION` == Watch `awdk/android/aither-wear/AndroidManifest.xml` `android:versionName`.
  - Increment `android:versionCode` independently on phone and watch (they track separate sequences).
  - Keep the version pin in the monorepo's `test_android_app_shell.py` in sync.

## 2. Verification Gates (Run Before Building Release)
These checks live in the AitherOS monorepo's dev-tools and dev-tests trees. Run every command below from the repo root; `git ls-files` finds each file by name, so no checkout layout is hardcoded.
1. **Component manifest check:**
   ```bash
   python3 "$(git ls-files '*/check_android_components.py')"
   ```
2. **Pure-Java unit tests (`--release 11`) via Windows JDK 21 from WSL2:**
   Always redirect stdin (`< /dev/null`) when invoking Windows `.exe` binaries from WSL2 so interop never hangs, and **never** put `PATH/u` in `WSLENV`.
   Run from the repo root; `REPO_WIN` is its Windows path, so no checkout location is hardcoded.
   ```bash
   JAVAC="/var/mnt/c/Program Files/Microsoft/jdk-21.0.9.10-hotspot/bin/javac.exe"
   JAVA="/var/mnt/c/Program Files/Microsoft/jdk-21.0.9.10-hotspot/bin/java.exe"
   REPO_WIN="$(wslpath -w "$(git rev-parse --show-toplevel)")"
   "$JAVAC" --release 11 -encoding UTF-8 -d "$REPO_WIN\\.tmp-java-check" \
     "$REPO_WIN\\awdk\\android\\aither\\src\\com\\aitherium\\aither\\MeshRemote.java" \
     "$REPO_WIN\\awdk\\android\\aither\\test\\MeshRemoteCheck.java" < /dev/null
   "$JAVA" -cp "$REPO_WIN\\.tmp-java-check" com.aitherium.aither.MeshRemoteCheck < /dev/null
   ```
3. **Python test suites (use Python 3.11+ for `test_release_aither_android.py`):**
   ```bash
   python3 -m pytest \
     "$(git ls-files '*/test_android_app_shell.py')" \
     "$(git ls-files '*/test_android_components.py')" \
     awdk/android/aither/test/test_build_release.py -q
   /usr/bin/python3.11 -m pytest "$(git ls-files '*/test_release_aither_android.py')" -q
   ```

## 3. Official Signed OTA Release Lane (NEVER Raw `gh release create`)
Phones (`Updater.java`) and Wear OS watches (`WearUpdater.java`) self-update from **`Aitherium/awdk`** GitHub Releases tagged **`aither-android-v<version>`** (NOT `Aitherium/AitherOS`).

Run the official release tool through Windows Python 3.12 with JDK 21, Android SDK 36, and `gh` on `PATH` (resolving `%LOCALAPPDATA%` dynamically — never hardcode a username). Export `AITHEROS_ROOT` as the Windows path of your checkout first (from the repo root: `export AITHEROS_ROOT="$(wslpath -w "$PWD")"` and add `AITHEROS_ROOT` to `WSLENV`):
```bash
WIN_USER=$(cmd.exe /c "echo %USERNAME%" < /dev/null 2>/dev/null | tr -d '\r\n')
"/var/mnt/c/Users/${WIN_USER}/AppData/Local/Programs/Python/Python312/python.exe" -c '
import glob, os, subprocess, sys
local_app = os.environ["LOCALAPPDATA"]
jdk = sorted(glob.glob(r"C:\Program Files\Microsoft\jdk-21*") + [os.environ.get("JAVA_HOME", "")])[-1]
sdk = os.environ.get("ANDROID_HOME") or os.path.join(local_app, "Android", "Sdk")
build_tools = sorted(glob.glob(os.path.join(sdk, "build-tools", "*")))[-1]
os.environ["JAVA_HOME"] = jdk
os.environ["ANDROID_HOME"] = os.environ["ANDROID_SDK_ROOT"] = sdk
os.environ["PATH"] = ";".join([
    os.path.join(jdk, "bin"),
    build_tools,
    os.path.join(sdk, "platform-tools"),
    r"C:\Program Files\GitHub CLI",
    os.environ.get("PATH", ""),
])
sys.exit(subprocess.run([
    sys.executable,
    os.path.join(os.environ["AITHEROS_ROOT"], "AitherOS", "dev", "tools", "release_aither_android.py"),
    "--publish",
    "--notes", "<release notes>",
]).returncode)
' < /dev/null
```

This single lane:
1. Fetches the signing keystore from `AitherSecrets` via `sign_aither_apk.py` (deleted immediately after).
2. Builds and verifies `RELEASE_CERT_SHA256` on `aither-<v>.apk`, `aither-wear-<v>.apk`, `aither-watchface-<fv>.apk`, and `SHA256SUMS`.
3. Publishes `aither-android-v<v>` to `Aitherium/awdk` and confirms every asset uploaded.
4. Updates Veil's download manifest `public/aither-android.json` (commit this in the same `awgit blob-commit` PR).

## 4. Post-Publish Verification
```bash
gh release view aither-android-v<v> --repo Aitherium/awdk --json tagName,assets --jq '{tag:.tagName,assets:[.assets[]|{name:.name,size:.size,downloads:.downloadCount}]}'
/usr/bin/python3.11 -m pytest "$(git ls-files '*/test_release_aither_android.py')" -q
```
