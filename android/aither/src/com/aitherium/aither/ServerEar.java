package com.aitherium.aither;

import android.content.Context;
import android.media.MediaRecorder;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.file.Files;
import java.util.Map;
import java.util.UUID;

/**
 * The last ear (Hear.Route.SERVER): record a short AAC clip on this device, end it when the
 * person stops talking (Hear.Ear), and ask Aither's own recognizer,
 * POST {base}/api/voice/hear, with the caller's credentials. The clip is a
 * cache file deleted as soon as it is sent; nothing is kept.
 *
 * Shared by the phone (session cookie, app.aitherium.com) and the watch (bearer,
 * api.aitherium.com): the caller supplies the base URL and the auth headers. Every callback
 * runs on the main thread.
 */
final class ServerEar {
    interface Listener {
        /** Recording started. */
        void listening();
        /** The words Aither heard (may be "" when the engine heard no speech). */
        void heard(String text);
        /** Nobody spoke before the silence timeout; nothing was sent. */
        void silent();
        /** A refusal or failure, in words for the person (Hear.serverError). */
        void failed(String message);
    }

    private static final long TICK_MS = 100L;

    private final Context ctx;
    private final Handler main = new Handler(Looper.getMainLooper());
    private MediaRecorder rec;
    private File clip;
    private Hear.Ear ear;
    private long startedAt;
    private Listener who;
    private String base;
    private Map<String, String> headers;

    ServerEar(Context c) { ctx = c.getApplicationContext(); }

    boolean active() { return rec != null; }

    /** Start recording. The caller already holds the microphone permission. */
    void start(String base, Map<String, String> headers, Listener l) {
        cancel();
        this.base = base;
        this.headers = headers;
        who = l;
        ear = new Hear.Ear();
        clip = new File(ctx.getCacheDir(), "aither-ear.m4a");
        try {
            rec = android.os.Build.VERSION.SDK_INT >= 31 ? new MediaRecorder(ctx) : legacyRecorder();
            rec.setAudioSource(MediaRecorder.AudioSource.VOICE_RECOGNITION);
            rec.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4);
            rec.setAudioEncoder(MediaRecorder.AudioEncoder.AAC);
            rec.setAudioSamplingRate(16000);
            rec.setAudioEncodingBitRate(32000);
            rec.setAudioChannels(1);
            rec.setOutputFile(clip.getPath());
            rec.prepare();
            rec.start();
        } catch (Exception e) {
            release();
            l.failed("The microphone is busy. Try again.");
            return;
        }
        startedAt = SystemClock.elapsedRealtime();
        android.util.Log.i("AitherVoice", "listening (Aither's recognizer)");
        l.listening();
        main.postDelayed(this::tick, TICK_MS);
    }

    /** The person tapped stop: send what was said so far. */
    void finish() {
        if (rec == null) return;
        Listener l = who;
        if (ear != null && ear.spoke()) end(true, l); else end(false, l);
    }

    /** Drop the recording without sending anything. */
    void cancel() {
        main.removeCallbacksAndMessages(null);
        release();
        deleteClip();
    }

    private void tick() {
        if (rec == null) return;
        int level;
        try { level = rec.getMaxAmplitude(); } catch (Exception e) { level = 0; }
        Hear.Step s = ear.feed(SystemClock.elapsedRealtime() - startedAt, level);
        if (s == Hear.Step.KEEP) { main.postDelayed(this::tick, TICK_MS); return; }
        end(s == Hear.Step.DONE, who);
    }

    private void end(boolean send, Listener l) {
        main.removeCallbacksAndMessages(null);
        try { if (rec != null) rec.stop(); } catch (Exception e) { send = false; } // too short to encode
        release();
        if (!send) { deleteClip(); if (l != null) l.silent(); return; }
        File f = clip;
        String b = base;
        Map<String, String> h = headers;
        new Thread(() -> upload(f, b, h, l), "aither-ear").start();
    }

    private void upload(File f, String base, Map<String, String> h, Listener l) {
        int code = 0;
        String text = null;
        try {
            byte[] audio = Files.readAllBytes(f.toPath());
            String boundary = "aither" + UUID.randomUUID().toString().replace("-", "");
            byte[] body = Hear.multipart(boundary, "clip.m4a", "audio/mp4", audio);
            HttpURLConnection c = (HttpURLConnection) new URL(base + Hear.TRANSCRIBE_PATH).openConnection();
            c.setConnectTimeout(15000);
            c.setReadTimeout(90000);
            c.setDoOutput(true);
            c.setRequestMethod("POST");
            c.setRequestProperty("Content-Type", Hear.contentType(boundary));
            c.setRequestProperty("Accept", "application/json");
            for (Map.Entry<String, String> e : h.entrySet()) c.setRequestProperty(e.getKey(), e.getValue());
            c.setFixedLengthStreamingMode(body.length);
            try (OutputStream o = c.getOutputStream()) { o.write(body); }
            code = c.getResponseCode();
            if (code == 200) {
                try (InputStream in = c.getInputStream()) {
                    text = new JSONObject(read(in)).optString("text", "");
                }
            }
            c.disconnect();
        } catch (Exception e) {
            android.util.Log.w("AitherVoice", "transcribe failed: " + e.getClass().getSimpleName());
        } finally {
            deleteFile(f);
        }
        final int status = code;
        final String words = text;
        main.post(() -> {
            if (l == null) return;
            if (words != null) l.heard(words.trim());
            else l.failed(Hear.serverError(status));
        });
    }

    @SuppressWarnings("deprecation") // the only constructor below API 31 (minSdk 29)
    private static MediaRecorder legacyRecorder() { return new MediaRecorder(); }

    private static String read(InputStream in) throws java.io.IOException {
        ByteArrayOutputStream o = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        for (int n; (n = in.read(buf)) > 0; ) o.write(buf, 0, n);
        return o.toString("UTF-8");
    }

    private void release() {
        MediaRecorder r = rec;
        rec = null;
        if (r != null) try { r.release(); } catch (Exception e) { /* already released */ }
    }

    private void deleteClip() { deleteFile(clip); }

    private static void deleteFile(File f) {
        if (f != null && f.exists() && !f.delete()) f.deleteOnExit();
    }
}
