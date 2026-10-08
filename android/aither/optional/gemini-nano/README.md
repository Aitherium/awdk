# Gemini Nano engine (not in the default build)

`MlKitNano.java` is the `GeminiNano.Engine` on ML Kit's GenAI Prompt API
(`com.google.mlkit:genai-prompt:1.0.0-beta4`, Java futures surface). The default build
(`build.py`: javac/d8/aapt2, no Gradle, no AndroidX) does not compile it, and
`GeminiNano.engine()` then reports no engine, so every phone answers with Bonsai as before.

It compiles against the real library (checked 2026-10-07):

    javac -source 11 -target 11 -classpath "android.jar;genai-prompt/classes.jar;genai-common/classes.jar;listenablefuture-1.0.jar" \
        optional/gemini-nano/MlKitNano.java src/com/aitherium/aither/GeminiNano.java src/com/aitherium/aither/NanoRoute.java

## What shipping it takes

1. **The dependency closure.** genai-prompt's POM pulls genai-common, genai-schema, mlkit
   common, play-services-basement/-tasks, firebase-encoders(-json), datatransport
   (transport-api/-backend-cct/-runtime), guava listenablefuture, kotlin-stdlib and
   kotlinx-coroutines (core/guava/reactive), and play-services-basement brings AndroidX
   (core, fragment, lifecycle). build.py has no AAR handling: each AAR's classes.jar must be
   dexed, its res compiled and linked with `--extra-packages`, and its manifest merged
   (genai-prompt alone adds `com.google.android.apps.aicore.service.BIND_SERVICE` and a
   `<queries>` entry for `com.google.android.aicore`; the transitive AARs add more).
   Doing that by hand in build.py is the unmaintainable option. The maintainable one is a
   small Gradle side module that builds this one class plus its closure into a dex and a
   manifest fragment, which build.py then adds when present.
2. **The data question.** The model runs in AICore and the question stays on the phone, but
   ML Kit reports diagnostics to Google (device model, app package and version, latency,
   API config; https://developers.google.com/ml-kit/android-data-disclosure). That is a
   change to "nothing leaves the phone" and to the Play Data safety form.
3. **Device testing.** Prompt API is beta. Gemini Nano for it ships on Pixel 9/9 Pro and
   Pixel 10 (nano-v3) and Pixel 11 (nano-v4); not on phones with an unlocked bootloader;
   inference only while the app is the top foreground app (BACKGROUND_USE_BLOCKED);
   per-app quotas (BUSY, PER_APP_BATTERY_USE_QUOTA_EXCEEDED). NanoRoute handles each of
   those by falling back to Bonsai.

## The opt-in is in place (2026-10-08)

Settings > "AI & voice on this phone" > "Use Gemini Nano on this phone" is OFF by default
(`Config.nanoPreferred()` defaults to false), with the ML Kit diagnostics disclosure under the
switch. Ask Aither offers the Nano download only after that switch is on. A child's phone never
gets it: `Config.localAiBlocked()` is NanoRoute's first gate, and the switch is disabled there
(no guardian override exists yet). In a build without the engine the switch reads "(not in this
version)" and stays disabled.

## Smallest way to ship the engine: the options

| Option | Works? | Cost |
|---|---|---|
| Companion APK (Gradle-built "Aither Nano" app, bound service) | **No**: AICore runs inference only for the TOP foreground app (BACKGROUND_USE_BLOCKED); the companion is never foreground while Ask Aither is | none, ruled out |
| Vendor the AARs into build.py (dex classes.jar, aapt2-compile each AAR's res, `--extra-packages`, hand-merge manifests) | Yes | ~25 AARs, R classes for each package, manifest merging by hand; breaks on every ML Kit bump |
| **Gradle side module** (`optional/gemini-nano/` builds MlKitNano + closure into one dex + a compiled-res zip + a manifest fragment; build.py merges them when `--nano` is passed) | Yes | one small Gradle project, CI needs the Android Gradle plugin; the default build stays Gradle-free |
| Move the whole app to Gradle | Yes | biggest change; also unlocks Jetpack AppFunctions (KSP), Compose A2UI and Play App Bundles natively; do it with the Play launch |

Recommended: the Gradle side module now (behind `--nano`, off in the default release until the
owner accepts the diagnostics), and the full Gradle move together with the Play Store launch.
The same closure also brings ML Kit's GenAI speech recognition, which could replace Android's
SpeechRecognizer for Talk when the owner opts in; NanoRoute already falls back on every error.
