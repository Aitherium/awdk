package com.aitherium.aither;

import android.content.Context;
import android.content.SharedPreferences;
import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.util.Base64;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;

/**
 * The watch reads Aither's answer aloud WHILE it streams, sentence by sentence, in Aither's
 * own voice: Genesis /voice-builds/platform-voice/say (custom:aither, any signed-in caller,
 * 240 characters a line, 40 uncached lines a minute) played on the watch speaker.
 *
 * Each finished sentence (WearFeed) is queued with {@link #add}: its audio is fetched at once,
 * the next one's while the current one plays, so speech follows the text by one synthesis
 * (about 1.5 s) instead of waiting for the whole answer. While a line plays, {@link Progress}
 * hears which character the voice has reached, so the screen lights the words as they are
 * said, by the audio clock.
 *
 * ONLY Aither's voice, never another (owner, 2026-10-08: "NO FALLBACKS"). Lines are asked
 * for as Ogg/Opus ({"format": "opus"}): the same voice in about a twelfth of the WAV's
 * bytes, because a watch riding the phone's Bluetooth pulled 600 KB WAV lines slower than
 * it could speak them, and 0.3.19-0.3.20 then handed the answer to Android's robotic
 * voice. A line that cannot be fetched is tried once more; if it still fails the text
 * stays on screen and Progress.unavailable() shows a small "voice unavailable" note.
 * Talk.spoken rules (via WearFeed): no "*actions*", emoji, code or links read out.
 *
 * One switch, kept across launches: "voice" in the wear prefs (on unless turned off).
 *
 * Every failure is logged under AitherWearVoice with its cause (the /say status, a player
 * error), and audio focus is asked for while an answer is spoken.
 */
final class WearVoice {
    static final int LINE = 240; // Genesis PlatformSayRequest.text max_length
    /** How long the player waits for a line's audio before asking for it once more. */
    static final long FETCH_WAIT_MS = 12000;
    private static final String PREF = "voice";
    private static final String TAG = "AitherWearVoice";

    /** Called from the voice thread; callers post to the UI themselves. */
    interface Progress {
        /** The voice has reached {@code upTo} characters into {@code s}. */
        void speaking(WearFeed.Sentence s, int upTo);
        /** The first sound of this answer. */
        void started();
        /** Everything queued has been said (or skipped). */
        void done();
        /** A line could not be had in Aither's voice even on a second try: its text stays,
         *  unspoken (never another voice). */
        default void unavailable() {}
    }

    private static final class Line {
        final WearFeed.Sentence s;
        final Future<byte[]> wav;
        Line(WearFeed.Sentence s, Future<byte[]> wav) { this.s = s; this.wav = wav; }
    }

    private static final Line END = new Line(null, null);

    private final Context ctx;
    private final WearApi api;
    private final SharedPreferences p;
    private final ExecutorService fetcher = Executors.newFixedThreadPool(3);
    private volatile int gen;
    private volatile MediaPlayer playing;
    private volatile LinkedBlockingQueue<Line> queue;

    WearVoice(Context c, WearApi api) {
        ctx = c.getApplicationContext();
        this.api = api;
        p = ctx.getSharedPreferences("wear", Context.MODE_PRIVATE);
        migrate();
    }

    private android.media.AudioFocusRequest focus;

    private void takeFocus() {
        try {
            android.media.AudioManager am = ctx.getSystemService(android.media.AudioManager.class);
            focus = new android.media.AudioFocusRequest.Builder(
                    android.media.AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
                    .setAudioAttributes(new AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_ASSISTANT)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build()).build();
            int got = am.requestAudioFocus(focus);
            if (got != android.media.AudioManager.AUDIOFOCUS_REQUEST_GRANTED) {
                android.util.Log.w(TAG, "audio focus not granted (" + got + "); speaking anyway");
            }
        } catch (Exception e) {
            android.util.Log.w(TAG, "audio focus failed: " + e);
        }
    }

    private void dropFocus() {
        try {
            if (focus != null) ctx.getSystemService(android.media.AudioManager.class).abandonAudioFocusRequest(focus);
        } catch (Exception e) { /* nothing held */ }
        focus = null;
    }

    boolean on() { return p.getBoolean(PREF, true); }

    /**
     * 0.3.20, once: spoken answers back ON. On 0.3.19 a stray tap on the round edge (the
     * old one-tap toggle in the home list) turned them off and the owner heard nothing.
     * From here the switch lives in Settings behind a confirm (WearActivity.settings).
     */
    static final String MIGRATED = "voice_v2";

    void migrate() {
        if (p.getBoolean(MIGRATED, false)) return;
        boolean was = on();
        p.edit().putBoolean(PREF, true).putBoolean(MIGRATED, true).apply();
        android.util.Log.i(TAG, "voice pref migration: spoken answers on (was " + (was ? "on" : "off") + ")");
    }

    /** Every change is logged with who made it ({@code source}: "settings", "answer screen"…). */
    void setOn(boolean v, String source) {
        android.util.Log.i(TAG, "spoken answers " + (v ? "on" : "off") + " (from " + source + ", was "
                + (on() ? "on" : "off") + ")");
        p.edit().putBoolean(PREF, v).apply();
        if (!v) stop();
    }

    /** A new answer: whatever was being said stops, and lines added from now on are this one's. */
    void begin(Progress progress) {
        stop();
        int at = gen;
        LinkedBlockingQueue<Line> q = new LinkedBlockingQueue<>();
        queue = q;
        new Thread(() -> run(at, q, progress), "wear-voice").start();
    }

    /** Queue one sentence; its audio is fetched now. */
    void add(WearFeed.Sentence s) {
        LinkedBlockingQueue<Line> q = queue;
        if (q == null || s == null || s.say.isEmpty()) return;
        if (!on()) {
            android.util.Log.i(TAG, "spoken answers are off: not speaking");
            return;
        }
        int at = gen;
        Future<byte[]> wav = fetcher.submit(() -> at == gen ? fetch(s.say) : null);
        q.add(new Line(s, wav));
    }

    void addAll(List<WearFeed.Sentence> list) {
        for (WearFeed.Sentence s : list) add(s);
    }

    /** No more lines for this answer: done() comes once the queue is said. */
    void finish() {
        LinkedBlockingQueue<Line> q = queue;
        if (q != null) q.add(END);
    }

    void stop() {
        gen++;
        LinkedBlockingQueue<Line> q = queue;
        queue = null;
        if (q != null) { q.clear(); q.add(END); }
        MediaPlayer m = playing;
        playing = null;
        if (m != null) try { m.stop(); } catch (Exception e) { /* already done */ }
    }

    void shutdown() {
        stop();
        fetcher.shutdownNow();
    }

    private void run(int at, LinkedBlockingQueue<Line> q, Progress progress) {
        boolean first = true;
        long began = android.os.SystemClock.elapsedRealtime();
        try {
            while (at == gen) {
                Line l = q.poll(180, TimeUnit.SECONDS);
                if (l == null || l == END || at != gen) break;
                byte[] wav = null;
                long waitFrom = android.os.SystemClock.elapsedRealtime();
                if (l.wav != null) {
                    try { wav = l.wav.get(FETCH_WAIT_MS, TimeUnit.MILLISECONDS); } catch (Exception e) { wav = null; }
                }
                if (wav == null && at == gen) {
                    android.util.Log.w(TAG, "a line's audio did not come; asking once more");
                    wav = fetch(l.s.say);
                }
                if (at != gen) break;
                if (first) { first = false; takeFocus(); progress.started(); }
                long waited = android.os.SystemClock.elapsedRealtime() - waitFrom;
                long playFrom = android.os.SystemClock.elapsedRealtime();
                boolean ok = wav != null && play(at, wav, l.s, progress);
                // waited > 0 is a gap in the speech (synthesis behind); played is the line's length
                if (ok) android.util.Log.i("AitherWearLat", "line_said ms=" + (android.os.SystemClock.elapsedRealtime() - began)
                        + " waited=" + waited + " played=" + (android.os.SystemClock.elapsedRealtime() - playFrom)
                        + " chars=" + l.s.say.length() + (q.isEmpty() ? " (caught up)" : ""));
                if (!ok && at == gen) {
                    android.util.Log.w(TAG, "Aither's voice unavailable for a line: its text stays, unspoken");
                    progress.unavailable();
                    progress.speaking(l.s, l.s.text.length());
                }
            }
        } catch (InterruptedException e) {
            return;
        }
        dropFocus();
        if (at == gen) progress.done();
    }

    private byte[] fetch(String line) {
        try {
            String text = line.length() > LINE ? line.substring(0, LINE) : line;
            WearApi.Resp r = api.post("/api/genesis/voice-builds/platform-voice/say",
                    new JSONObject().put("text", text).put("format", "opus"), true, 15000);
            String b64 = r.code == 200 ? r.str("audio_base64") : "";
            if (b64.isEmpty()) {
                android.util.Log.w(TAG, "Aither's voice refused a line: HTTP " + r.code + " "
                        + ApprovalCard.clip(r.text == null ? "" : r.text, 120));
                return null;
            }
            return Base64.decode(b64, Base64.DEFAULT);
        } catch (Exception e) {
            android.util.Log.w(TAG, "Aither's voice failed: " + e);
            return null;
        }
    }

    /** Play one line to the end, lighting words by the audio clock. False when it could not play. */
    private boolean play(int at, byte[] wav, WearFeed.Sentence s, Progress progress) {
        // Ogg/Opus starts "OggS", WAV "RIFF": name the file for what it is
        boolean ogg = wav.length > 4 && wav[0] == 'O' && wav[1] == 'g' && wav[2] == 'g' && wav[3] == 'S';
        File f = new File(ctx.getCacheDir(), ogg ? "aither-say.ogg" : "aither-say.wav");
        try (FileOutputStream o = new FileOutputStream(f)) {
            o.write(wav);
        } catch (Exception e) {
            return false;
        }
        CountDownLatch done = new CountDownLatch(1);
        MediaPlayer m = new MediaPlayer();
        try {
            m.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ASSISTANT)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build());
            m.setDataSource(f.getPath());
            m.setOnCompletionListener(x -> done.countDown());
            m.setOnErrorListener((x, w, e) -> {
                android.util.Log.w(TAG, "player error " + w + "/" + e);
                done.countDown();
                return true;
            });
            m.prepare();
            if (at != gen) return true;
            playing = m;
            int dur = Math.max(1, m.getDuration());
            m.start();
            int[] ends = WearFeed.wordEnds(s.text);
            long limit = System.currentTimeMillis() + dur + 3000;
            while (!done.await(40, TimeUnit.MILLISECONDS)) {
                if (at != gen || System.currentTimeMillis() > limit) break;
                int pos;
                try { pos = m.getCurrentPosition(); } catch (Exception e) { break; }
                progress.speaking(s, WearFeed.reached(ends, pos, dur));
            }
            if (at == gen) progress.speaking(s, s.text.length());
            return true;
        } catch (Exception e) {
            android.util.Log.w(TAG, "could not play Aither's voice: " + e);
            return false;
        } finally {
            if (playing == m) playing = null;
            try { m.release(); } catch (Exception e) { /* released */ }
        }
    }
}
