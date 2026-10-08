package com.aitherium.aither;

import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * NearbyBook (the Wi-Fi pairing advert contract and the flood-proof book) on a desktop JVM.
 * Same cases as awdk tests/test_lan_pair.py and awdesk electron/lan-pair.test.cjs.
 *   javac -d out src/com/aitherium/aither/NearbyBook.java test/NearbyBookCheck.java
 *   java -cp out com.aitherium.aither.NearbyBookCheck
 */
public final class NearbyBookCheck {
    private static int fails;
    private static final String RID = "0f1e2d3c4b5a69788796a5b4c3d2e1f0";
    private static long now = 1_000_000L;

    private static void eq(String what, Object got, Object want) {
        boolean ok = want == null ? got == null : want.equals(got);
        if (!ok) {
            fails++;
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
        }
    }

    private static Map<String, byte[]> txt(String... kv) {
        Map<String, byte[]> m = new LinkedHashMap<>();
        m.put("v", b("1"));
        m.put("rid", b(RID));
        m.put("class", b("watch"));
        for (int i = 0; i + 1 < kv.length; i += 2) {
            if (kv[i + 1] == null) m.remove(kv[i]);
            else m.put(kv[i], b(kv[i + 1]));
        }
        return m;
    }

    private static byte[] b(String s) { return s.getBytes(StandardCharsets.UTF_8); }

    private static String rid(int i) { return String.format("%032x", i); }

    private static NearbyBook book(int maxCandidates, int maxPerSource, double burst, double refill) {
        return new NearbyBook(() -> now, NearbyBook.MAX_WINDOW_MS, maxCandidates, maxPerSource, burst, refill);
    }

    private static String rep(char c, int n) {
        StringBuilder s = new StringBuilder();
        for (int i = 0; i < n; i++) s.append(c);
        return s.toString();
    }

    public static void main(String[] args) {
        // the contract
        Map<String, String> good = NearbyBook.parseTxt(txt());
        eq("good", good == null ? null : good.get("rid") + "/" + good.get("class"), RID + "/watch");
        Map<String, Object> mixed = new HashMap<>();
        mixed.put("V", "1");
        mixed.put("RID", RID);
        mixed.put("Class", b("PHONE"));
        Map<String, String> m = NearbyBook.parseTxt(mixed);
        eq("case + bytes", m == null ? null : m.get("class"), "phone");
        eq("unknown keys ignored", NearbyBook.parseTxt(txt("owner", "mallory")).size(), 3);
        for (String k : NearbyBook.FORBIDDEN_KEYS) {
            eq("secret " + k, NearbyBook.parseTxt(txt(k, "123456")), null);
            eq("secret upper " + k, NearbyBook.parseTxt(txt(k.toUpperCase(), "123456")), null);
        }
        eq("v2", NearbyBook.parseTxt(txt("v", "2")), null);
        eq("no v", NearbyBook.parseTxt(txt("v", null)), null);
        eq("short rid", NearbyBook.parseTxt(txt("rid", "short")), null);
        eq("long rid", NearbyBook.parseTxt(txt("rid", rep('a', 33))), null);
        eq("upper rid", NearbyBook.parseTxt(txt("rid", RID.toUpperCase())), null);
        eq("b64 rid", NearbyBook.parseTxt(txt("rid", "Zx9_kq-3Lm0pQrStUvWxZx9_kq-3Lm0p")), null);
        eq("rented class", NearbyBook.parseTxt(txt("class", "spark")), null);
        eq("no rid", NearbyBook.parseTxt(txt("rid", null)), null);
        eq("toaster", NearbyBook.parseTxt(txt("class", "toaster")), null);
        eq("no class", NearbyBook.parseTxt(txt("class", null)), null);
        eq("long value", NearbyBook.parseTxt(txt("junk", rep('x', 65))), null);
        eq("9 keys", NearbyBook.parseTxt(txt("a", "", "b", "", "c", "", "d", "", "e", "", "f", "")), null);
        eq("> 400 bytes", NearbyBook.parseTxt(txt(rep('a', 30), rep('x', 64), rep('b', 30), rep('x', 64),
                rep('c', 30), rep('x', 64), rep('d', 30), rep('x', 64), rep('e', 30), rep('x', 64))), null);
        Map<String, Object> dup = new HashMap<>();
        dup.put("v", "1");
        dup.put("rid", RID);
        dup.put("RID", rid(1));
        dup.put("class", "watch");
        eq("dup after fold", NearbyBook.parseTxt(dup), null);
        eq("null", NearbyBook.parseTxt(null), null);

        eq("label", NearbyBook.cleanLabel("Kid‮enohp\n\u0007's   Watch"), "Kidenohp's Watch");
        eq("label cap", NearbyBook.cleanLabel(rep('x', 100)).length(), NearbyBook.MAX_LABEL);
        eq("approve url", NearbyBook.approveUrl(RID), "https://app.aitherium.com/?app=control&nearby=" + RID);
        eq("approve bad", NearbyBook.approveUrl(RID + "&next=https://evil"), null);
        eq("quote", NearbyBook.quote("a\"<b>\\ "), "\"a\\\"\\u003cb\\u003e\\\\\\u2028\"");

        // the book
        NearbyBook bk = book(16, 2, 40, 4);
        eq("added", bk.offer(txt(), "Pixel\u0000 Watch", "10.0.0.5"), "added");
        now += 10_000;
        eq("refreshed", bk.offer(txt(), "renamed", "10.0.0.5"), "refreshed");
        eq("json", bk.json(), "[{\"rid\":\"" + RID + "\",\"class\":\"watch\",\"label\":\"Pixel Watch\",\"verified\":false}]");
        eq("rejected", bk.offer(txt("sas", "123456"), "", "a"), "rejected");

        NearbyBook ex = book(16, 2, 40, 4);
        ex.offer(txt(), "", "a");
        for (int i = 0; i < 10; i++) { now += 31_000; ex.offer(txt(), "", "a"); }
        eq("no extend", ex.list().size(), 0);
        eq("expired stays gone", ex.offer(txt(), "", "a"), "banned");

        NearbyBook cf = book(16, 2, 40, 4);
        eq("c added", cf.offer(txt(), "", "10.0.0.5"), "added");
        eq("conflict", cf.offer(txt(), "", "10.0.0.66"), "conflict");
        eq("conflict empties", cf.list().size(), 0);
        eq("banned", cf.offer(txt(), "", "10.0.0.5"), "banned");
        NearbyBook flip = book(16, 2, 40, 4);
        flip.offer(txt(), "", "a");
        eq("class flip", flip.offer(txt("class", "laptop"), "", "a"), "conflict");
        now += 301_000;
        eq("ban lifts", cf.offer(txt(), "", "10.0.0.5"), "added");

        NearbyBook ps = book(16, 2, 40, 4);
        eq("ps1", ps.offer(txt("rid", rid(0)), "", "evil"), "added");
        eq("ps2", ps.offer(txt("rid", rid(1)), "", "evil"), "added");
        eq("ps3", ps.offer(txt("rid", rid(2)), "", "evil"), "source-cap");
        eq("ps other", ps.offer(txt("rid", rid(9)), "", "other"), "added");

        NearbyBook full = book(3, 99, 40, 4);
        for (int i = 0; i < 3; i++) full.offer(txt("rid", rid(i)), "", "s" + i);
        eq("full", full.offer(txt("rid", rid(3)), "", "s3"), "full");

        NearbyBook rl = book(99, 99, 5, 1);
        int floods = 0;
        for (int i = 0; i < 8; i++) if ("flood".equals(rl.offer(txt("rid", rid(i)), "", "s" + i))) floods++;
        eq("floods", floods, 3);
        eq("dropped", rl.dropped, 3);
        now += 2000;
        eq("refill", rl.offer(txt("rid", rid(50)), "", "s50"), "added");
        eq("has", rl.has(rid(50)), true);
        eq("has not", rl.has(rid(77)), false);

        if (fails > 0) {
            System.out.println(fails + " failed");
            System.exit(1);
        }
        System.out.println("NearbyBookCheck ok");
    }
}
