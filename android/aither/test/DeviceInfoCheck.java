package com.aitherium.aither;

import java.util.Map;

/** Exit 0 when the check-in reports platform, Android release + patch, and app version. */
public class DeviceInfoCheck {
    static int bad = 0;

    static void eq(String what, Object got, Object want) {
        if (!String.valueOf(got).equals(String.valueOf(want))) {
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
            bad++;
        }
    }

    public static void main(String[] a) {
        Map<String, String> m = DeviceInfo.telemetry("16", "2026-09-05", "0.3.26", 29);
        eq("platform", m.get("platform"), "android");
        eq("os", m.get("os_version"), "Android 16 · patch 2026-09-05");
        eq("app", m.get("app_version"), "0.3.26");
        eq("agent", m.get("agent_version"), "29");
        eq("keys", m.keySet(), "[platform, os_version, app_version, agent_version]");

        // no patch, no code: the fields that are known still go; nothing is invented
        Map<String, String> n = DeviceInfo.telemetry("15", "", "0.3.26", 0);
        eq("os without patch", n.get("os_version"), "Android 15");
        eq("no code", n.containsKey("agent_version"), false);

        // never more than the server keeps, never control characters
        Map<String, String> o = DeviceInfo.telemetry("1\n6" + "x".repeat(80), null, "0.3.26\u0000", 1);
        eq("capped", o.get("os_version").length() <= DeviceInfo.MAX, true);
        eq("no newline", o.get("os_version").contains("\n"), false);
        eq("clean app", o.get("app_version"), "0.3.26");

        if (bad > 0) System.exit(1);
        System.out.println("DeviceInfoCheck ok");
    }
}
