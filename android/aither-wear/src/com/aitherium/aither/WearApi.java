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
 * app's private storage and sent as Authorization; a 401 anywhere forgets it.
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
    static final String AGENT = "aeon";

    private final SharedPreferences p;

    WearApi(Context c) {
        p = c.getSharedPreferences("wear", Context.MODE_PRIVATE);
    }

    /** The bearer, or "" (none, or past the expiry the grant gave). */
    String token() {
        long until = p.getLong("token_until", 0);
        if (until > 0 && System.currentTimeMillis() > until) signOut();
        return p.getString("token", "");
    }

    void keep(String token, long expiresInS) {
        p.edit().putString("token", token)
                .putLong("token_until", expiresInS > 0 ? System.currentTimeMillis() + expiresInS * 1000 : 0)
                .apply();
    }

    void signOut() {
        p.edit().remove("token").remove("token_until").apply();
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
        String bearer = auth ? token() : "";
        if (auth && bearer.isEmpty()) return new Resp(401, null, "");
        try {
            HttpURLConnection c = (HttpURLConnection) new URL(API + path).openConnection();
            c.setRequestMethod("POST");
            c.setConnectTimeout(20000);
            c.setReadTimeout(readMs);
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json");
            c.setRequestProperty("Origin", "https://aitherium.com");
            if (auth) c.setRequestProperty("Authorization", "Bearer " + bearer);
            try (OutputStream o = c.getOutputStream()) {
                o.write(body.toString().getBytes(StandardCharsets.UTF_8));
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
            if (auth && code == 401) signOut();
            JSONObject j = null;
            try { j = new JSONObject(sb.toString()); } catch (Exception e) { /* not JSON (SSE) */ }
            return new Resp(code, j, sb.toString());
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

    /** Ask the assistant; the answer text, or "" with why in err[0]. */
    String ask(String message, String[] err) {
        Resp r;
        try {
            r = post("/api/agent-chat", new JSONObject().put("message", message).put("agent", AGENT), true, 120000);
        } catch (Exception e) {
            r = new Resp(0, null, "");
        }
        if (r.code == 401) { err[0] = "Signed out. Sign in again."; return ""; }
        if (r.code != 200) { err[0] = r.code == 0 ? "No connection." : "Aither answered " + r.code + "."; return ""; }
        String answer = "", complete = "";
        for (String line : r.text.split("\n")) {
            if (!line.startsWith("data: ")) continue;
            try {
                JSONObject e = new JSONObject(line.substring(6));
                String type = e.optString("type");
                if ("answer".equals(type) && !e.optString("answer").isEmpty()) answer = e.optString("answer");
                else if ("complete".equals(type) && !e.optString("content").isEmpty()) complete = e.optString("content");
                else if ("error".equals(type) && !e.optString("error").isEmpty()) err[0] = e.optString("error");
            } catch (Exception ignored) { /* a heartbeat or a partial line */ }
        }
        String out = answer.isEmpty() ? complete : answer;
        if (out.isEmpty() && err[0] == null) err[0] = "No answer came back.";
        return out;
    }
}
