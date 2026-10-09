package com.aitherium.aither;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.util.Base64;
import android.webkit.CookieManager;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

/**
 * Aither speaks on the phone in Aither's own voice and NO other (owner, 2026-10-08:
 * "NO FALLBACKS"): Genesis /voice-builds/platform-voice/say, asked for as Ogg/Opus (the same
 * voice in about a twelfth of the WAV's bytes), with the page's session cookie.
 *
 * An answer is split into sentences of at most 240 characters (the server's line cap); the
 * next two are fetched while one plays. A line that cannot be had is asked for once more;
 * if it still fails, {@code onUnavailable} runs and the rest stays text. The phone's
 * built-in TTS is never used.
 */
final class AitherSay {
    static final int LINE = 240;

    interface Listener {
        /** Aither's voice could not speak a line (the text stays on screen). */
        void onUnavailable();
    }

    private final Context ctx;
    private final ExecutorService fetcher = Executors.newFixedThreadPool(3);
    private volatile int gen;
    private volatile MediaPlayer playing;

    AitherSay(Context c) { ctx = c.getApplicationContext(); }

    /** Sentences of at most {@link #LINE} characters, in order. Pure (tested). */
    static List<String> lines(String text) {
        List<String> out = new ArrayList<>();
        if (text == null) return out;
        String t = text.trim();
        StringBuilder cur = new StringBuilder();
        for (String part : t.split("(?<=[.!?])\\s+")) {
            String p = part.trim();
            if (p.isEmpty()) continue;
            while (p.length() > LINE) { // one long sentence: cut at a space
                int cut = p.lastIndexOf(' ', LINE);
                if (cut <= 0) cut = LINE;
                if (cur.length() > 0) { out.add(cur.toString()); cur.setLength(0); }
                out.add(p.substring(0, cut).trim());
                p = p.substring(cut).trim();
            }
            if (cur.length() > 0 && cur.length() + 1 + p.length() > LINE) {
                out.add(cur.toString());
                cur.setLength(0);
            }
            if (cur.length() > 0) cur.append(' ');
            cur.append(p);
        }
        if (cur.length() > 0) out.add(cur.toString());
        return out;
    }

    /** Speak `text` (already Talk.spoken-cleaned); stops whatever was being said. */
    void speak(String text, Listener l) {
        stop();
        int at = gen;
        List<String> parts = lines(text);
        if (parts.isEmpty()) return;
        new Thread(() -> {
            List<Future<byte[]>> got = new ArrayList<>();
            for (String p : parts) got.add(fetcher.submit(() -> at == gen ? fetch(p) : null));
            for (int i = 0; i < parts.size() && at == gen; i++) {
                byte[] audio;
                try { audio = got.get(i).get(12, TimeUnit.SECONDS); } catch (Exception e) { audio = null; }
                if (audio == null && at == gen) audio = fetch(parts.get(i)); // once more
                if (audio == null || !play(at, audio)) {
                    android.util.Log.w("AitherSay", "Aither's voice unavailable: the rest stays text");
                    if (at == gen && l != null) l.onUnavailable();
                    return;
                }
            }
        }, "aither-say").start();
    }

    void stop() {
        gen++;
        MediaPlayer m = playing;
        playing = null;
        if (m != null) try { m.stop(); } catch (Exception e) { /* done */ }
    }

    void shutdown() {
        stop();
        fetcher.shutdownNow();
    }

    private byte[] fetch(String line) {
        try {
            String cookie = CookieManager.getInstance().getCookie(HeartbeatJob.API);
            if (cookie == null || cookie.isEmpty()) return null;
            HttpURLConnection c = (HttpURLConnection) new URL(HeartbeatJob.API
                    + "/api/genesis/voice-builds/platform-voice/say").openConnection();
            c.setRequestMethod("POST");
            c.setConnectTimeout(8000);
            c.setReadTimeout(15000);
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json");
            c.setRequestProperty("Origin", "https://aitherium.com");
            c.setRequestProperty("Cookie", cookie);
            try (OutputStream o = c.getOutputStream()) {
                o.write(new JSONObject().put("text", line).put("format", "opus").toString()
                        .getBytes(StandardCharsets.UTF_8));
            }
            if (c.getResponseCode() != 200) {
                android.util.Log.w("AitherSay", "say answered " + c.getResponseCode());
                return null;
            }
            StringBuilder sb = new StringBuilder();
            try (InputStream in = c.getInputStream()) {
                byte[] b = new byte[8192];
                int n;
                while ((n = in.read(b)) > 0) sb.append(new String(b, 0, n, StandardCharsets.UTF_8));
            }
            String b64 = new JSONObject(sb.toString()).optString("audio_base64", "");
            return b64.isEmpty() ? null : Base64.decode(b64, Base64.DEFAULT);
        } catch (Exception e) {
            android.util.Log.w("AitherSay", "say failed: " + e);
            return null;
        }
    }

    private boolean play(int at, byte[] audio) {
        boolean ogg = audio.length > 4 && audio[0] == 'O' && audio[1] == 'g' && audio[2] == 'g' && audio[3] == 'S';
        File f = new File(ctx.getCacheDir(), ogg ? "aither-say.ogg" : "aither-say.wav");
        try (FileOutputStream o = new FileOutputStream(f)) {
            o.write(audio);
        } catch (Exception e) {
            return false;
        }
        MediaPlayer m = new MediaPlayer();
        java.util.concurrent.CountDownLatch done = new java.util.concurrent.CountDownLatch(1);
        try {
            m.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ASSISTANT)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build());
            m.setDataSource(f.getPath());
            m.setOnCompletionListener(x -> done.countDown());
            m.setOnErrorListener((x, w, e) -> { done.countDown(); return true; });
            m.prepare();
            if (at != gen) return true;
            playing = m;
            m.start();
            done.await(Math.max(5, m.getDuration() / 1000 + 5), TimeUnit.SECONDS);
            return true;
        } catch (Exception e) {
            android.util.Log.w("AitherSay", "could not play: " + e);
            return false;
        } finally {
            if (playing == m) playing = null;
            try { m.release(); } catch (Exception e) { /* released */ }
        }
    }
}
