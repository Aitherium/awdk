package com.aitherium.aither;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.function.LongSupplier;

/**
 * "Nearby devices": what an unpaired device in pairing mode advertises on the Wi-Fi
 * (_aither-pair._tcp, TXT {v=1, rid, class}) and the book a member's phone keeps of them.
 * Phase 2 of device join (AitherOS/docs/devices/LAN_PAIR_DISCOVERY.md). The rid and the
 * classes are Identity's (services/security/identity_device_join.py: _RID_RE,
 * JOINABLE_CLASSES). Kept in step with
 * awdk adk/lan_pair.py and awdesk electron/lan-pair.cjs (same test cases).
 *
 * Everything heard is UNTRUSTED: only those three TXT keys are believed, an advert carrying
 * a secret-shaped key is dropped whole, the advertised host and port are never contacted,
 * and one rid seen from two hosts is a replay (dropped, banned for a window). Joining still
 * needs the device's 6-digit code typed on the signed-in approval page (Phase 1, Identity).
 * Plain Java (no Android imports) so test/NearbyBookCheck.java runs on a desktop JVM.
 */
final class NearbyBook {
    static final String SERVICE_TYPE = "_aither-pair._tcp";
    static final String ADVERT_VERSION = "1";
    static final Set<String> DEVICE_CLASSES = Collections.unmodifiableSet(new HashSet<>(Arrays.asList(
            "phone", "watch", "laptop", "desktop", "deck")));
    static final Set<String> FORBIDDEN_KEYS = Collections.unmodifiableSet(new HashSet<>(Arrays.asList(
            "sas", "code", "pin", "otp", "token", "poll", "poll_token", "secret", "key",
            "password", "pass", "pw", "auth", "bearer", "nonce", "claim", "claim_secret",
            "join", "join_code", "sig", "signature")));
    static final long MAX_WINDOW_MS = 5 * 60 * 1000L;
    static final int MAX_TXT_KEYS = 8;
    static final int MAX_TXT_VALUE = 64;
    static final int MAX_TXT_BYTES = 400;
    static final int MAX_LABEL = 40;
    /** Phase-1 seam: Aither Control's Devices tab ("Devices waiting to join"), signed in,
     *  where the member compares or types the device's number. Change HERE. */
    static final String APPROVE_PAGE = "https://app.aitherium.com/?app=control&nearby=";

    private NearbyBook() {
        this(System::currentTimeMillis, MAX_WINDOW_MS, 16, 2, 40, 4);
    }

    NearbyBook(LongSupplier clock, long ttlMs, int maxCandidates, int maxPerSource, double burst,
               double refillPerSec) {
        this.clock = clock;
        this.ttlMs = Math.min(ttlMs, MAX_WINDOW_MS);
        this.maxCandidates = maxCandidates;
        this.maxPerSource = maxPerSource;
        this.burst = burst;
        this.refillPerSec = refillPerSec;
        this.tokens = burst;
    }

    static NearbyBook standard() { return new NearbyBook(); }

    static boolean ridOk(String rid) {
        return rid != null && rid.matches("[0-9a-f]{32}");
    }

    private static String str(Object v) {
        if (v == null) return "";
        if (v instanceof byte[]) return new String((byte[]) v, StandardCharsets.UTF_8);
        return v.toString();
    }

    /** TXT (Android's NsdServiceInfo.getAttributes(): String -> byte[]) -> {v, rid, class} or null. */
    static Map<String, String> parseTxt(Map<String, ?> txt) {
        if (txt == null || txt.size() > MAX_TXT_KEYS) return null;
        Map<String, String> seen = new HashMap<>();
        int total = 0;
        for (Map.Entry<String, ?> e : txt.entrySet()) {
            String key = str(e.getKey()).trim().toLowerCase(Locale.ROOT);
            String val = str(e.getValue()).trim();
            total += key.length() + val.length() + 2;
            if (key.isEmpty() || seen.containsKey(key) || val.length() > MAX_TXT_VALUE
                    || total > MAX_TXT_BYTES) return null;
            if (FORBIDDEN_KEYS.contains(key)) return null;
            seen.put(key, val);
        }
        if (!ADVERT_VERSION.equals(seen.get("v"))) return null;
        String rid = seen.getOrDefault("rid", "");
        String cls = seen.getOrDefault("class", "").toLowerCase(Locale.ROOT);
        if (!ridOk(rid) || !DEVICE_CLASSES.contains(cls)) return null;
        Map<String, String> out = new LinkedHashMap<>();
        out.put("v", ADVERT_VERSION);
        out.put("rid", rid);
        out.put("class", cls);
        return out;
    }

    /** An advert's service name for display: printable, one line, short. Never verified. */
    static String cleanLabel(String name) {
        if (name == null) return "";
        StringBuilder b = new StringBuilder();
        name.codePoints().forEach(cp -> {
            int t = Character.getType(cp);
            if (t == Character.CONTROL || t == Character.FORMAT || t == Character.SURROGATE
                    || t == Character.PRIVATE_USE || t == Character.UNASSIGNED
                    || t == Character.LINE_SEPARATOR || t == Character.PARAGRAPH_SEPARATOR) return;
            b.appendCodePoint(cp);
        });
        String one = b.toString().trim().replaceAll("\\s+", " ");
        int n = one.codePointCount(0, one.length());
        return n <= MAX_LABEL ? one : one.substring(0, one.offsetByCodePoints(0, MAX_LABEL));
    }

    /** The approval page for one rid, or null when the rid is off-contract. */
    static String approveUrl(String rid) {
        return ridOk(rid) ? APPROVE_PAGE + rid : null;
    }

    static final class Candidate {
        final String rid;
        final String cls;
        final String label;
        final String source;
        final long firstSeen;
        long lastSeen;

        Candidate(String rid, String cls, String label, String source, long t) {
            this.rid = rid;
            this.cls = cls;
            this.label = label;
            this.source = source;
            this.firstSeen = t;
            this.lastSeen = t;
        }
    }

    private final LongSupplier clock;
    private final long ttlMs;
    private final int maxCandidates;
    private final int maxPerSource;
    private final double burst;
    private final double refillPerSec;
    private double tokens;
    private long lastRefill = Long.MIN_VALUE;
    private final Map<String, Candidate> cands = new LinkedHashMap<>();
    private final Map<String, Long> banned = new HashMap<>();
    int dropped;

    private boolean takeToken(long t) {
        if (lastRefill == Long.MIN_VALUE) lastRefill = t;
        tokens = Math.min(burst, tokens + ((t - lastRefill) / 1000.0) * refillPerSec);
        lastRefill = t;
        if (tokens < 1.0) return false;
        tokens -= 1.0;
        return true;
    }

    private void expire(long t) {
        for (Iterator<Candidate> it = cands.values().iterator(); it.hasNext(); ) {
            Candidate c = it.next();
            if (t - c.firstSeen > ttlMs) {
                // a rid lives one window: re-announcing it after that never brings it back
                it.remove();
                banned.put(c.rid, t + ttlMs);
            }
        }
        banned.values().removeIf(until -> t > until);
    }

    /** added refreshed rejected flood full source-cap conflict banned. */
    synchronized String offer(Map<String, ?> txt, String label, String source) {
        long t = clock.getAsLong();
        if (!takeToken(t)) { dropped++; return "flood"; }
        expire(t);
        Map<String, String> adv = parseTxt(txt);
        if (adv == null) { dropped++; return "rejected"; }
        String rid = adv.get("rid");
        String cls = adv.get("class");
        String src = str(source).trim();
        if (src.length() > 64) src = src.substring(0, 64);
        if (banned.containsKey(rid)) return "banned";
        Candidate have = cands.get(rid);
        if (have != null) {
            if (!have.source.equals(src) || !have.cls.equals(cls)) {
                cands.remove(rid);
                banned.put(rid, t + ttlMs);
                return "conflict";
            }
            have.lastSeen = t; // firstSeen kept: a refresh never extends the window
            return "refreshed";
        }
        int fromSource = 0;
        for (Candidate c : cands.values()) if (c.source.equals(src)) fromSource++;
        if (fromSource >= maxPerSource) { dropped++; return "source-cap"; }
        if (cands.size() >= maxCandidates) { dropped++; return "full"; }
        cands.put(rid, new Candidate(rid, cls, cleanLabel(label), src, t));
        return "added";
    }

    synchronized boolean has(String rid) {
        expire(clock.getAsLong());
        return rid != null && cands.containsKey(rid);
    }

    synchronized void clear() { cands.clear(); }

    synchronized List<Candidate> list() {
        expire(clock.getAsLong());
        return new ArrayList<>(cands.values());
    }

    /** The list as JSON for the page: rid, class, label, verified=false. Built by hand, escaped. */
    synchronized String json() {
        StringBuilder b = new StringBuilder("[");
        for (Candidate c : list()) {
            if (b.length() > 1) b.append(',');
            b.append("{\"rid\":\"").append(c.rid).append("\",\"class\":\"").append(c.cls)
                    .append("\",\"label\":").append(quote(c.label)).append(",\"verified\":false}");
        }
        return b.append(']').toString();
    }

    static String quote(String s) {
        StringBuilder b = new StringBuilder("\"");
        for (int i = 0; i < s.length(); i++) {
            char ch = s.charAt(i);
            if (ch == '"' || ch == '\\') b.append('\\').append(ch);
            else if (ch < 0x20 || ch == '<' || ch == '>' || ch == '&' || ch == ' ' || ch == ' ') {
                b.append(String.format("\\u%04x", (int) ch));
            } else b.append(ch);
        }
        return b.append('"').toString();
    }
}
