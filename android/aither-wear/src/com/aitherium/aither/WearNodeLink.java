package com.aitherium.aither;

import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Typeface;
import android.view.Gravity;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.KeyFactory;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.PrivateKey;
import java.security.Signature;
import java.security.spec.PKCS8EncodedKeySpec;
import java.util.function.IntSupplier;

/**
 * This watch as a node on the owner's mesh (device discovery phase 1, 2026-10-07).
 *
 * The watch is signed in with the device grant (WearApi), which makes it the owner's
 * ACCOUNT on the wrist but never a mesh node: nothing ever enrolled it, so it was missing
 * from the device list. Joining adds it, with the owner's say-so:
 *
 *   1. the watch makes an Ed25519 key (app-private storage) and asks Identity to join
 *      (POST /v1/nodes/join/requests, its own bearer, class "watch");
 *   2. it checks the six-digit number Identity sent against its own key (WearJoin.sas)
 *      and shows it; the owner's phone shows the same number on its lock screen
 *      (a push approval) and in This device > Devices waiting to join;
 *   3. the owner approves there; the watch, polling with its claim secret and a signature,
 *      collects a single-use pairing code bound to its key;
 *   4. it confirms the code at /v1/nodes/pairing/confirm as a "watch" and keeps the device
 *      token and command key it is handed, exactly as the phone's NodeLink does.
 *
 * New file on purpose (the phone and watch apps are being changed in another session):
 * WearActivity only gains one button that opens {@link #show}.
 */
final class WearNodeLink {
    static final String IDP = "https://idp.aitherium.com";
    static final String UA = "Mozilla/5.0 (Linux; Android; Wear OS) AitherWear/0.1";
    private static final int POLL_MS = 3000;

    private final SharedPreferences p;

    WearNodeLink(Context c) {
        p = c.getApplicationContext().getSharedPreferences("wear-node", Context.MODE_PRIVATE);
    }

    boolean linked() { return !p.getString("node_id", "").isEmpty() && !p.getString("bearer", "").isEmpty(); }

    /** This watch's node id: made once, kept (a re-join keeps the same device row). */
    String nodeId() {
        String id = p.getString("wanted_id", "");
        if (id.isEmpty()) {
            byte[] r = new byte[6];
            new java.security.SecureRandom().nextBytes(r);
            id = "watch-" + WearJoin.hex(r);
            p.edit().putString("wanted_id", id).apply();
        }
        return id;
    }

    /** The watch's Ed25519 key pair, made once. Null when this Android has no Ed25519
     *  (it arrived in the platform's provider with API 33; Pixel Watch 4 is API 36). */
    private KeyPair keys() {
        try {
            String priv = p.getString("key_priv", ""), pub = p.getString("key_pub", "");
            if (!priv.isEmpty() && !pub.isEmpty()) {
                PrivateKey k = KeyFactory.getInstance("Ed25519").generatePrivate(
                        new PKCS8EncodedKeySpec(android.util.Base64.decode(priv, android.util.Base64.NO_WRAP)));
                return new KeyPair(null, k);
            }
            KeyPair kp = KeyPairGenerator.getInstance("Ed25519").generateKeyPair();
            p.edit().putString("key_priv", android.util.Base64.encodeToString(kp.getPrivate().getEncoded(), android.util.Base64.NO_WRAP))
                    .putString("key_pub", WearJoin.rawPublicHex(kp.getPublic().getEncoded())).apply();
            return kp;
        } catch (Exception e) {
            return null;
        }
    }

    private String pubHex() { return p.getString("key_pub", ""); }

    private static String sign(PrivateKey k, byte[] msg) throws Exception {
        Signature s = Signature.getInstance("Ed25519");
        s.initSign(k);
        s.update(msg);
        return WearJoin.hex(s.sign());
    }

    // ------------------------------------------------------------------ http

    static final class Resp {
        final int code;
        final JSONObject json;

        Resp(int code, JSONObject json) {
            this.code = code;
            this.json = json == null ? new JSONObject() : json;
        }
    }

    static Resp call(String path, String bearer, JSONObject body) {
        try {
            HttpURLConnection c = (HttpURLConnection) new URL(IDP + path).openConnection();
            c.setRequestMethod("POST");
            c.setConnectTimeout(20000);
            c.setReadTimeout(60000);
            c.setDoOutput(true);
            c.setRequestProperty("User-Agent", UA);
            c.setRequestProperty("Accept", "application/json");
            c.setRequestProperty("Content-Type", "application/json");
            if (bearer != null && !bearer.isEmpty()) c.setRequestProperty("Authorization", "Bearer " + bearer);
            try (OutputStream o = c.getOutputStream()) {
                o.write(body.toString().getBytes(StandardCharsets.UTF_8));
            }
            int code = c.getResponseCode();
            InputStream in = code < 400 ? c.getInputStream() : c.getErrorStream();
            String text = "";
            if (in != null) {
                ByteArrayOutputStream b = new ByteArrayOutputStream();
                byte[] buf = new byte[8192];
                int n;
                try {
                    while (b.size() < (1 << 20) && (n = in.read(buf)) > 0) b.write(buf, 0, n);
                } finally {
                    in.close();
                }
                text = b.toString("UTF-8");
            }
            JSONObject j = null;
            try { j = new JSONObject(text); } catch (Exception e) { /* not JSON */ }
            return new Resp(code, j);
        } catch (Exception e) {
            return new Resp(0, null);
        }
    }

    // ------------------------------------------------------------------ the screen

    /** Run the join on {@code col} (already cleared for screen {@code at}). {@code now} is the
     *  activity's current screen: anything for an older screen is dropped. */
    void show(Activity act, LinearLayout col, WearApi api, int at, IntSupplier now, Runnable done) {
        TextView status = text(act, col, "Asking to join your devices…", 13, Ui.DIM);
        TextView number = text(act, col, "", 30, Ui.INK);
        number.setTypeface(Typeface.create("monospace", Typeface.BOLD));
        TextView hint = text(act, col, "", 12, Ui.DIM);
        java.util.function.Consumer<Runnable> onUi = r -> act.runOnUiThread(() -> { if (now.getAsInt() == at) r.run(); });
        new Thread(() -> {
            String end = join(api, at, now, s -> onUi.accept(() -> {
                number.setText(WearJoin.spaced(s));
                status.setText("Approve on your phone");
                hint.setText("Only if your phone shows this same number.");
            }));
            onUi.accept(() -> {
                number.setText("");
                hint.setText("");
                status.setText(end);
                done.run();
            });
        }, "aither-wear-join").start();
    }

    /** The whole flow, off the main thread. Returns the line the watch ends on. */
    String join(WearApi api, int at, IntSupplier now, java.util.function.Consumer<String> showSas) {
        KeyPair kp = keys();
        if (kp == null) return "This watch cannot make a device key (needs Wear OS 4 or later).";
        String bearer = api.token();
        if (bearer.isEmpty()) return "Sign in first.";
        try {
            Resp r = call("/v1/nodes/join/requests", bearer, new JSONObject()
                    .put("device_class", "watch").put("pubkey", pubHex())
                    .put("label", WearJoin.hostname(android.os.Build.MODEL)));
            if (r.code == 401) { api.signOut(); return "Signed out. Sign in again."; }
            if (r.code == 402) return "Your plan has no room for another device.";
            if (r.code == 429) return "Too many tries. Wait a few minutes.";
            String rid = r.json.optString("rid"), nonce = r.json.optString("nonce");
            String sas = r.json.optString("sas"), secret = r.json.optString("claim_secret");
            if (r.code != 200 || !WearJoin.validRid(rid) || secret.isEmpty()) {
                return "Couldn't ask to join (" + r.code + ").";
            }
            // the number must come from THIS watch's key, or it is not shown at all
            if (!WearJoin.sas(rid, pubHex(), nonce).equals(sas)) return "The numbers did not check out. Nothing was added.";
            showSas.accept(sas);
            String signature = sign(kp.getPrivate(), WearJoin.message(rid, nonce));
            long until = System.currentTimeMillis() + Math.max(60, r.json.optInt("expires_in", 300)) * 1000L;
            while (now.getAsInt() == at && System.currentTimeMillis() < until) {
                Thread.sleep(POLL_MS);
                Resp c = call("/v1/nodes/join/requests/" + rid + "/claim", null,
                        new JSONObject().put("claim_secret", secret).put("signature", signature));
                String phase = WearJoin.phase(c.code, c.json.optString("state"));
                if ("approved".equals(phase)) return confirm(c.json.optString("code"));
                String endLine = WearJoin.endLine(phase);
                if (endLine != null) return endLine;
            }
            return now.getAsInt() == at ? "That request expired. Try again." : "";
        } catch (InterruptedException e) {
            return "";
        } catch (Exception e) {
            return "Joining failed: " + e.getClass().getSimpleName();
        }
    }

    private String confirm(String code) throws Exception {
        if (!WearJoin.validCode(code)) return "No code came back. Try again.";
        JSONObject body = new JSONObject()
                .put("code", code)
                .put("node_id", nodeId())
                .put("hostname", WearJoin.hostname(android.os.Build.MODEL))
                .put("platform", "android")
                .put("node_class", "watch")
                .put("inference_kind", "none")
                .put("seal_pubkey", pubHex())
                .put("cpu_count", Runtime.getRuntime().availableProcessors())
                .put("capabilities", new JSONArray());
        Resp r = call("/v1/nodes/pairing/confirm", null, body);
        String node = r.json.optString("node_id", ""), tok = r.json.optString("bearer_token", "");
        if (r.code != 200 || node.isEmpty() || tok.isEmpty()) return "Identity answered " + r.code + ". Try again.";
        p.edit().putString("node_id", node).putString("bearer", tok)
                .putString("key", r.json.optString("command_key", "")).apply();
        return "This watch is on your devices.";
    }

    private static TextView text(Activity act, LinearLayout col, String s, float sp, int color) {
        TextView t = Ui.text(act, s, sp, color);
        t.setGravity(Gravity.CENTER_HORIZONTAL);
        t.setPadding(0, Ui.dp(act, 4), 0, Ui.dp(act, 4));
        col.addView(t, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));
        return t;
    }
}
