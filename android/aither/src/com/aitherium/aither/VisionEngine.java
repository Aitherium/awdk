package com.aitherium.aither;

import android.app.ActivityManager;
import android.content.Context;
import android.os.PowerManager;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;

/**
 * The picture model on this phone (Vision says which, and when): llama-server with
 * SmolVLM-500M and its vision projector (--mmproj), as its own process on 127.0.0.1:18487
 * with its own key. Nothing but this class talks to it; LocalProxy never forwards there.
 * Started on the first picture, stopped after IDLE_MS without one, on a low-memory signal
 * (DescribeActivity.onTrimMemory) or when the phone gets hot.
 */
final class VisionEngine {
    static final int PORT = 18487;
    static final long IDLE_MS = 3 * 60 * 1000;

    interface Progress { void bytes(long done, long total); }

    private static final Object LOCK = new Object();
    private static Process proc;
    private static String key;
    private static volatile long lastUse;
    private static Thread idleWatch;
    /** What the engine last did, for DescribeActivity's status line. */
    static volatile String state = "not loaded";

    private VisionEngine() {}

    static File dir(Context c) { return new File(c.getFilesDir(), "models"); }
    static File model(Context c) { return new File(dir(c), Vision.MODEL_FILE); }
    static File proj(Context c) { return new File(dir(c), Vision.PROJ_FILE); }
    static File binary(Context c) {
        return new File(c.getApplicationInfo().nativeLibraryDir, "libllamaserver.so");
    }

    static boolean installed(Context c) {
        return model(c).length() == Vision.MODEL_BYTES && proj(c).length() == Vision.PROJ_BYTES;
    }

    static boolean hot(Context c) {
        PowerManager pm = c.getSystemService(PowerManager.class);
        return pm != null && pm.getCurrentThermalStatus() >= PowerManager.THERMAL_STATUS_SEVERE;
    }

    static ActivityManager.MemoryInfo memory(Context c) {
        ActivityManager.MemoryInfo mi = new ActivityManager.MemoryInfo();
        c.getSystemService(ActivityManager.class).getMemoryInfo(mi);
        return mi;
    }

    /** Vision.pick with this phone's facts. */
    static Vision.Route route(Context c, Config cfg) {
        ActivityManager.MemoryInfo mi = memory(c);
        dir(c).mkdirs();
        return Vision.pick(cfg.localAiBlocked(), binary(c).exists(), installed(c),
                cfg.visionDeclined(), mi.lowMemory ? 0 : mi.availMem, mi.totalMem,
                dir(c).getUsableSpace(), hot(c), Session.hasSession(
                        android.webkit.CookieManager.getInstance().getCookie(AppTabs.ORIGIN)));
    }

    static String whyNotLocal(Context c, Config cfg) {
        ActivityManager.MemoryInfo mi = memory(c);
        dir(c).mkdirs();
        return Vision.whyNotLocal(cfg.localAiBlocked(), binary(c).exists(), installed(c),
                mi.lowMemory ? 0 : mi.availMem, mi.totalMem, dir(c).getUsableSpace(), hot(c));
    }

    /**
     * Fetch both files, each kept only if its SHA-256 is the pinned one. Blocking: call off
     * the main thread, and only after the owner tapped Download. "" on success.
     */
    static String download(Context c, Progress p) {
        dir(c).mkdirs();
        if (!Vision.roomFor(dir(c).getUsableSpace() + model(c).length() + proj(c).length())) {
            return "not enough storage for the picture model (" + Vision.sizeMb() + ")";
        }
        long[] done = {0};
        String err = fetch(Vision.BASE + Vision.PROJ_FILE, Vision.PROJ_SHA256, Vision.PROJ_BYTES,
                proj(c), done, p);
        if (err.isEmpty()) {
            err = fetch(Vision.BASE + Vision.MODEL_FILE, Vision.MODEL_SHA256, Vision.MODEL_BYTES,
                    model(c), done, p);
        }
        return err;
    }

    /** Delete both files (Settings, when the owner turns the picture model off). */
    static void remove(Context c) {
        unload("removed");
        model(c).delete();
        proj(c).delete();
        new File(model(c).getPath() + ".part").delete();
        new File(proj(c).getPath() + ".part").delete();
    }

    private static String fetch(String url, String sha, long size, File dest, long[] done, Progress p) {
        if (dest.length() == size) {
            done[0] += size;
            return "";
        }
        File part = new File(dest.getPath() + ".part");
        try {
            HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
            c.setRequestProperty("User-Agent", "Mozilla/5.0 (Linux; Android) Aither/" + Config.VERSION);
            c.setConnectTimeout(20000);
            c.setReadTimeout(60000);
            c.setInstanceFollowRedirects(true); // the hub answers with a redirect to its CDN
            if (c.getResponseCode() != 200) return "picture model download refused (" + c.getResponseCode() + ")";
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            long n0 = done[0];
            try (InputStream in = c.getInputStream(); OutputStream out = new FileOutputStream(part)) {
                byte[] buf = new byte[1 << 16];
                int n;
                long got = 0;
                while ((n = in.read(buf)) > 0) {
                    out.write(buf, 0, n);
                    md.update(buf, 0, n);
                    got += n;
                    if (got > size) break; // never more than the pinned size
                    if (p != null) p.bytes(n0 + got, Vision.DOWNLOAD_BYTES);
                }
            }
            StringBuilder hex = new StringBuilder();
            for (byte b : md.digest()) hex.append(String.format("%02x", b & 0xff));
            if (part.length() != size || !sha.equals(hex.toString())) {
                part.delete();
                return "the picture model download was corrupt; try again";
            }
            if (!part.renameTo(dest)) return "could not store the picture model";
            done[0] = n0 + size;
            return "";
        } catch (Exception e) {
            part.delete();
            return "picture model download failed: " + e.getClass().getSimpleName();
        }
    }

    /** Make sure the engine is up; "" when ready, else why not. Blocking. */
    static String ensure(Context c, Config cfg) {
        synchronized (LOCK) {
            if (proc != null && proc.isAlive()) return "";
            proc = null;
            String why = whyNotLocal(c, cfg);
            if (!why.isEmpty()) return state = why;
            byte[] k = new byte[24];
            new SecureRandom().nextBytes(k);
            key = android.util.Base64.encodeToString(k, android.util.Base64.NO_WRAP | android.util.Base64.URL_SAFE);
            long t0 = System.currentTimeMillis();
            try {
                ProcessBuilder pb = new ProcessBuilder(binary(c).getAbsolutePath(),
                        "-m", model(c).getAbsolutePath(), "--mmproj", proj(c).getAbsolutePath(),
                        "--host", "127.0.0.1", "--port", String.valueOf(PORT),
                        "--api-key", key, "--alias", Vision.MODEL_ID, "-c", "4096", "-np", "1",
                        "-t", "4", "--cache-ram", "0", "--no-webui", "--jinja");
                pb.redirectErrorStream(true);
                pb.redirectOutput(new File(c.getFilesDir(), "vision.log"));
                proc = pb.start();
            } catch (Exception e) {
                proc = null;
                return state = "the picture engine did not start";
            }
            for (int i = 0; i < 900; i++) { // up to 90 s: the projector loads too
                if (!proc.isAlive()) { proc = null; return state = "the picture engine exited (see vision.log)"; }
                if (healthy()) {
                    lastUse = System.currentTimeMillis();
                    state = "loaded in " + (lastUse - t0) + " ms";
                    watchIdle();
                    return "";
                }
                try { Thread.sleep(100); } catch (InterruptedException e) { break; }
            }
            unload("the picture engine did not come up");
            return state;
        }
    }

    /** Ask the model on this phone about one picture (JPEG bytes). Blocking. */
    static String describe(Context c, Config cfg, byte[] jpeg, String question) throws Exception {
        String why = ensure(c, cfg);
        if (!why.isEmpty()) throw new IllegalStateException(why);
        lastUse = System.currentTimeMillis();
        String body = Vision.chatBody(question,
                android.util.Base64.encodeToString(jpeg, android.util.Base64.NO_WRAP), 300);
        HttpURLConnection h = (HttpURLConnection) new URL("http://127.0.0.1:" + PORT
                + "/v1/chat/completions").openConnection();
        h.setConnectTimeout(2000);
        h.setReadTimeout(180000);
        h.setDoOutput(true);
        h.setRequestProperty("Content-Type", "application/json");
        h.setRequestProperty("Authorization", "Bearer " + key);
        try (OutputStream o = h.getOutputStream()) {
            o.write(body.getBytes(StandardCharsets.UTF_8));
        }
        int code = h.getResponseCode();
        InputStream in = code < 400 ? h.getInputStream() : h.getErrorStream();
        String text = in == null ? "" : new String(in.readAllBytes(), StandardCharsets.UTF_8);
        lastUse = System.currentTimeMillis();
        if (code != 200) throw new IllegalStateException("the picture engine answered " + code);
        return new JSONObject(text).getJSONArray("choices").getJSONObject(0)
                .getJSONObject("message").optString("content", "").trim();
    }

    static void unload(String why) {
        synchronized (LOCK) {
            if (proc != null) {
                proc.destroy();
                proc = null;
            }
            state = why;
        }
    }

    private static boolean healthy() {
        try {
            HttpURLConnection h = (HttpURLConnection) new URL("http://127.0.0.1:" + PORT + "/health").openConnection();
            h.setConnectTimeout(500);
            h.setReadTimeout(1000);
            return h.getResponseCode() == 200;
        } catch (Exception e) {
            return false;
        }
    }

    private static void watchIdle() {
        if (idleWatch != null && idleWatch.isAlive()) return;
        idleWatch = new Thread(() -> {
            while (true) {
                try { Thread.sleep(30000); } catch (InterruptedException e) { return; }
                synchronized (LOCK) {
                    if (proc == null) return;
                    if (System.currentTimeMillis() - lastUse > IDLE_MS) {
                        unload("unloaded: idle");
                        return;
                    }
                }
            }
        }, "aither-vision-idle");
        idleWatch.setDaemon(true);
        idleWatch.start();
    }
}
