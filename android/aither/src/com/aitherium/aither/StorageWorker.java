package com.aitherium.aither;

import android.content.Context;
import android.util.Log;
import android.webkit.CookieManager;

import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/**
 * The family storage pool's worker on this phone (B6): it dials out, takes the jobs the
 * household gave it and runs them. Nothing reaches the phone; there is no open port.
 *
 *   store  download the (already sealed) bytes into files/pool/, hash them, report the hash
 *   fetch  upload a kept copy so the owner can read it
 *   drop   delete a copy
 *
 * It runs only while StorageRules says this phone lends right now (household switch on, a
 * quota, on Wi-Fi while charging, battery saver off; a child's phone always keeps Wi-Fi and
 * charging), re-checks before every job, stays within its quota, and stops at its time
 * budget. When the household switches lending off, the pool folder is wiped: the copies were
 * the family's ciphertext, and repair places them elsewhere.
 */
final class StorageWorker {
    static final String API = "https://api.aitherium.com/api/tutor/me/device/storage";
    static final int MAX_JOBS = 20;
    static volatile String last = "idle";

    private StorageWorker() {}

    static File pool(Context ctx) {
        return new File(ctx.getFilesDir(), "pool");
    }

    /** Run jobs for up to {@code budgetMs}. Never throws. */
    static void drain(Context ctx, long budgetMs) {
        Config cfg = new Config(ctx);
        if (!cfg.storageShare()) {
            if (wipe(pool(ctx)) > 0) last = "lending is off: this phone's copies were removed";
            return;
        }
        String token = token(cfg);
        if (token.isEmpty()) return;
        long until = System.currentTimeMillis() + budgetMs;
        try {
            for (int n = 0; n < MAX_JOBS && System.currentTimeMillis() < until; n++) {
                StorageRules.Verdict v = StorageShare.verdict(ctx);
                JSONObject poll = new JSONObject().put("token", token)
                        .put("used_bytes", StorageShare.usedBytes(ctx))
                        .put("free_bytes", StorageShare.freeBytes(ctx))
                        .put("paused", v.lent ? "" : (v.paused.isEmpty() ? v.reason : v.paused))
                        .put("device_class", tablet(ctx) ? "tablet" : "phone");
                Resp r = call("POST", API + "/poll", token, poll.toString().getBytes(StandardCharsets.UTF_8));
                if (!v.lent || r.code == 204) {
                    last = v.lent ? "nothing to do" : "resting: " + (v.paused.isEmpty() ? v.reason : v.paused);
                    return;
                }
                if (r.code != 200) {
                    last = "the household answered " + r.code;
                    return;
                }
                run(ctx, token, new JSONObject(new String(r.body, StandardCharsets.UTF_8)), v);
            }
        } catch (Exception e) {
            last = "error: " + e.getClass().getSimpleName();
            Log.w("AitherStorage", last);
        }
    }

    private static void run(Context ctx, String token, JSONObject job, StorageRules.Verdict v) throws Exception {
        String id = job.optString("job_id", ""), oid = job.optString("object_id", "");
        String kind = job.optString("kind", "");
        if (!id.matches("sj_[A-Za-z0-9_-]{8,64}") || !oid.matches("[0-9a-f]{64}")) return;
        File dir = pool(ctx);
        File kept = new File(dir, oid);
        if ("store".equals(kind)) {
            long size = job.optLong("size", 0);
            if (StorageShare.usedBytes(ctx) + size > v.quotaBytes) { // never past the quota
                done(token, id, false, "");
                last = "skipped a copy: it would pass this phone's limit";
                return;
            }
            Resp r = call("GET", API + "/blob/" + id, token, null);
            if (r.code != 200) {
                last = "could not download a copy (" + r.code + ")";
                return;
            }
            String sha = sha256(r.body);
            if (!sha.equals(oid)) { // never keep bytes that are not the object
                done(token, id, false, sha);
                return;
            }
            dir.mkdirs();
            File part = new File(dir, oid + ".part");
            try (OutputStream o = new FileOutputStream(part)) {
                o.write(r.body);
            }
            if (!part.renameTo(kept)) throw new java.io.IOException("could not keep the copy");
            done(token, id, true, sha);
            last = "kept a copy";
        } else if ("fetch".equals(kind)) {
            if (!kept.isFile()) return; // nothing to send: the household notices and repairs
            byte[] data = read(kept);
            Resp r = call("PUT", API + "/upload/" + id, token, data);
            last = r.code == 200 ? "sent a copy back" : "the household refused a copy (" + r.code + ")";
        } else if ("drop".equals(kind)) {
            if (kept.isFile() && !kept.delete()) return;
            done(token, id, true, "");
            last = "removed a copy";
        }
    }

    private static void done(String token, String id, boolean ok, String sha) throws Exception {
        JSONObject b = new JSONObject().put("token", token).put("ok", ok).put("sha256", sha);
        call("POST", API + "/done/" + id, token, b.toString().getBytes(StandardCharsets.UTF_8));
    }

    /** Delete every file in the pool folder; returns how many went. */
    static int wipe(File dir) {
        File[] files = dir.listFiles();
        if (files == null) return 0;
        int n = 0;
        for (File f : files) {
            if (f.isDirectory()) n += wipe(f);
            if (f.delete()) n++;
        }
        return n;
    }

    private static boolean tablet(Context ctx) {
        return ctx.getResources().getConfiguration().smallestScreenWidthDp >= 600;
    }

    private static String token(Config cfg) {
        try {
            return new JSONObject(cfg.familyDevice()).optString("token", "");
        } catch (Exception e) {
            return "";
        }
    }

    static String sha256(byte[] data) throws Exception {
        byte[] d = MessageDigest.getInstance("SHA-256").digest(data);
        StringBuilder sb = new StringBuilder();
        for (byte x : d) sb.append(String.format("%02x", x & 0xff));
        return sb.toString();
    }

    private static byte[] read(File f) throws java.io.IOException {
        try (InputStream in = new FileInputStream(f)) {
            return readAll(in, (int) Math.min(f.length() + 1, 64L << 20));
        }
    }

    private static byte[] readAll(InputStream in, int max) throws java.io.IOException {
        java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
        byte[] b = new byte[1 << 16];
        int n;
        while ((n = in.read(b)) > 0) {
            out.write(b, 0, n);
            if (out.size() > max) throw new java.io.IOException("too large");
        }
        return out.toByteArray();
    }

    private static final class Resp {
        final int code;
        final byte[] body;

        Resp(int code, byte[] body) {
            this.code = code;
            this.body = body;
        }
    }

    private static Resp call(String method, String url, String token, byte[] body) throws java.io.IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setRequestMethod(method);
        c.setConnectTimeout(20000);
        c.setReadTimeout(60000);
        c.setRequestProperty("Origin", "https://aitherium.com");
        c.setRequestProperty("User-Agent", NodeLink.UA);
        c.setRequestProperty("x-aither-device-token", token);
        String cookie = CookieManager.getInstance().getCookie("https://api.aitherium.com");
        if (cookie != null) c.setRequestProperty("Cookie", cookie);
        if (body != null) {
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "POST".equals(method) ? "application/json" : "application/octet-stream");
            try (OutputStream o = c.getOutputStream()) {
                o.write(body);
            }
        }
        int code = c.getResponseCode();
        InputStream in = code < 400 ? c.getInputStream() : c.getErrorStream();
        return new Resp(code, in == null ? new byte[0] : readAll(in, 65 << 20));
    }
}
