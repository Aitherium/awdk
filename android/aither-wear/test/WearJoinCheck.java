package com.aitherium.aither;

/**
 * Exit 0 when the watch's join rules (WearJoin) agree with Identity
 * (services/security/identity_device_join.py). Args: rid pubkey nonce expectedSas, the
 * vector dev/tests/test_identity_device_join.py computed with Identity's own compute_sas.
 */
public class WearJoinCheck {
    static int bad = 0;

    static void eq(String what, Object got, Object want) {
        if (!String.valueOf(got).equals(String.valueOf(want))) {
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
            bad++;
        }
    }

    public static void main(String[] a) throws Exception {
        // the number is Identity's number, byte for byte
        eq("sas matches identity", WearJoin.sas(a[0], a[1], a[2]), a[3]);
        // a different key gives a different number (the swap the watch refuses to show)
        eq("another key, another number",
                WearJoin.sas(a[0], a[1].replace(a[1].charAt(0), a[1].charAt(0) == 'a' ? 'b' : 'a'), a[2]).equals(a[3]), false);
        eq("six digits", WearJoin.sas("0".repeat(32), "0".repeat(64), "x").matches("[0-9]{6}"), true);
        eq("spaced", WearJoin.spaced("482913"), "482 913");
        eq("spaced refuses junk", WearJoin.spaced("48291"), "");
        eq("message", new String(WearJoin.message("r", "n"), "UTF-8"), "aither-join-v1|r|n");
        // the poll answer
        eq("approved", WearJoin.phase(200, "approved"), "approved");
        eq("pending", WearJoin.phase(200, "pending"), "waiting");
        eq("denied", WearJoin.phase(200, "denied"), "denied");
        eq("gone", WearJoin.phase(404, ""), "gone");
        eq("bad key", WearJoin.phase(403, ""), "key");
        eq("offline retries", WearJoin.phase(0, ""), "retry");
        eq("limited retries", WearJoin.phase(429, ""), "retry");
        eq("waiting does not end", WearJoin.endLine("waiting"), null);
        eq("denied ends", WearJoin.endLine("denied") != null, true);
        // input checks
        eq("rid", WearJoin.validRid("a".repeat(32)), true);
        eq("rid junk", WearJoin.validRid("../x"), false);
        eq("code", WearJoin.validCode("ABCD2345"), true);
        eq("code junk", WearJoin.validCode("abcd2345"), false);
        eq("hostname", WearJoin.hostname("Pixel Watch 4"), "pixel-watch-4");
        eq("hostname empty", WearJoin.hostname("__"), "wear-watch");
        // an X.509 Ed25519 key from this JVM: the raw 32 bytes are its last 32
        java.security.KeyPair kp = java.security.KeyPairGenerator.getInstance("Ed25519").generateKeyPair();
        String hex = WearJoin.rawPublicHex(kp.getPublic().getEncoded());
        eq("raw key is 64 hex", hex.matches("[0-9a-f]{64}"), true);
        if (bad > 0) System.exit(1);
        System.out.println("OK WearJoinCheck");
    }
}
