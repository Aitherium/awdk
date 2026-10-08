# Aither on Wear OS

A standalone watch app (Wear OS 3+, API 30; Pixel Watch 4 runs Wear OS 6, API 36). It uses
the watch's own Wi-Fi/LTE: no phone app, no Google Play Services, no Gradle, no AndroidX.

- **Sign in**: the web's device grant. The watch shows a code; enter it on a signed-in
  phone at the page shown (`idp.aitherium.com/link`). The watch keeps a bearer token.
- **Talk to Aither**: the on-device recognizer when the watch has one (the phone's rule,
  `Talk`), else a text box. Asks `/api/agent-chat` (agent `aeon`), as the web desktop does.
- **Waiting for you**: `/api/push/inbox` (the phone's inbox), open approvals this account
  can still answer, each with Approve / Deny → `/api/push/decide` with the phone's own body
  (`ApprovalCard.decideBody`). Only an unlocked watch with a screen lock answers; the server
  refuses children, stale digests and second votes.

The phone's approval cards are also bridged to the watch (not local-only, dismissal id per
notice). Tapping Approve on a bridged card runs the phone's action, so a locked phone asks
to be unlocked first (`setAuthenticationRequired`).

## Build

    python awdk/android/aither/build.py --wear --out D:\aither-build\wear
    python awdk/android/aither/build.py --wear --keystore <ks> --storepass-file <f>   # release cert

Compiles `src/` plus the phone's `ApprovalCard`, `Talk`, `Ui`, `DeviceLink`, `Qr`, `BlePair` and `BleCandidate`, the phone's launcher icons,
and signs with the same key and certificate check as the phone app.

## Install on the watch

1. Watch: Settings > System > About > Versions, tap **Build number** 7 times.
2. Settings > Developer options: turn on **ADB debugging** and **Wireless debugging**
   (Allow on this network). Watch and computer on the same Wi-Fi.
3. Wireless debugging > **Pair new device**: `adb pair <ip>:<pairing-port>`, enter the code.
4. Back on Wireless debugging, note the IP and port: `adb connect <ip>:<port>`.
5. `adb -s <ip>:<port> install -r D:\aither-build\wear\aither-wear.apk`
   (or `build.py --wear --install <ip>:<port>`).

## Signing in (device link)

The watch asks Identity for a device code and shows it as a QR of
`https://app.aitherium.com/auth/device?code=ABCD-2345` with the code under it
(`DeviceLink.linkUrl`, drawn by the dependency-free `Qr` encoder). On the phone:

- **Camera:** point the phone's camera at the QR. `app.aitherium.com` is a verified App Link,
  so it opens Aither, and MainActivity hands the code to **Link a device** (`LinkActivity`):
  one question, Approve or Cancel, then `POST /api/auth/device-authorize` with the app's
  session. Without the app the same link opens the web page with the code filled in.
- **By hand:** Aither > Settings > **Link a device**, type the code.
- A child's account is refused, in the app (`DeviceLink.childRefusal`) and by Identity (403).

**Bluetooth one-tap ("Sign in your watch"), investigated 2026-10-07: not shipped.** A phone
app and its watch app talk through the Wearable Data Layer (`MessageClient`), which is a
Google Play services library and needs the Play-distributed app pair; this build carries no
Play services by rule. Classic Bluetooth (an RFCOMM socket between the two apps) is not a
documented path for third-party Wear OS apps: the watch's Bluetooth link belongs to the Wear
OS companion, and it would need nearby-device permissions and a pairing prompt on both ends.
The path is **Play distribution + the Data Layer**: the phone mints a pre-approved, 3-minute,
single-use grant (Identity `/auth/device/setup-code`) and sends it to the watch, which
polls it once. Until then the QR is the one-step sign-in.

## Add to my devices (Bluetooth LE)

Signing in (above) gives the watch a session. **Add to my devices** makes it one of the
owner's devices (an Identity node, like the phone), from a phone in Bluetooth range, with no
Play services: plain BLE, the watch advertising and the phone connecting.

1. Watch: **Add to my devices** > Start (`WearBlePair`). It advertises the Aither pairing
   service for at most 3 minutes and only while that screen is open: ten bytes of service
   data (version, "watch", a request id that is a hash of a fresh key, replaced every
   minute). No name, no account, no code.
2. Phone: Aither > Settings > **Nearby devices** (`NearbyDevicesActivity`, Android 12+,
   `BLUETOOTH_SCAN` with `neverForLocation`, scanning only while the screen is open) lists
   it, connects, and runs the commit/reveal key exchange (`BlePair`).
3. Both screens show the same six digits. The phone's owner taps Approve: the phone asks
   Identity for a single-use pairing code (`/api/me/machines` → `/v1/nodes/pairing/init`,
   which refuses a child account) and writes it to the watch sealed with AES-GCM under the
   exchanged key. The watch's owner taps Matches; only then does the watch confirm the code
   with Identity itself (`/v1/nodes/pairing/confirm`, `node_class` "watch") and keep the
   device token in its private storage.

A phone joins the same way from Settings > **Add this phone nearby** (`BleJoinActivity`).
When Bluetooth can't finish, the phone offers "Use a code instead" (the device-code
approval in `LinkActivity`, or the internet join request once `NearbyDevicesActivity.fallback`
is set). The rules are pure Java and run on a desktop JVM: `test/BlePairCheck.java` in the
phone app, run by the platform's BLE pairing test.

## Talking, streamed

`/api/agent-chat` streams Genesis' eager protocol (`answer_segment`, `token`, `segment_end`,
then `answer`/`complete`, which can come ~30 s after the last token). The watch never waits
for the end: `WearFeed` shows tokens as they arrive and hands each finished sentence to
`WearVoice`, which fetches that sentence in Aither's voice at once (the next one while the
current plays) and lights the word being said. Measured on the Pixel Watch 4 (2026-10-08):
first Aither-voice audio 1.2 s after the first sentence ends. Every turn logs `AitherWearLat`.

The agent is picked on the watch (Aither by default); quick asks sit under the talk orb.
The **Talk to Aither** launcher entry opens straight into listening: put it on the button
(Settings > Gestures). The look follows the watch face: Aither, Hearth or Minimal, and its
accents (`WearTheme`).

## Tile

`WearTile`: the wordmark, a Talk orb (opens Talk to Aither) and what waits for you
(approvals and agent asks). It needs Jetpack Tiles, so `build.py --wear` fetches the pinned
classes (`WEAR_LIBS`, SHA-256 checked, cached under `~/.cache/aither-android/m2`). Add it
from the watch's tile list (long-press a tile > +).

## Not yet

- A complication ("N waiting" on the face) needs `androidx.wear.watchface` complications.
- On-wrist background alerts: the phone's bridged cards cover alerts.
