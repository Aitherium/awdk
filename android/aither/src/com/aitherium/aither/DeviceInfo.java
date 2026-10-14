package com.aitherium.aither;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * What this device reports it runs, on every household check-in (the
 * heartbeat's platform / os_version / app_version / agent_version fields): so the owner can ask "are my devices up to date?" and
 * get an answer per phone and tablet instead of empty columns. Pure Java (no Android
 * types) so the desktop check can run it; HeartbeatJob passes Build values in.
 *
 * platform      always "android" (a tablet is an Android device too)
 * os_version    "Android 16 · patch 2026-09-05" (the security patch is what "up to date"
 *               means for the OS; Google ships it, this app only reports it)
 * app_version   the app's versionName, compared with the newest aither-android-v* release
 * agent_version the versionCode, which tells two builds of one versionName apart
 */
final class DeviceInfo {
    static final int MAX = 64; // the server's TELEMETRY_MAX

    private DeviceInfo() {}

    static Map<String, String> telemetry(String release, String securityPatch, String versionName,
                                         long versionCode) {
        Map<String, String> m = new LinkedHashMap<>();
        m.put("platform", "android");
        String os = "Android " + clean(release);
        String patch = clean(securityPatch);
        if (!patch.isEmpty()) os += " · patch " + patch;
        m.put("os_version", cap(os.trim()));
        m.put("app_version", cap(clean(versionName)));
        if (versionCode > 0) m.put("agent_version", String.valueOf(versionCode));
        return m;
    }

    private static String clean(String s) {
        return s == null ? "" : s.replaceAll("[^\\x20-\\x7E·]", "").trim();
    }

    private static String cap(String s) {
        return s.length() <= MAX ? s : s.substring(0, MAX);
    }
}
