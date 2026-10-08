package com.aitherium.aither;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/**
 * The watch's half of device join (Identity /v1/nodes/join, device discovery phase 1):
 * the rules only, no Android classes, so test/WearJoinCheck.java runs on a desktop JVM.
 *
 * The watch asks to join with its own Ed25519 public key; Identity answers with a request
 * id, a nonce and the six-digit comparison number. The watch RECOMPUTES that number from its
 * own key ({@link #sas}) and shows it only when the two agree, so a swapped key never reaches
 * a screen as the right number. The owner's phone shows the same number on its lock screen;
 * when the owner approves, the watch collects a single-use pairing code with a signature over
 * {@link #message} and enrols at /v1/nodes/pairing/confirm as a "watch".
 */
final class WearJoin {
    static final String PREFIX = "aither-join-v1";

    private WearJoin() {}

    /** Identity's compute_sas: sha256("aither-join-v1|sas|rid|pubkey|nonce"), first 8 bytes,
     *  big-endian unsigned, mod 10^6, zero-padded to six digits. */
    static String sas(String rid, String pubkeyHex, String nonce) {
        try {
            byte[] h = MessageDigest.getInstance("SHA-256").digest(
                    (PREFIX + "|sas|" + rid + "|" + pubkeyHex + "|" + nonce).getBytes(StandardCharsets.UTF_8));
            java.math.BigInteger n = new java.math.BigInteger(1, java.util.Arrays.copyOf(h, 8));
            String s = n.mod(java.math.BigInteger.valueOf(1_000_000)).toString();
            while (s.length() < 6) s = "0" + s;
            return s;
        } catch (Exception e) {
            return "";
        }
    }

    /** What the watch signs when it polls: proof it holds the key the number committed to. */
    static byte[] message(String rid, String nonce) {
        return (PREFIX + "|" + rid + "|" + nonce).getBytes(StandardCharsets.UTF_8);
    }

    /** "482 913": how people read the number off a watch face. */
    static String spaced(String sas) {
        return sas != null && sas.matches("[0-9]{6}") ? sas.substring(0, 3) + " " + sas.substring(3) : "";
    }

    static boolean validRid(String rid) { return rid != null && rid.matches("[0-9a-f]{32}"); }

    static boolean validCode(String code) { return code != null && code.matches("[A-Z0-9]{8}"); }

    /** The raw 32-byte Ed25519 public key from its X.509 SubjectPublicKeyInfo (44 bytes), hex. */
    static String rawPublicHex(byte[] spki) {
        if (spki == null || spki.length < 32) return "";
        StringBuilder sb = new StringBuilder(64);
        for (int i = spki.length - 32; i < spki.length; i++) sb.append(String.format("%02x", spki[i] & 0xff));
        return sb.toString();
    }

    static String hex(byte[] b) {
        StringBuilder sb = new StringBuilder(b.length * 2);
        for (byte x : b) sb.append(String.format("%02x", x & 0xff));
        return sb.toString();
    }

    /** One poll's answer as a word the screen acts on. */
    static String phase(int code, String state) {
        if (code == 200 && "approved".equals(state)) return "approved";
        if (code == 200 && "pending".equals(state)) return "waiting";
        if (code == 200 && "denied".equals(state)) return "denied";
        if (code == 200 && "claimed".equals(state)) return "used";
        if (code == 404) return "gone";
        if (code == 403) return "key";
        if (code == 429 || code == 0 || code >= 500) return "retry";
        return "gone";
    }

    /** What the watch says for each phase that ends the attempt (null = keep going). */
    static String endLine(String phase) {
        switch (phase) {
            case "denied": return "Not approved. Nothing was added.";
            case "gone": return "That request expired. Try again.";
            case "used": return "That request was already used. Try again.";
            case "key": return "This watch's key did not match. Try again.";
            default: return null;
        }
    }

    /** A DNS-safe hostname for Identity (no '_', lower-case, at most 63). */
    static String hostname(String model) {
        String h = (model == null ? "" : model).toLowerCase(java.util.Locale.ROOT)
                .replaceAll("[^a-z0-9-]+", "-").replaceAll("^-+|-+$", "");
        if (h.length() > 63) h = h.substring(0, 63).replaceAll("-+$", "");
        return h.isEmpty() ? "wear-watch" : h;
    }
}
