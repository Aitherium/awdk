package com.aitherium.aither;

import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.BatteryManager;
import android.os.PowerManager;
import android.webkit.CookieManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * "Share with my family": this phone answers small chat requests from the household's
 * Family AI Pool with its own model, without ever being reachable from outside. It only
 * dials out: it long-polls the pool for a job routed to it, runs it on the local
 * llama-server, and posts the answer back (computepool's pull contract).
 *
 * Off unless the owner turns it on here. On an adult's own phone that switch IS the opt-in:
 * the app tells the pool (POST /compute/share, the device token plus the signed-in owner
 * of the device) and opts out again when it is turned off. A child's phone is refused by
 * the pool (only a guardian can opt it in) and this app never shares from one anyway. It
 * pauses (withdrawing its offer) while the phone is not charging, is warm, or is saving power.
 */
final class FamilyShare implements Runnable {
    static final String POOL = "https://api.aitherium.com/api/tutor/me/device/compute"; // the same edge path as the household heartbeat
    static final int WAIT_S = 25;
    static final int MAX_TOKENS = 1024;
    static volatile String state = "off";
    static volatile int answered;

    private final Context ctx;
    private final LocalProxy.Backend llm;
    private final Config cfg;
    private volatile boolean stop;
    private Thread thread;
    private String offered = "";
    private boolean optedIn;
    private String lastPath = "";

    FamilyShare(Context c, LocalProxy.Backend backend) {
        ctx = c.getApplicationContext();
        llm = backend;
        cfg = new Config(ctx);
    }

    void start() {
        if (thread != null) return;
        stop = false;
        thread = new Thread(this, "aither-family-share");
        thread.setDaemon(true);
        thread.start();
    }

    void stop() {
        stop = true;
        if (thread != null) thread.interrupt();
        thread = null;
    }

    @Override
    public void run() {
        long backoff = 0;
        while (!stop) {
            try {
                if (backoff > 0) Thread.sleep(backoff);
                backoff = once();
            } catch (InterruptedException e) {
                break;
            } catch (Exception e) {
                state = "error: " + e.getClass().getSimpleName() + "; retrying";
                backoff = 60_000;
            }
        }
        withdraw();
        if (optedIn && !cfg.shareFamily()) optOut();
        state = "off";
    }

    /** The owner turned sharing off on this phone: tell the pool, not just stop polling. */
    private void optOut() {
        optedIn = false;
        try {
            post("/share", new JSONObject().put("token", token()).put("compute_share", false));
        } catch (Exception e) { /* withdrawn already; the pool drops a silent device */ }
    }

    /** One step. Returns how long to wait before the next one (0 = right away). */
    private long once() throws Exception {
        String token = token();
        String why = paused(token);
        if (!why.isEmpty()) {
            withdraw();
            state = "paused: " + why;
            return 60_000;
        }
        if (!optedIn) {
            Resp r = post("/share", new JSONObject().put("token", token).put("compute_share", true));
            if (r.code != 200) {
                if (r.code == 403 && r.body.contains("child_needs_guardian")) {
                    state = "waiting: only a guardian can share a child's phone";
                    return 10 * 60_000;
                }
                return refused(r);
            }
            optedIn = true;
        }
        JSONObject st = deviceState();
        if (!st.toString().equals(offered)) {
            Resp r = post("/offer", new JSONObject().put("token", token).put("endpoint", "pull://")
                    .put("models", new JSONArray().put(llm.modelId())).put("max_concurrent", 1)
                    .put("state", st));
            if (r.code != 200) return refused(r);
            offered = st.toString();
        }
        state = "sharing · " + answered + " answered";
        Resp r = post("/next", new JSONObject().put("token", token).put("wait_s", WAIT_S));
        if (r.code == 204) return 0;
        if (r.code != 200) {
            offered = "";
            return refused(r);
        }
        JSONObject job = new JSONObject(r.body);
        String id = job.optString("id", "");
        if (id.isEmpty() || !id.matches("[A-Za-z0-9_-]{1,128}")) return 0;
        JSONObject reply = new JSONObject();
        int status = answer(job.optJSONObject("body"), reply);
        Resp done = post("/result/" + id, new JSONObject().put("token", token)
                .put("status", status).put("body", reply));
        if (done.code == 200 && status == 200) answered++;
        return 0;
    }

    private long refused(Resp r) {
        String err = "";
        try { err = new JSONObject(r.body).optString("detail", new JSONObject(r.body).optString("error", "")); }
        catch (Exception e) { /* not JSON */ }
        if (r.code == 403 && err.contains("compute_share_off")) {
            state = "waiting: a guardian has not turned on family sharing";
            return 10 * 60_000;
        }
        if (r.code == 401) {
            state = "signed out: open AitherOS to sign in again";
            return 15 * 60_000;
        }
        if (r.code == 410) {
            state = "removed from the household";
            return 15 * 60_000;
        }
        state = lastPath + ": the pool answered " + r.code + (err.isEmpty() ? "" : " (" + err + ")") + "; retrying";
        return 60_000;
    }

    /** Run one pool job on the local model. Fills {@code out}; returns the HTTP status. */
    private int answer(JSONObject req, JSONObject out) throws Exception {
        if (req == null || req.optJSONArray("messages") == null) {
            out.put("error", "no messages");
            return 400;
        }
        String not = llm.ensure();
        if (!not.isEmpty()) {
            out.put("error", not);
            return 503;
        }
        // only the fields a chat needs, on this phone's model, never streamed, bounded
        JSONObject body = new JSONObject().put("model", llm.modelId()).put("stream", false)
                .put("messages", req.getJSONArray("messages"))
                .put("max_tokens", Math.max(1, Math.min(req.optInt("max_tokens", 256), MAX_TOKENS)));
        if (req.has("temperature")) body.put("temperature", req.optDouble("temperature", 0.7));
        if (req.has("stop")) body.put("stop", req.opt("stop"));
        llm.touched();
        HttpURLConnection c = (HttpURLConnection) new URL("http://127.0.0.1:" + llm.port()
                + "/v1/chat/completions").openConnection();
        c.setRequestMethod("POST");
        c.setConnectTimeout(5000);
        c.setReadTimeout(115_000);
        c.setDoOutput(true);
        c.setRequestProperty("Content-Type", "application/json");
        c.setRequestProperty("Authorization", "Bearer " + llm.key());
        try (OutputStream o = c.getOutputStream()) {
            o.write(body.toString().getBytes(StandardCharsets.UTF_8));
        }
        int code = c.getResponseCode();
        InputStream in = code < 400 ? c.getInputStream() : c.getErrorStream();
        String text = in == null ? "" : NodeLink.read(in, 1 << 20);
        llm.touched();
        try {
            JSONObject j = new JSONObject(text);
            for (java.util.Iterator<String> it = j.keys(); it.hasNext(); ) {
                String k = it.next();
                out.put(k, j.get(k));
            }
        } catch (Exception e) {
            out.put("error", "the local model answered " + code);
        }
        return code;
    }

    // ------------------------------------------------------------ guards

    private String token() {
        try {
            return new JSONObject(cfg.familyDevice()).optString("token", "");
        } catch (Exception e) {
            return "";
        }
    }

    private String paused(String token) {
        if (!cfg.shareFamily()) return "turned off on this phone";
        if (token.isEmpty()) return "this phone is not in a household";
        String blocked = cfg.localAiBlocked();
        if (!blocked.isEmpty()) return blocked;
        PowerManager pm = ctx.getSystemService(PowerManager.class);
        if (pm.isPowerSaveMode()) return "battery saver is on";
        if (pm.getCurrentThermalStatus() >= PowerManager.THERMAL_STATUS_MODERATE) return "the phone is warm";
        if (!charging()) return "not charging";
        return "";
    }

    private boolean charging() {
        Intent b = ctx.registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        return b != null && b.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0;
    }

    private JSONObject deviceState() throws Exception {
        PowerManager pm = ctx.getSystemService(PowerManager.class);
        return new JSONObject().put("ac", charging()).put("gaming", false)
                .put("thermal", pm.getCurrentThermalStatus());
    }

    private void withdraw() {
        if (offered.isEmpty()) return;
        offered = "";
        try {
            post("/withdraw", new JSONObject().put("token", token()));
        } catch (Exception e) { /* the pool drops a silent device on its own */ }
    }

    // ------------------------------------------------------------ http

    private static final class Resp {
        final int code;
        final String body;

        Resp(int code, String body) {
            this.code = code;
            this.body = body;
        }
    }

    private Resp post(String path, JSONObject json) throws java.io.IOException {
        lastPath = path;
        HttpURLConnection c = (HttpURLConnection) new URL(POOL + path).openConnection();
        c.setRequestMethod("POST");
        c.setConnectTimeout(20000);
        c.setReadTimeout((WAIT_S + 15) * 1000);
        c.setDoOutput(true);
        c.setRequestProperty("Content-Type", "application/json");
        c.setRequestProperty("Origin", "https://aitherium.com");
        c.setRequestProperty("User-Agent", NodeLink.UA);
        String cookie = CookieManager.getInstance().getCookie("https://api.aitherium.com");
        if (cookie != null) c.setRequestProperty("Cookie", cookie);
        try (OutputStream o = c.getOutputStream()) {
            o.write(json.toString().getBytes(StandardCharsets.UTF_8));
        }
        int code = c.getResponseCode();
        InputStream in = code < 400 ? c.getInputStream() : c.getErrorStream();
        return new Resp(code, in == null ? "" : NodeLink.read(in, 1 << 20));
    }
}
