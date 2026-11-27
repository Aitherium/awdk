package com.aitherium.aither;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;
import android.util.Base64;
import android.webkit.CookieManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.security.cert.X509Certificate;
import java.util.ArrayList;
import java.util.List;

import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLSocketFactory;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

/**
 * Direct AitherMesh client on the phone: talks to awnix-voice-remote over the owner's private
 * Tailscale mesh (DGX Spark 100.64.0.38:9448, 5090 PC 100.64.0.46:9449, OptiPlex) with the
 * paired bearer token. Never active on a child's phone.
 *
 * Self-signed HTTPS is accepted ONLY for private mesh/LAN hosts validated by
 * {@link MeshRemote#isPrivateMeshHost(String)}; public hosts are refused outright.
 */
final class MeshClient {
    static final String PREFS = "mesh_remote";
    static final String CLOUD_API = "https://api.aitherium.com/api/ecosystem/voice-remote";
    private static volatile SSLSocketFactory meshSsl;

    static final class NodeInfo {
        final String id;
        final String name;
        final boolean online;
        final String posture;
        final String model;
        final String gpu;
        final double freeGib;
        final List<String> availablePostures;

        NodeInfo(String id, String name, boolean online, String posture, String model, String gpu, double freeGib) {
            this(id, name, online, posture, model, gpu, freeGib, new ArrayList<>());
        }

        NodeInfo(String id, String name, boolean online, String posture, String model, String gpu, double freeGib, List<String> availablePostures) {
            this.id = id;
            this.name = name;
            this.online = online;
            this.posture = posture;
            this.model = model;
            this.gpu = gpu;
            this.freeGib = freeGib;
            this.availablePostures = availablePostures != null ? availablePostures : new ArrayList<>();
        }

        String summary() {
            if (!online) return name + " · offline";
            StringBuilder sb = new StringBuilder(name).append(" · online");
            if (!posture.isEmpty()) sb.append(" · ").append(posture);
            if (!model.isEmpty()) sb.append(" · ").append(model);
            if (freeGib > 0) sb.append(String.format(java.util.Locale.ROOT, " · %.0f GiB free", freeGib));
            return sb.toString();
        }
    }

    static final class ChatReply {
        boolean ok;
        String reply = "";
        String node = "";
        String model = "";
        String error = "";
        byte[] audio;
    }

    static final class PostureResult {
        boolean ok;
        String posture = "";
        String node = "";
        String detail = "";
    }

    private final Context ctx;
    private final SharedPreferences p;

    MeshClient(Context c) {
        ctx = c.getApplicationContext();
        p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    boolean allowed() {
        Config cfg = new Config(ctx);
        return !cfg.childDevice() && !"child".equals(cfg.profileKind());
    }

    String primaryHub() {
        String s = MeshRemote.normalizeHubUrl(p.getString("hub", MeshRemote.DEFAULT_PRIMARY_HUB));
        return s.isEmpty() ? MeshRemote.DEFAULT_PRIMARY_HUB : s;
    }

    String fallbackHub() {
        String s = MeshRemote.normalizeHubUrl(p.getString("fallback_hub", MeshRemote.DEFAULT_FALLBACK_HUB));
        return s.isEmpty() ? MeshRemote.DEFAULT_FALLBACK_HUB : s;
    }

    String token() {
        return p.getString("token", "");
    }

    boolean directPaired() {
        return allowed() && !primaryHub().isEmpty() && MeshRemote.validToken(token());
    }

    private String cloudCookie() {
        try {
            String c = CookieManager.getInstance().getCookie("https://api.aitherium.com");
            return c == null ? "" : c.trim();
        } catch (Throwable ignored) {
            return "";
        }
    }

    boolean cloudSignedIn() {
        return allowed() && !cloudCookie().isEmpty();
    }

    /** True when either directly paired over Tailscale/LAN OR signed into Aitherium (zero-QR cloud relay). */
    boolean paired() {
        return directPaired() || cloudSignedIn();
    }

    /** "phone" | "auto" | "dgx-spark" | "pc" | "optiplex". */
    String target() {
        String t = p.getString("target", paired() ? "auto" : "phone");
        if ("phone".equals(t) || MeshRemote.validNode(t)) return t;
        return "phone";
    }

    void setTarget(String t) {
        if ("phone".equals(t) || MeshRemote.validNode(t)) {
            p.edit().putString("target", t).apply();
        }
    }

    boolean saveConfig(String hub, String fallback, String tok) {
        String h1 = MeshRemote.normalizeHubUrl(hub);
        String h2 = fallback == null || fallback.trim().isEmpty() ? "" : MeshRemote.normalizeHubUrl(fallback);
        if (h1.isEmpty() || !MeshRemote.validToken(tok)) return false;
        SharedPreferences.Editor e = p.edit().putString("hub", h1).putString("token", tok.trim());
        if (!h2.isEmpty()) e.putString("fallback_hub", h2);
        if ("phone".equals(p.getString("target", "phone"))) e.putString("target", "auto");
        e.apply();
        return true;
    }

    boolean acceptPairUri(Uri uri) {
        if (uri == null || !allowed()) return false;
        MeshRemote.Pair pair = MeshRemote.parsePairUrl(uri.toString());
        if (pair == null) return false;
        SharedPreferences.Editor e = p.edit()
                .putString("hub", pair.hub)
                .putString("token", pair.token);
        if (!pair.node.isEmpty()) e.putString("target", pair.node);
        else if ("phone".equals(p.getString("target", "phone"))) e.putString("target", "auto");
        e.apply();
        return true;
    }

    String statusSummary() {
        if (!allowed()) return "Not available on a child's phone";
        if (directPaired()) {
            return "Paired · target: " + MeshRemote.nodeLabel(target()) + " · " + primaryHub().replaceFirst("^https://", "");
        }
        if (cloudSignedIn()) {
            return "Connected via Aitherium (zero-QR) · target: " + MeshRemote.nodeLabel(target());
        }
        return "Sign in to Aitherium or scan a mesh QR to connect your nodes";
    }

    private List<String> hubs() {
        List<String> list = new ArrayList<>();
        String h1 = primaryHub();
        String h2 = fallbackHub();
        if (!h1.isEmpty()) list.add(h1);
        if (!h2.isEmpty() && !h2.equals(h1)) list.add(h2);
        return list;
    }

    private static List<NodeInfo> parseNodes(String body) throws Exception {
        List<NodeInfo> out = new ArrayList<>();
        JSONObject j = new JSONObject(body);
        JSONArray arr = j.optJSONArray("nodes");
        if (arr == null) arr = j.optJSONArray("peers");
        if (arr != null) {
            for (int i = 0; i < arr.length(); i++) {
                JSONObject o = arr.optJSONObject(i);
                if (o == null) continue;
                List<String> avail = new ArrayList<>();
                JSONArray pa = o.optJSONArray("available_postures");
                for (int k = 0; pa != null && k < pa.length(); k++) {
                    String pv = pa.optString(k, "").trim();
                    if (!pv.isEmpty()) avail.add(pv);
                }
                out.add(new NodeInfo(
                        o.optString("id", ""),
                        o.optString("name", o.optString("label", o.optString("id", ""))),
                        o.optBoolean("online", false),
                        o.optString("posture", ""),
                        o.optString("llm_model", o.optString("model", "")),
                        o.optString("gpu", ""),
                        o.optDouble("free_gib", o.optDouble("mem_avail_gib", 0)),
                        avail));
            }
        }
        return out;
    }

    /** Fetch all known AitherMesh nodes: direct LAN/Tailscale hub first, then zero-QR Aitherium cloud relay. */
    List<NodeInfo> fetchNodes(String[] errOut) {
        List<NodeInfo> out = new ArrayList<>();
        if (!paired()) {
            if (errOut != null && errOut.length > 0) errOut[0] = "Sign in to Aitherium or pair a mesh hub first.";
            return out;
        }
        String lastErr = "No mesh hub answered.";
        if (directPaired()) {
            for (String hub : hubs()) {
                try {
                    Resp r = request("GET", hub + "/api/nodes", null, 4000, 8000);
                    if (r.code == 401 || r.code == 403) {
                        lastErr = "Token refused by " + hub + " (" + r.code + ").";
                        continue;
                    }
                    if (r.code != 200) {
                        lastErr = hub + " answered " + r.code;
                        continue;
                    }
                    out = parseNodes(r.body);
                    if (errOut != null && errOut.length > 0) errOut[0] = "";
                    return out;
                } catch (Exception e) {
                    lastErr = e.getClass().getSimpleName() + ": " + e.getMessage();
                }
            }
        }
        if (cloudSignedIn()) {
            try {
                Resp r = cloudRequest("GET", CLOUD_API + "/nodes", null, 6000, 15000);
                if (r.code == 200) {
                    out = parseNodes(r.body);
                    if (errOut != null && errOut.length > 0) errOut[0] = "";
                    return out;
                }
                lastErr = "Aitherium relay answered " + r.code;
            } catch (Exception e) {
                lastErr = e.getClass().getSimpleName() + ": " + e.getMessage();
            }
        }
        if (errOut != null && errOut.length > 0) errOut[0] = lastErr;
        return out;
    }

    /** Send a voice/text turn to the chosen AitherMesh node: direct mesh first, then zero-QR Aitherium cloud relay. */
    ChatReply chat(String text, String node, boolean speak) {
        ChatReply out = new ChatReply();
        if (!paired()) {
            out.error = "Sign in to Aitherium or pair AitherMesh in Settings.";
            return out;
        }
        String body = MeshRemote.chatBody(text, node, speak);
        String lastErr = "No mesh node answered.";
        if (directPaired()) {
            for (String hub : hubs()) {
                try {
                    Resp r = request("POST", hub + "/api/chat", body, 4000, 90000);
                    if (r.code == 401 || r.code == 403) {
                        lastErr = "Mesh token rejected (" + r.code + ").";
                        continue;
                    }
                    if (r.code != 200) {
                        lastErr = "Mesh node answered " + r.code;
                        continue;
                    }
                    JSONObject j = new JSONObject(r.body);
                    out.ok = true;
                    out.reply = j.optString("reply", "").trim();
                    out.node = j.optString("node", j.optString("target", node));
                    out.model = j.optString("model", "");
                    String b64 = j.optString("audio_b64", j.optString("audio_wav_b64", ""));
                    if (!b64.isEmpty()) {
                        try { out.audio = Base64.decode(b64, Base64.DEFAULT); } catch (Exception ignored) { /* text stays */ }
                    }
                    return out;
                } catch (Exception e) {
                    lastErr = e.getClass().getSimpleName() + (e.getMessage() == null ? "" : ": " + e.getMessage());
                }
            }
        }
        if (cloudSignedIn()) {
            try {
                Resp r = cloudRequest("POST", CLOUD_API + "/ask", body, 6000, 60000);
                if (r.code == 200) {
                    JSONObject j = new JSONObject(r.body);
                    out.ok = true;
                    out.reply = j.optString("reply", "").trim();
                    out.node = j.optString("target", j.optString("node", node));
                    out.model = j.optString("model", "");
                    String b64 = j.optString("audio_wav_b64", j.optString("audio_b64", ""));
                    if (!b64.isEmpty()) {
                        try { out.audio = Base64.decode(b64, Base64.DEFAULT); } catch (Exception ignored) { /* text stays */ }
                    }
                    return out;
                }
                lastErr = "Aitherium relay answered " + r.code;
            } catch (Exception e) {
                lastErr = e.getClass().getSimpleName() + (e.getMessage() == null ? "" : ": " + e.getMessage());
            }
        }
        out.error = lastErr;
        return out;
    }

    /** Switch model posture on an AitherMesh node: direct mesh first, then zero-QR Aitherium cloud relay. */
    PostureResult setPosture(String posture, String node) {
        PostureResult out = new PostureResult();
        out.posture = posture;
        out.node = node;
        if (!paired()) {
            out.detail = "Sign in to Aitherium or pair AitherMesh first.";
            return out;
        }
        String body = MeshRemote.postureBody(posture, node);
        if (body.isEmpty()) {
            out.detail = "Unknown posture: " + posture;
            return out;
        }
        String lastErr = "No mesh hub answered.";
        if (directPaired()) {
            for (String hub : hubs()) {
                try {
                    Resp r = request("POST", hub + "/api/posture", body, 4000, 60000);
                    if (r.code == 200) {
                        JSONObject j = new JSONObject(r.body);
                        out.ok = j.optBoolean("ok", true);
                        out.posture = j.optString("posture", posture);
                        out.node = j.optString("node", node);
                        out.detail = j.optString("message", j.optString("detail", out.posture + " applied on " + out.node));
                        return out;
                    }
                    lastErr = "Mesh answered " + r.code + ": " + r.body;
                } catch (Exception e) {
                    lastErr = e.getClass().getSimpleName() + ": " + e.getMessage();
                }
            }
        }
        if (cloudSignedIn()) {
            try {
                Resp r = cloudRequest("POST", CLOUD_API + "/posture", body, 6000, 65000);
                if (r.code == 200) {
                    JSONObject j = new JSONObject(r.body);
                    out.ok = j.optBoolean("ok", true);
                    out.posture = j.optString("posture", posture);
                    out.node = j.optString("node", node);
                    out.detail = j.optString("message", j.optString("detail", out.posture + " applied on " + out.node));
                    return out;
                }
                lastErr = "Aitherium relay answered " + r.code + ": " + r.body;
            } catch (Exception e) {
                lastErr = e.getClass().getSimpleName() + ": " + e.getMessage();
            }
        }
        out.detail = lastErr;
        return out;
    }

    /** Standard CA-verified HTTPS call to api.aitherium.com using the signed-in WebView session cookie. */
    private Resp cloudRequest(String method, String urlStr, String jsonBody, int connMs, int readMs) throws Exception {
        URL u = new URL(urlStr);
        HttpsURLConnection c = (HttpsURLConnection) u.openConnection();
        c.setRequestMethod(method);
        c.setConnectTimeout(connMs);
        c.setReadTimeout(readMs);
        c.setInstanceFollowRedirects(false);
        String cookie = cloudCookie();
        if (!cookie.isEmpty()) c.setRequestProperty("Cookie", cookie);
        c.setRequestProperty("Origin", "https://aitherium.com");
        c.setRequestProperty("Accept", "application/json");
        try {
            if (jsonBody != null) {
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", "application/json");
                byte[] bytes = jsonBody.getBytes(StandardCharsets.UTF_8);
                c.setFixedLengthStreamingMode(bytes.length);
                try (OutputStream o = c.getOutputStream()) { o.write(bytes); }
            }
            int code = c.getResponseCode();
            InputStream in = code < 400 ? c.getInputStream() : c.getErrorStream();
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            if (in != null) {
                try (InputStream i = in) {
                    byte[] b = new byte[16384];
                    int n;
                    while ((n = i.read(b)) > 0) {
                        if (buf.size() + n > (16 << 20)) throw new IOException("relay reply too large");
                        buf.write(b, 0, n);
                    }
                }
            }
            return new Resp(code, buf.toString("UTF-8"));
        } finally {
            c.disconnect();
        }
    }

    private static final class Resp {
        final int code;
        final String body;
        Resp(int code, String body) { this.code = code; this.body = body; }
    }

    private Resp request(String method, String urlStr, String jsonBody, int connMs, int readMs) throws Exception {
        URL u = new URL(urlStr);
        if (!"https".equalsIgnoreCase(u.getProtocol()) || !MeshRemote.isPrivateMeshHost(u.getHost())) {
            throw new IOException("refused non-mesh host: " + u.getHost());
        }
        HttpsURLConnection c = (HttpsURLConnection) u.openConnection();
        c.setSSLSocketFactory(privateMeshSsl());
        c.setHostnameVerifier((host, session) -> MeshRemote.isPrivateMeshHost(host));
        c.setRequestMethod(method);
        c.setConnectTimeout(connMs);
        c.setReadTimeout(readMs);
        c.setInstanceFollowRedirects(false);
        c.setRequestProperty("Authorization", "Bearer " + token());
        c.setRequestProperty("Accept", "application/json");
        try {
            if (jsonBody != null) {
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", "application/json");
                byte[] bytes = jsonBody.getBytes(StandardCharsets.UTF_8);
                c.setFixedLengthStreamingMode(bytes.length);
                try (OutputStream o = c.getOutputStream()) { o.write(bytes); }
            }
            int code = c.getResponseCode();
            InputStream in = code < 400 ? c.getInputStream() : c.getErrorStream();
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            if (in != null) {
                try (InputStream i = in) {
                    byte[] b = new byte[16384];
                    int n;
                    while ((n = i.read(b)) > 0) {
                        if (buf.size() + n > (16 << 20)) throw new IOException("mesh reply too large");
                        buf.write(b, 0, n);
                    }
                }
            }
            return new Resp(code, buf.toString("UTF-8"));
        } finally {
            c.disconnect();
        }
    }

    /** Self-signed TLS factory used ONLY for validated private mesh/LAN hosts (never public internet). */
    private static SSLSocketFactory privateMeshSsl() throws Exception {
        if (meshSsl != null) return meshSsl;
        synchronized (MeshClient.class) {
            if (meshSsl != null) return meshSsl;
            TrustManager[] tm = new TrustManager[] {
                new X509TrustManager() {
                    @Override public void checkClientTrusted(X509Certificate[] c, String a) {}
                    @Override public void checkServerTrusted(X509Certificate[] c, String a) {}
                    @Override public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
                }
            };
            SSLContext sc = SSLContext.getInstance("TLS");
            sc.init(null, tm, new SecureRandom());
            meshSsl = sc.getSocketFactory();
            return meshSsl;
        }
    }
}
