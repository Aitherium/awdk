package com.aitherium.aither;

import java.util.Locale;

/**
 * The watch's update rules, pure Java so the desktop check runs them (WearUpdater does the
 * Android half). The watch app and the watch face ship in the SAME GitHub release as the
 * phone app (Aitherium/awdk, tag aither-android-vX.Y.Z), as aither-wear-X.Y.Z.apk and
 * aither-watchface-A.B.C.apk, each listed in that release's SHA256SUMS.
 *
 * The watch app has the phone app's package name, so an asset is accepted only by its
 * exact name: the phone's aither-X.Y.Z.apk is never a watch update (WearUpdater also
 * refuses an APK that does not declare the watch feature).
 */
final class WearUpdate {
    static final String RELEASES = "https://api.github.com/repos/Aitherium/awdk/releases?per_page=30";
    static final String TAG = "aither-android-v";
    static final String DOWNLOADS = "https://github.com/Aitherium/awdk/releases/download/";
    static final String WEAR_PREFIX = "aither-wear-";
    static final String FACE_PREFIX = "aither-watchface-";
    static final String FACE_PACKAGE = "com.aitherium.aither.watchface";
    static final long CHECK_EVERY_MS = 6L * 3600 * 1000;

    private WearUpdate() {}

    /** a > b, numerically per dot-part; a part that is not a number never wins. */
    static boolean newer(String a, String b) {
        String[] x = a.split("\\."), y = b.split("\\.");
        for (int i = 0; i < Math.max(x.length, y.length); i++) {
            int u = i < x.length ? num(x[i]) : 0, v = i < y.length ? num(y[i]) : 0;
            if (u != v) return u > v;
        }
        return false;
    }

    private static int num(String s) {
        try {
            return Integer.parseInt(s);
        } catch (NumberFormatException e) {
            return -1;
        }
    }

    /** The version of a published release tag ("aither-android-v0.3.26" -> "0.3.26"), or "". */
    static String tagVersion(String tag, boolean draft, boolean prerelease) {
        if (tag == null || draft || prerelease || !tag.startsWith(TAG)) return "";
        String v = tag.substring(TAG.length());
        return v.matches("\\d+\\.\\d+\\.\\d+") ? v : "";
    }

    static String wearAsset(String version) {
        return WEAR_PREFIX + version + ".apk";
    }

    /** The watch-face asset among a release's asset names ("" when the release has none). */
    static String faceAsset(Iterable<String> names) {
        String best = "", bestV = "0";
        for (String n : names) {
            if (n == null || !n.startsWith(FACE_PREFIX) || !n.endsWith(".apk")) continue;
            String v = n.substring(FACE_PREFIX.length(), n.length() - 4);
            if (v.matches("\\d+\\.\\d+\\.\\d+") && newer(v, bestV)) {
                best = n;
                bestV = v;
            }
        }
        return best;
    }

    /** The lower-case SHA-256 SHA256SUMS lists for {@code name}, or "" (sha256sum text or binary form). */
    static String sumFor(String sums, String name) {
        if (sums == null) return "";
        for (String line : sums.split("\n")) {
            String[] f = line.trim().split("\\s+\\*?");
            if (f.length == 2 && f[1].equals(name) && f[0].matches("[0-9a-fA-F]{64}")) {
                return f[0].toLowerCase(Locale.ROOT);
            }
        }
        return "";
    }

    /** Only GitHub's own download URLs for this repo are ever fetched. */
    static boolean trustedUrl(String url) {
        return url != null && url.startsWith(DOWNLOADS);
    }

    static boolean due(long checkedAt, long now) {
        return now - checkedAt >= CHECK_EVERY_MS || now < checkedAt;
    }
}
