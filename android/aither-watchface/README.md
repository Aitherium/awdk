# Aither watch face

A Watch Face Format (WFF v2) face for Wear OS 5+ (Pixel Watch 4 runs Wear OS 6). It is
resources only: no code, `android:hasCode="false"`, package `com.aitherium.aither.watchface`,
signed with the Aither release certificate like the phone and watch apps.

    python awdk/android/aither/build.py --watchface [--install WATCH]   # debug key
    # release builds are signed by the platform's release lane with the release key
    python awdk/android/aither-watchface/tools/render_previews.py --res # previews

- `res/raw/watchface.xml`: the face. Three styles (Aither, Hearth, Minimal) and an accent
  color (cyan, ember, violet, white). There are three complication slots (steps, battery and
  next event by default) and a built-in weather readout. Ambient mode leaves only the time
  and a dim date lit, about 4% of pixels.
- Tapping the Aither mark opens the Aither watch app (`com.aitherium.aither/.WearActivity`).
- `third_party/wff-xsd-v2/`: Google's WFF v2 schema (Apache-2.0). The platform's app-shell
  schema test validates against it.
- `tools/render_previews.py`: renders every style, accent and ambient variant from the XML,
  for review. It also writes `preview.png` and the style icons. The output is approximate:
  the watch's renderer is the authority.

## Not yet: Aither data on the face

"Waiting for you: N approvals" as a complication needs a complication data source in
aither-wear: a `ComplicationDataSourceService` that answers with `ComplicationData`. There is
no framework class for it. It lives only in androidx (`androidx.wear.watchface:
watchface-complications-data-source`, plus its `-data` and Kotlin/coroutines dependencies),
and the no-Gradle build has no AAR merge or Kotlin stdlib. Two ways to do it:

1. Vendor those AARs. build.py would then unzip `classes.jar` and the manifest entries, pass
   them to javac and d8, and pin each one by SHA-256. This is about six artifacts, Kotlin
   stdlib included.
2. Speak the wire protocol by hand. That means the `IComplicationProvider` AIDL and the
   `ComplicationData` parcel. These are internal and versioned, so they are fragile.

Until then, the "Ask Aither" tap target is the mark, and the approvals count stays in the
watch app.
