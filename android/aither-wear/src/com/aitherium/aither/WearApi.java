package com.aitherium.aither;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * The watch's own line to Aither over its Wi-Fi or LTE: no phone, no Google Play Services.
 *
 * Sign-in is the device grant (RFC 8628) the web already serves: POST /api/auth/device/code
 * mints a short code, the owner enters it on a signed-in phone at the page Identity names
 * (verification_uri, idp.aitherium.com/link; app.aitherium.com/auth/device takes it too), and /api/auth/device/token then hands this watch a bearer token for that
 * account (scope "session", decided at approval time by Identity). The token is kept in this
 * app's private storage and sent as Authorization.
 *
 * Staying signed in: the session is a sliding 30-day one, renewed through POST
 * /api/auth/refresh (Identity extends it; the token does not change) at most once every
 * RENEW_EVERY_MS, from the app and the tile. A 401 from any other endpoint is NOT taken at
 * its word: a Genesis that cannot reach Identity (a security-core restart, measured
 * 2026-10-08 14:38) answers a valid bearer with 401, and forgetting the token on that
 * signed the watch out silently. Only Identity's own refresh saying 401, twice and some
 * seconds apart, forgets it; when Identity is unreachable the token is kept and the call
 * reads as 503. Revoking the device on the account still ends it (refresh then fails).
 *
 * With it the watch reads the same inbox the phone's DutyService reads (/api/push/inbox),
 * answers with the same body (/api/push/decide via WearRules.answer), and asks the same
 * assistant the web desktop does (/api/agent-chat, Genesis /chat/stream).
 */
final class WearApi {
    static final String API = "https://api.aitherium.com";
    /** Where the owner enters the code when the answer names no verification_uri. */
    static final String VERIFY = "idp.aitherium.com/link";
    static final String CLIENT = "Aither on Wear OS";
    /** Who answers unless the owner picks another (the web desktop's default). */
    static final String AGENT = "aither";
    /** The owner's agents the watch offers: id, name. */
    static final String[][] AGENTS = {
            {"aither", "Aither"}, {"iris", "Iris"}, {"saga", "Saga"}, {"lyra", "Lyra"},
            {"atlas", "Atlas"}, {"demiurge", "Demi"}, {"hera", "Hera"}, {"vera", "Vera"},
    };
    /** One-tap asks on the home screen: label, what is asked, which agent ("" = the chosen one). */
    static final String[][] QUICK = {
            {"Fleet status", "In two short sentences: how is my fleet right now? Name anything down or degraded.", "aither"},
            {"Home", "In two short sentences: what's happening at home right now, and is anything waiting for me?", "aither"},
            {"My day", "In two short sentences: what's on my calendar and task list today?", "aither"},
            {"What did I miss?", "In two short sentences: what changed in the last hour that I should know about?", "aither"},
    };

    /** A known agent id, else the default (a stale or tampered pref never reaches the API). */
    static String agentId(String id) {
        for (String[] a : AGENTS) if (a[0].equals(id)) return id;
        return AGENT;
    }

    static String agentName(String id) {
        for (String[] a : AGENTS) if (a[0].equals(id)) return a[1];
        return "Aither";
    }

    private final SharedPreferences p;

    WearApi(Context c) {
        p = c.getSharedPreferences("wear", Context.MODE_PRIVATE);
    }

    /** The bearer, or "" (none, or past the expiry the grant gave). */
    String token() {
        long until = p.getLong("token_until", 0);
        if (until > 0 && System.currentTimeMillis() > until) signOut("the grant's expiry passed");
        return p.getString("token", "");
    }

    void keep(String token, long expiresInS) {
        p.edit().putString("token", token)
                .putLong("token_until", expiresInS > 0 ? System.currentTimeMillis() + expiresInS * 1000 : 0)
                .apply();
    }

    /** Forget the token. Every caller names why, so a sign-out is never a mystery in the log. */
    void signOut(String why) {
        android.util.Log.i("AitherWear", "signed out: " + why + " (had token: " + !p.getString("token", "").isEmpty() + ")");
        p.edit().remove("token").remove("token_until").remove("renewed_at").apply();
    }

    /** Renew the sliding session at most this often (Identity keeps 30 days from each renewal). */
    static final long RENEW_EVERY_MS = 20L * 3600_000L;
    /** Between the two refresh calls that must both say 401 before the token is forgotten. */
    static final long CONFIRM_GAP_MS = 8000;
    private static final Object SESSION = new Object();
    private static volatile long sessionOkAt;

    /** POST /api/auth/refresh with the bearer: Identity's own word on the session (and a renewal). */
    private int refresh() {
        String bearer = p.getString("token", "");
        if (bearer.isEmpty()) return 401;
        try {
            HttpURLConnection c = (HttpURLConnection) new URL(API + "/api/auth/refresh").openConnection();
            c.setRequestMethod("POST");
            c.setConnectTimeout(15000);
            c.setReadTimeout(20000);
            c.setDoOutput(true);
            c.setRequestProperty("Origin", "https://aitherium.com");
            c.setRequestProperty("Authorization", "Bearer " + bearer);
            c.setRequestProperty("Content-Type", "application/json");
            try (OutputStream o = c.getOutputStream()) { o.write("{}".getBytes(StandardCharsets.UTF_8)); }
            int code = c.getResponseCode();
            c.disconnect();
            if (code == 200) {
                sessionOkAt = System.currentTimeMillis();
                p.edit().putLong("renewed_at", sessionOkAt).apply();
            }
            return code;
        } catch (Exception e) {
            return 0;
        }
    }

    /**
     * Is the session really gone? 200: alive (renewed); 401: Identity refused it twice,
     * CONFIRM_GAP_MS apart, and the token is forgotten; anything else: Identity could not
     * be asked, the token is kept.
     */
    int confirmSession() {
        synchronized (SESSION) {
            if (System.currentTimeMillis() - sessionOkAt < 30_000) return 200;
            int first = refresh();
            if (!WearRules.sessionRefused(first)) return first;
            try { Thread.sleep(CONFIRM_GAP_MS); } catch (InterruptedException e) { return 0; }
            int second = refresh();
            if (WearRules.sessionRefused(second)) {
                android.util.Log.i("AitherWear", "session refused by Identity twice: signed out");
                signOut("Identity refused the session twice");
            }
            return second;
        }
    }

    /** Keep the sliding session alive: renew when the last renewal is older than RENEW_EVERY_MS. */
    void renewIfDue() {
        if (p.getString("token", "").isEmpty()) return;
        if (!WearRules.renewDue(p.getLong("renewed_at", 0), System.currentTimeMillis(), RENEW_EVERY_MS)) return;
        if (WearRules.sessionRefused(refresh())) confirmSession();
    }

    /** One answer: HTTP status (0 = unreachable), the JSON body when there is one, the text. */
    static final class Resp {
        final int code;
        final JSONObject json;
        final String text;

        Resp(int code, JSONObject json, String text) {
            this.code = code;
            this.json = json;
            this.text = text;
        }

        String str(String k) { return json == null ? "" : json.optString(k, ""); }
    }

    Resp post(String path, JSONObject body, boolean auth, int readMs) {
        return call("POST", path, body == null ? null : body.toString().getBytes(StandardCharsets.UTF_8),
                "application/json", auth, readMs);
    }

    /** POST one audio file as multipart field {@code audio} (POST /api/voice/hear). */
    Resp postAudio(String path, byte[] audio, String filename, String mime, int readMs) {
        String boundary = "aither" + java.util.UUID.randomUUID().toString().replace("-", "");
        return call("POST", path, WearMicRules.multipart(boundary, filename, mime, audio),
                "multipart/form-data; boundary=" + boundary, true, readMs);
    }

    Resp get(String path, int readMs) {
        return call("GET", path, null, null, true, readMs);
    }

    private Resp call(String method, String path, byte[] body, String type, boolean auth, int readMs) {
        Resp r = call1(method, path, body, type, auth, readMs);
        if (!auth || r.code != 401) return r;
        // a 401 from a service is checked with Identity before anything is forgotten
        int s = confirmSession();
        if (s == 200) return call1(method, path, body, type, auth, readMs);
        return WearRules.sessionRefused(s) ? r : new Resp(503, null, "");
    }

    private Resp call1(String method, String path, byte[] body, String type, boolean auth, int readMs) {
        String bearer = auth ? token() : "";
        if (auth && bearer.isEmpty()) return new Resp(401, null, "");
        try {
            HttpURLConnection c = (HttpURLConnection) new URL(API + path).openConnection();
            c.setRequestMethod(method);
            c.setConnectTimeout(20000);
            c.setReadTimeout(readMs);
            c.setRequestProperty("Origin", "https://aitherium.com");
            if (auth) c.setRequestProperty("Authorization", "Bearer " + bearer);
            if (body != null) {
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", type);
                c.setFixedLengthStreamingMode(body.length);
                try (OutputStream o = c.getOutputStream()) {
                    o.write(body);
                }
            }
            int code = c.getResponseCode();
            StringBuilder sb = new StringBuilder();
            try (InputStream in = code < 400 ? c.getInputStream() : c.getErrorStream()) {
                if (in != null) {
                    byte[] b = new byte[8192];
                    int n;
                    while ((n = in.read(b)) > 0 && sb.length() < 2_000_000) {
                        sb.append(new String(b, 0, n, StandardCharsets.UTF_8));
                    }
                }
            }
            JSONObject j = null;
            try { j = new JSONObject(sb.toString()); } catch (Exception e) { /* not JSON (SSE) */ }
            return new Resp(code, j, sb.toString());
        } catch (Exception e) {
            return new Resp(0, null, "");
        }
    }

    // ------------------------------------------------------------------ decisions

    /** The owner's open decision cards (GET /api/decisions?status=open), or null (why in status[0]). */
    List<WearDecision> decisions(int[] status) {
        Resp r = get("/api/decisions?status=open", 20000);
        status[0] = r.code;
        if (r.code != 200 || r.json == null) return null;
        return WearDecision.parseAll(r.json.optJSONArray("decisions"));
    }

    /** POST /api/decisions/{id}/answer. 403 means this answer needs a passkey (the phone). */
    Resp answer(WearDecision d, String choice) {
        if (!WearDecision.validId(d.id)) return new Resp(400, null, "");
        try {
            return post("/api/decisions/" + d.id + "/answer",
                    new JSONObject().put("choice", choice).put("note", "answered on the watch"), true, 30000);
        } catch (Exception e) {
            return new Resp(0, null, "");
        }
    }

    // ------------------------------------------------------------------ sign-in

    Resp deviceCode() {
        try {
            return post("/api/auth/device/code", new JSONObject().put("client_name", CLIENT), false, 20000);
        } catch (Exception e) {
            return new Resp(0, null, "");
        }
    }

    Resp deviceToken(String deviceCode) {
        try {
            return post("/api/auth/device/token", new JSONObject().put("device_code", deviceCode), false, 20000);
        } catch (Exception e) {
            return new Resp(0, null, "");
        }
    }

    // ------------------------------------------------------------------ approvals

    /** Every card the inbox still holds for this account, by id. Null when it could not be read. */
    Map<String, ApprovalCard> inbox(int[] status) {
        Map<String, ApprovalCard> byId = WearRules.byId();
        double since = 0;
        for (int pages = 0; ; ) {
            Resp r;
            try {
                r = post("/api/push/inbox", new JSONObject().put("since", since).put("wait_s", 0), true, 30000);
            } catch (Exception e) {
                r = new Resp(0, null, "");
            }
            status[0] = r.code;
            if (r.code != 200 || r.json == null) return null;
            JSONArray list = r.json.optJSONArray("notices");
            List<ApprovalCard> page = new ArrayList<>();
            for (int i = 0; list != null && i < list.length(); i++) page.add(parse(list.optJSONObject(i)));
            WearRules.merge(byId, page);
            pages++;
            double next = r.json.optDouble("cursor", since);
            if (!WearRules.more(page.size(), pages) || next <= since) return byId;
            since = next;
        }
    }

    /** The phone's Notices.parse, field for field. */
    static ApprovalCard parse(JSONObject n) {
        if (n == null || !ApprovalCard.validId(n.optString("notice_id"))) return null;
        JSONObject a = n.optJSONObject("approval");
        return new ApprovalCard(n.optString("notice_id"), n.optString("kind"), n.optString("title"),
                n.optString("body"), n.optBoolean("urgent"), n.optBoolean("quiet"),
                a == null ? "" : a.optString("digest"), a == null ? "" : a.optString("state"),
                a == null ? 0 : a.optInt("have"), a == null ? 0 : a.optInt("need", 1),
                a == null ? "" : a.optString("mine"), a == null ? "" : a.optString("denied_by"),
                a == null ? 0 : a.optDouble("ran_at", 0));
    }

    /** POST /api/push/decide with the body WearRules.answer built for the card shown. */
    Resp decide(ApprovalCard card, boolean allow) {
        String body = WearRules.answer(card, allow);
        if (body == null) return new Resp(400, null, "");
        try {
            return post("/api/push/decide", new JSONObject(body), true, 60000);
        } catch (Exception e) {
            return new Resp(0, null, "");
        }
    }

    // ------------------------------------------------------------------ assistant

    /** What a streamed answer reports, in order, on the reading thread. */
    interface Stream {
        void segment(String kind);
        void token(String text);
        void segmentEnd();
        /** The terminal answer (may arrive long after the last token, or never). */
        void answer(String text);
        void error(String why);
    }

    /**
     * Ask {@code agent} and hand each event over as it arrives (Genesis' eager protocol:
     * answer_segment, token, segment_end, then answer/complete). Returns the HTTP status
     * (0: unreachable). {@code alive} is polled between events: false hangs up.
     */
    int stream(String message, String agent, Stream to, java.util.function.BooleanSupplier alive) {
        return stream(message, agent, "", to, alive);
    }

    /**
     * {@code session}: the conversation's thread id ("" for a one-off ask). Every turn of one
     * watch conversation carries the same id, so Genesis answers a follow-up with what was
     * said before (verified live 2026-10-08: "what was my test word?" answered from turn 1).
     */
    int stream(String message, String agent, String session, Stream to, java.util.function.BooleanSupplier alive) {
        int code = stream1(message, agent, session, to, alive);
        if (code != 401) return code;
        int s = confirmSession(); // a 401 is checked with Identity before anything is forgotten
        if (s == 200) return stream1(message, agent, session, to, alive);
        return WearRules.sessionRefused(s) ? 401 : 503;
    }

    private int stream1(String message, String agent, String session, Stream to,
                        java.util.function.BooleanSupplier alive) {
        String bearer = token();
        if (bearer.isEmpty()) return 401;
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(API + "/api/agent-chat").openConnection();
            c.setRequestMethod("POST");
            c.setConnectTimeout(20000);
            c.setReadTimeout(120000);
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json");
            c.setRequestProperty("Accept", "text/event-stream");
            c.setRequestProperty("Origin", "https://aitherium.com");
            c.setRequestProperty("Authorization", "Bearer " + bearer);
            JSONObject req = new JSONObject().put("message", message).put("agent", agent).put("client", CLIENT);
            if (session != null && !session.isEmpty()) req.put("session_id", session);
            byte[] body = req.toString().getBytes(StandardCharsets.UTF_8);
            c.setFixedLengthStreamingMode(body.length);
            try (OutputStream o = c.getOutputStream()) {
                o.write(body);
            }
            int code = c.getResponseCode();
            if (code != 200) return code;
            boolean answered = false;
            try (java.io.BufferedReader in = new java.io.BufferedReader(
                    new java.io.InputStreamReader(c.getInputStream(), StandardCharsets.UTF_8))) {
                String line;
                while ((line = in.readLine()) != null) {
                    if (!alive.getAsBoolean()) break;
                    if (!line.startsWith("data:")) continue;
                    JSONObject e;
                    try { e = new JSONObject(line.substring(5).trim()); } catch (Exception x) { continue; }
                    switch (e.optString("type")) {
                        case "answer_segment": to.segment(e.optString("kind", "initial")); break;
                        case "token": to.token(e.optString("t", "")); break;
                        case "segment_end": to.segmentEnd(); break;
                        case "answer":
                        case "final_answer":
                            if (!answered) {
                                String a = e.optString("answer", e.optString("content", ""));
                                if (!a.isEmpty()) { answered = true; to.answer(a); }
                            }
                            break;
                        case "complete":
                            if (!answered && !e.optString("content").isEmpty()) to.answer(e.optString("content"));
                            return 200;
                        case "error":
                            if (!e.optString("error").isEmpty()) to.error(e.optString("error"));
                            break;
                        default: break;
                    }
                }
            }
            return 200;
        } catch (Exception e) {
            return 0;
        } finally {
            if (c != null) c.disconnect();
        }
    }
}
