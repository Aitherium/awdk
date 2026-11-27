package com.aitherium.aither;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
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
 * Direct AitherMesh client for the Wear OS watch app (shares MeshRemote.java via WEAR_SHARED).
 * Lets the Pixel Watch target DGX Spark, PC 5090, or OptiPlex directly over Tailscale/LAN
 * when paired, and switch model postures from the wrist. Falls back cleanly to cloud Genesis
 * (/api/agent-chat) when away from home or unpaired.
 */
final class WearMesh {
    private static final String PREFS = "wear_mesh";
    private static volatile SSLSocketFactory meshSsl;

    static final String[][] TARGETS = {
        {"cloud", "Cloud (anywhere)"},
        {"auto", "Mesh: Auto"},
        {"dgx-spark", "DGX Spark"},
        {"pc", "PC 5090"},
        {"optiplex", "OptiPlex"},
    };

    static final class Node {
        final String id;
        final String name;
        final boolean online;
        final String posture;
        final String model;

        Node(String id, String name, boolean online, String posture, String model) {
            this.id = id;
            this.name = name;
            this.online = online;
            this.posture = posture;
            this.model = model;
        }

        String line() {
            if (!online) return name + " · offline";
            return name + " · " + (posture.isEmpty() ? "online" : posture)
                    + (model.isEmpty() ? "" : " · " + model);
        }
    }

    static final class Reply {
        boolean ok;
        String reply = "";
        String node = "";
        String model = "";
        String error = "";
        byte[] audio;
    }

    private final Context ctx;
    private final SharedPreferences p;

    WearMesh(Context c) {
        ctx = c.getApplicationContext();
        p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    String hub() {
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
        return !hub().isEmpty() && MeshRemote.validToken(token());
    }

    private String wearBearer() {
        try {
            return new WearApi(ctx).token();
        } catch (Throwable ignored) {
            return "";
        }
    }

    boolean cloudSignedIn() {
        return !wearBearer().isEmpty();
    }

    boolean paired() {
        return directPaired() || cloudSignedIn();
    }

    /** "cloud" | "auto" | "dgx-spark" | "pc" | "optiplex". */
    String target() {
        String t = p.getString("target", "cloud");
        for (String[] row : TARGETS) if (row[0].equals(t)) return t;
        return "cloud";
    }

    void setTarget(String t) {
        for (String[] row : TARGETS) {
            if (row[0].equals(t)) {
                p.edit().putString("target", t).apply();
                return;
            }
        }
    }

    String targetLabel() {
        String t = target();
        for (String[] row : TARGETS) if (row[0].equals(t)) return row[1];
        return "Cloud (anywhere)";
    }

    boolean save(String rawHub, String rawToken) {
        if (rawToken != null && (rawToken.trim().startsWith("https://") || rawToken.trim().startsWith("aither://"))) {
            MeshRemote.Pair pair = MeshRemote.parsePairUrl(rawToken);
            if (pair == null) return false;
            p.edit().putString("hub", pair.hub).putString("token", pair.token).apply();
            if ("cloud".equals(target())) setTarget("auto");
            return true;
        }
        String h = MeshRemote.normalizeHubUrl(rawHub);
        if (h.isEmpty() || !MeshRemote.validToken(rawToken)) return false;
        p.edit().putString("hub", h).putString("token", rawToken.trim()).apply();
        if ("cloud".equals(target())) setTarget("auto");
        return true;
    }

    private List<String> hubs() {
        List<String> list = new ArrayList<>();
        String h1 = hub(), h2 = fallbackHub();
        if (!h1.isEmpty()) list.add(h1);
        if (!h2.isEmpty() && !h2.equals(h1)) list.add(h2);
        return list;
    }

    private static List<Node> parseNodes(String body) throws Exception {
        List<Node> out = new ArrayList<>();
        JSONObject j = new JSONObject(body);
        JSONArray arr = j.optJSONArray("nodes");
        if (arr == null) arr = j.optJSONArray("peers");
        for (int i = 0; arr != null && i < arr.length(); i++) {
            JSONObject o = arr.optJSONObject(i);
            if (o == null) continue;
            out.add(new Node(o.optString("id", ""), o.optString("name", o.optString("label", o.optString("id", ""))),
                    o.optBoolean("online", false), o.optString("posture", ""),
                    o.optString("llm_model", o.optString("model", ""))));
        }
        return out;
    }

    List<Node> nodes(String[] errOut) {
        List<Node> out = new ArrayList<>();
        if (!paired()) {
            if (errOut != null && errOut.length > 0) errOut[0] = "Sign in on watch or pair a mesh token first.";
            return out;
        }
        String last = "No mesh hub answered.";
        if (directPaired()) {
            for (String h : hubs()) {
                try {
                    Resp r = call("GET", h + "/api/nodes", null, 4000, 8000);
                    if (r.code != 200) { last = "Hub answered " + r.code; continue; }
                    out = parseNodes(r.body);
                    if (errOut != null && errOut.length > 0) errOut[0] = "";
                    return out;
                } catch (Exception e) {
                    last = e.getClass().getSimpleName();
                }
            }
        }
        if (cloudSignedIn()) {
            try {
                Resp r = cloudCall("GET", WearApi.API + "/api/ecosystem/voice-remote/nodes", null, 6000, 15000);
                if (r.code == 200) {
                    out = parseNodes(r.body);
                    if (errOut != null && errOut.length > 0) errOut[0] = "";
                    return out;
                }
                last = "Relay answered " + r.code;
            } catch (Exception e) {
                last = e.getClass().getSimpleName();
            }
        }
        if (errOut != null && errOut.length > 0) errOut[0] = last;
        return out;
    }

    Reply chat(String text, String node, boolean speak) {
        Reply out = new Reply();
        if (!paired()) { out.error = "Sign in on watch or pair mesh."; return out; }
        String body = MeshRemote.chatBody(text, node, speak);
        String last = "No mesh hub answered.";
        if (directPaired()) {
            for (String h : hubs()) {
                try {
                    Resp r = call("POST", h + "/api/chat", body, 4000, 60000);
                    if (r.code != 200) { last = "Mesh answered " + r.code; continue; }
                    JSONObject j = new JSONObject(r.body);
                    out.ok = true;
                    out.reply = j.optString("reply", "").trim();
                    out.node = j.optString("node", j.optString("target", node));
                    out.model = j.optString("model", "");
                    String b64 = j.optString("audio_b64", j.optString("audio_wav_b64", ""));
                    if (!b64.isEmpty()) {
                        try { out.audio = Base64.decode(b64, Base64.DEFAULT); } catch (Exception ignored) {}
                    }
                    return out;
                } catch (Exception e) {
                    last = e.getClass().getSimpleName();
                }
            }
        }
        if (cloudSignedIn()) {
            try {
                Resp r = cloudCall("POST", WearApi.API + "/api/ecosystem/voice-remote/ask", body, 6000, 60000);
                if (r.code == 200) {
                    JSONObject j = new JSONObject(r.body);
                    out.ok = true;
                    out.reply = j.optString("reply", "").trim();
                    out.node = j.optString("target", j.optString("node", node));
                    out.model = j.optString("model", "");
                    String b64 = j.optString("audio_wav_b64", j.optString("audio_b64", ""));
                    if (!b64.isEmpty()) {
                        try { out.audio = Base64.decode(b64, Base64.DEFAULT); } catch (Exception ignored) {}
                    }
                    return out;
                }
                last = "Relay answered " + r.code;
            } catch (Exception e) {
                last = e.getClass().getSimpleName();
            }
        }
        out.error = last;
        return out;
    }

    String setPosture(String posture, String node) {
        if (!paired()) return "Sign in on watch or pair mesh first.";
        String body = MeshRemote.postureBody(posture, node);
        if (body.isEmpty()) return "Unknown posture.";
        String last = "No mesh hub answered.";
        if (directPaired()) {
            for (String h : hubs()) {
                try {
                    Resp r = call("POST", h + "/api/posture", body, 4000, 45000);
                    if (r.code == 200) {
                        JSONObject j = new JSONObject(r.body);
                        return j.optString("message", j.optString("detail", posture + " applied"));
                    }
                    last = "Mesh answered " + r.code;
                } catch (Exception e) {
                    last = e.getClass().getSimpleName();
                }
            }
        }
        if (cloudSignedIn()) {
            try {
                Resp r = cloudCall("POST", WearApi.API + "/api/ecosystem/voice-remote/posture", body, 6000, 65000);
                if (r.code == 200) {
                    JSONObject j = new JSONObject(r.body);
                    return j.optString("message", j.optString("detail", posture + " applied"));
                }
                last = "Relay answered " + r.code;
            } catch (Exception e) {
                last = e.getClass().getSimpleName();
            }
        }
        return last;
    }

    private Resp cloudCall(String method, String urlStr, String jsonBody, int connMs, int readMs) throws Exception {
        URL u = new URL(urlStr);
        HttpsURLConnection c = (HttpsURLConnection) u.openConnection();
        c.setRequestMethod(method);
        c.setConnectTimeout(connMs);
        c.setReadTimeout(readMs);
        c.setInstanceFollowRedirects(false);
        c.setRequestProperty("Authorization", "Bearer " + wearBearer());
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
                        if (buf.size() + n > (12 << 20)) throw new IOException("too large");
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

    private Resp call(String method, String urlStr, String jsonBody, int connMs, int readMs) throws Exception {
        URL u = new URL(urlStr);
        if (!"https".equalsIgnoreCase(u.getProtocol()) || !MeshRemote.isPrivateMeshHost(u.getHost())) {
            throw new IOException("refused non-mesh host");
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
                        if (buf.size() + n > (12 << 20)) throw new IOException("too large");
                        buf.write(b, 0, n);
                    }
                }
            }
            return new Resp(code, buf.toString("UTF-8"));
        } finally {
            c.disconnect();
        }
    }

    private static SSLSocketFactory privateMeshSsl() throws Exception {
        if (meshSsl != null) return meshSsl;
        synchronized (WearMesh.class) {
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
