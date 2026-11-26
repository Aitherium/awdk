package com.aitherium.aither;

/**
 * Pure-Java unit tests for MeshRemote (shared by phone and Wear OS apps).
 *   javac --release 11 -d out src/com/aitherium/aither/MeshRemote.java test/MeshRemoteCheck.java
 *   java -cp out com.aitherium.aither.MeshRemoteCheck
 */
public final class MeshRemoteCheck {
    private static int fails;

    private static void eq(String what, Object got, Object want) {
        boolean ok = want == null ? got == null : want.equals(got);
        if (!ok) {
            fails++;
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
        }
    }

    public static void main(String[] args) {
        // Private mesh host allowlist: Tailscale 100.64/10, RFC1918, .ts.net, .local ONLY
        eq("tailscale spark", MeshRemote.isPrivateMeshHost("100.64.0.38"), true);
        eq("tailscale pc", MeshRemote.isPrivateMeshHost("100.127.255.254"), true);
        eq("not tailscale 100.128", MeshRemote.isPrivateMeshHost("100.128.0.1"), false);
        eq("lan 192.168", MeshRemote.isPrivateMeshHost("192.168.1.125"), true);
        eq("lan 10", MeshRemote.isPrivateMeshHost("10.0.0.5"), true);
        eq("lan 172.16", MeshRemote.isPrivateMeshHost("172.16.4.2"), true);
        eq("lan 172.32 denied", MeshRemote.isPrivateMeshHost("172.32.0.1"), false);
        eq("ts.net", MeshRemote.isPrivateMeshHost("dgx-spark.tail123.ts.net"), true);
        eq("local", MeshRemote.isPrivateMeshHost("dgx-spark.local"), true);
        eq("public domain denied", MeshRemote.isPrivateMeshHost("app.aitherium.com"), false);
        eq("public ip denied", MeshRemote.isPrivateMeshHost("8.8.8.8"), false);
        eq("evil path denied", MeshRemote.isPrivateMeshHost("100.64.0.38/foo"), false);

        // Hub URL normalization
        eq("hub with port", MeshRemote.normalizeHubUrl("https://100.64.0.38:9448"), "https://100.64.0.38:9448");
        eq("hub default port", MeshRemote.normalizeHubUrl("100.64.0.38"), "https://100.64.0.38:9448");
        eq("http refused", MeshRemote.normalizeHubUrl("http://100.64.0.38:9448"), "");
        eq("public hub refused", MeshRemote.normalizeHubUrl("https://evil.example:9448"), "");

        // QR / deep-link pairing parser
        MeshRemote.Pair p1 = MeshRemote.parsePairUrl("https://100.64.0.38:9448/?token=AbCdEf0123456789");
        eq("qr hub", p1 != null ? p1.hub : null, "https://100.64.0.38:9448");
        eq("qr token", p1 != null ? p1.token : null, "AbCdEf0123456789");

        MeshRemote.Pair p2 = MeshRemote.parsePairUrl(
                "aither://mesh?hub=https%3A%2F%2F100.64.0.46%3A9449&token=MySecretToken_99&node=pc");
        eq("deep link hub", p2 != null ? p2.hub : null, "https://100.64.0.46:9449");
        eq("deep link token", p2 != null ? p2.token : null, "MySecretToken_99");
        eq("deep link node", p2 != null ? p2.node : null, "pc");

        eq("bad public pair", MeshRemote.parsePairUrl("https://evil.example/?token=AbCdEf0123456789"), null);
        eq("short token refused", MeshRemote.parsePairUrl("https://100.64.0.38:9448/?token=short"), null);

        // Voice intent detection
        MeshRemote.IntentRoute r1 = MeshRemote.routeIntent("On the Spark, how much memory is free?");
        eq("spark route", r1.node, "dgx-spark");
        eq("spark mesh", r1.meshRelevant, true);
        eq("spark no posture", r1.posture, "");

        MeshRemote.IntentRoute r2 = MeshRemote.routeIntent("Switch the DGX Spark to heavy MoE");
        eq("switch node", r2.node, "dgx-spark");
        eq("switch posture", r2.posture, "heavy-moe");

        MeshRemote.IntentRoute r3 = MeshRemote.routeIntent("On my PC, run a quick check");
        eq("pc route", r3.node, "pc");
        eq("pc mesh", r3.meshRelevant, true);

        MeshRemote.IntentRoute r4 = MeshRemote.routeIntent("What is the weather in Portland?");
        eq("plain not mesh", r4.meshRelevant, false);
        eq("plain node", r4.node, "");

        // JSON builders
        eq("chat body", MeshRemote.chatBody("hi \"there\"", "spark", true),
                "{\"text\":\"hi \\\"there\\\"\",\"node\":\"dgx-spark\",\"speak\":true}");
        eq("posture body", MeshRemote.postureBody("heavy-moe", "auto"),
                "{\"posture\":\"heavy-moe\",\"node\":\"dgx-spark\"}");
        eq("invalid posture", MeshRemote.postureBody("rm -rf", "dgx-spark"), "");

        if (fails > 0) {
            System.out.println(fails + " FAILED");
            System.exit(1);
        }
        System.out.println("MeshRemoteCheck OK");
    }
}
