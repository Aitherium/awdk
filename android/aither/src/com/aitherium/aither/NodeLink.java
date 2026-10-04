package com.aitherium.aither;

import android.content.Context;
import android.content.SharedPreferences;
import android.webkit.CookieManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * This phone as an Identity node, the same record the household page shows (family's
 * device row is linked to it with link-node), and the device half of the command channel.
 *
 * Linking needs no taps: with the owner signed in to AitherOS in this app, it asks the
 * portal for a pairing code (POST /api/me/machines, the page's own session cookie),
 * confirms it with Identity as this phone, and keeps the answer's device token and
 * command key in app-private storage. A phone the owner paired for lending already has
 * both from that confirm ({@link #remember}).
 *
 * The check-in (every 15 minutes, and when AitherOS is opened) beats with the device
 * token and runs what came back through {@link Commands}, which refuses anything not
 * signed for this phone.
 */
final class NodeLink {
    static final String PORTAL = "https://app.aitherium.com";
    static final String FAMILY = "https://api.aitherium.com/api/tutor/me/device";
    static final String UA = "Mozilla/5.0 (Linux; Android) AitherAndroid/" + Config.VERSION;
    static volatile String last = "not linked";

    private final Context ctx;
    private final SharedPreferences p;
    private final Config cfg;

    NodeLink(Context c) {
        ctx = c.getApplicationContext();
        p = ctx.getSharedPreferences("node", Context.MODE_PRIVATE);
        cfg = new Config(ctx);
    }

    String nodeId() { return p.getString("node_id", ""); }
    String bearer() { return p.getString("bearer", ""); }
    String key() { return p.getString("key", ""); }
    boolean linked() { return !nodeId().isEmpty() && !bearer().isEmpty(); }
    SharedPreferences prefs() { return p; }

    /** Keep what an Identity confirm answered (here or in the lending pairing). */
    boolean remember(JSONObject confirm) {
        String node = confirm.optString("node_id", ""), tok = confirm.optString("bearer_token", "");
        if (node.isEmpty() || tok.isEmpty()) return false;
        p.edit().putString("node_id", node).putString("bearer", tok)
                .putString("key", confirm.optString("command_key", ""))
                .putBoolean("family_linked", false).apply();
        last = "linked as " + node;
        return true;
    }

    void forget(String why) {
        p.edit().remove("node_id").remove("bearer").remove("key").remove("family_linked").apply();
        last = why;
    }

    /** The node id this phone uses: its lending id when it has one, else its household id. */
    String wantedId() {
        if (!cfg.deviceId().isEmpty()) return cfg.deviceId();
        try {
            return new JSONObject(cfg.familyDevice()).optString("device_id", "");
        } catch (Exception e) {
            return "";
        }
    }

    /**
     * A DNS-safe name for Identity's hostname field, which refuses '_' (a household id is
     * fdev_...): the phone's model, lower-cased, anything outside [a-z0-9-] made '-'.
     */
    static String hostname(String model) {
        String h = (model == null ? "" : model).toLowerCase(java.util.Locale.ROOT)
                .replaceAll("[^a-z0-9-]+", "-").replaceAll("^-+|-+$", "");
        if (h.length() > 63) h = h.substring(0, 63).replaceAll("-+$", "");
        return h.isEmpty() ? "android-phone" : h;
    }

    /** Link this phone if it is not yet, then tie the household row to it. Never throws. */
    void ensureLinked() {
        try {
            if (!linked()) link();
            if (linked() && !p.getBoolean("family_linked", false)) linkFamily();
        } catch (Exception e) {
            last = "link failed: " + e.getClass().getSimpleName();
        }
    }

    private void link() throws Exception {
        String id = wantedId();
        if (id.isEmpty()) {
            last = "not linked: open AitherOS and add this phone to your household";
            return;
        }
        // a guardian's QR carried a code for this phone (a child cannot mint one); else the
        // signed-in owner's session mints one
        String code = cfg.takePairCode();
        if (code.isEmpty()) {
            String cookie = CookieManager.getInstance().getCookie(PORTAL);
            if (cookie == null || cookie.isEmpty()) {
                last = "not linked: sign in to AitherOS on this phone";
                return;
            }
            Resp mint = call("POST", PORTAL + "/api/me/machines", null, cookie, "{}");
            code = mint.code == 200 ? mint.json().optString("code", "") : "";
            if (code.isEmpty()) {
                // 402 = no plan or no room; 401 = signed out. Said plainly, retried next open.
                last = "not linked: the portal answered " + mint.code + " " + mint.json().optString("error", "");
                return;
            }
        }
        JSONObject body = new JSONObject()
                .put("code", code)
                .put("node_id", id)
                .put("hostname", hostname(android.os.Build.MODEL))
                .put("platform", "android")
                .put("node_class", "phone")
                .put("inference_kind", "llama-server")
                .put("seal_pubkey", cfg.pubkey())
                .put("cpu_count", Runtime.getRuntime().availableProcessors())
                .put("capabilities", new JSONArray().put("kvholder").put("commands"));
        String idp = cfg.identity().isEmpty() ? "https://idp.aitherium.com" : cfg.identity();
        Resp r = call("POST", idp + "/v1/nodes/pairing/confirm", null, null, body.toString());
        if (r.code != 200 || !remember(r.json())) {
            last = "not linked: Identity answered " + r.code;
        }
    }

    private void linkFamily() throws Exception {
        String token;
        try {
            token = new JSONObject(cfg.familyDevice()).optString("token", "");
        } catch (Exception e) {
            token = "";
        }
        if (token.isEmpty()) return; // not in a household: the node stands on its own
        String cookie = CookieManager.getInstance().getCookie("https://api.aitherium.com");
        JSONObject b = new JSONObject().put("token", token).put("node_id", nodeId());
        Resp r = call("POST", FAMILY + "/link-node", null, cookie, b.toString());
        if (r.code == 200) p.edit().putBoolean("family_linked", true).apply();
    }

    /** One check-in: beat, run what came back, report. True when done (no retry needed). */
    boolean checkIn() {
        if (!linked()) return true;
        try {
            String idp = cfg.identity().isEmpty() ? "https://idp.aitherium.com" : cfg.identity();
            JSONObject beat = new JSONObject()
                    .put("node_id", nodeId())
                    .put("inference_ready", modelReady())
                    .put("available_models", cfg.localAiBlocked().isEmpty()
                            ? new JSONArray().put(LlmService.MODEL_ID) : new JSONArray())
                    .put("inference_kind", cfg.llmEnabled() ? "llama-server" : "none")
                    .put("reach_kind", "none");
            Resp r = call("POST", idp + "/v1/nodes/device/heartbeat", bearer(), null, beat.toString());
            if (r.code == 401 || r.code == 403) {
                forget("unlinked: the device token was refused (" + r.code + ")");
                return true;
            }
            if (r.code != 200) {
                last = "check-in answered " + r.code;
                return false;
            }
            JSONObject j = r.json();
            String status = j.optString("status", "");
            if ("unknown_node".equals(status)) {
                forget("removed from the workspace: will link again when AitherOS is opened");
                return true;
            }
            if ("suspended".equals(status)) {
                last = "paused by the owner";
                return true;
            }
            JSONArray cmds = j.optJSONArray("commands");
            int ran = 0;
            if (cmds != null) {
                Commands run = new Commands(ctx, this);
                for (int i = 0; i < cmds.length(); i++) {
                    JSONObject result = run.handle(cmds.optJSONObject(i));
                    if (result == null) continue;
                    ran++;
                    call("POST", idp + "/v1/nodes/device/results/" + result.getString("id"),
                            bearer(), null, result.toString());
                }
            }
            last = "checked in " + new java.util.Date() + (ran > 0 ? " · ran " + ran : "");
            return true;
        } catch (Exception e) {
            last = "check-in failed: " + e.getClass().getSimpleName();
            return false;
        }
    }

    /** The model is on the phone, checked for size, and this phone may run it. */
    boolean modelReady() {
        java.io.File m = new java.io.File(ctx.getFilesDir(), "models/" + LlmService.MODEL_FILE);
        return cfg.llmEnabled() && cfg.localAiBlocked().isEmpty()
                && m.exists() && m.length() == LlmService.MODEL_BYTES;
    }

    // ------------------------------------------------------------ http

    static final class Resp {
        final int code;
        final String body;

        Resp(int code, String body) {
            this.code = code;
            this.body = body;
        }

        JSONObject json() {
            try {
                return new JSONObject(body);
            } catch (Exception e) {
                return new JSONObject();
            }
        }
    }

    static Resp call(String method, String url, String bearer, String cookie, String json)
            throws java.io.IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setRequestMethod(method);
        c.setConnectTimeout(20000);
        c.setReadTimeout(60000);
        c.setRequestProperty("User-Agent", UA);
        c.setRequestProperty("Accept", "application/json");
        if (bearer != null) c.setRequestProperty("Authorization", "Bearer " + bearer);
        if (cookie != null) {
            c.setRequestProperty("Cookie", cookie);
            c.setRequestProperty("Origin", url.startsWith(PORTAL) ? PORTAL : "https://aitherium.com");
        }
        if (json != null) {
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json");
            try (OutputStream o = c.getOutputStream()) {
                o.write(json.getBytes(StandardCharsets.UTF_8));
            }
        }
        int code = c.getResponseCode();
        InputStream in = code < 400 ? c.getInputStream() : c.getErrorStream();
        return new Resp(code, in == null ? "" : read(in, 1 << 20));
    }

    static String read(InputStream in, int max) throws java.io.IOException {
        ByteArrayOutputStream b = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        try {
            while (b.size() < max && (n = in.read(buf)) > 0) b.write(buf, 0, n);
        } finally {
            in.close();
        }
        return b.toString("UTF-8");
    }
}
