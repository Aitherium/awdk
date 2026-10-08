package com.aitherium.aither;

import android.content.Context;
import android.content.SharedPreferences;
import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.speech.tts.TextToSpeech;
import android.util.Base64;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * The watch reads Aither's answer aloud, in Aither's own voice: Genesis
 * /voice-builds/platform-voice/say (custom:aither, any signed-in caller, 240 characters a
 * line) played on the watch speaker. When that voice cannot play (offline, refused), the
 * watch's own TextToSpeech reads the rest. Talk.spoken first: no "*actions*", emoji, code
 * or links read out. The answer already came from the server, so speaking it there sends
 * nothing new off the watch.
 *
 * One switch, kept across launches: "voice" in the wear prefs (on unless turned off).
 */
final class WearVoice {
    static final int LINE = 240; // Genesis PlatformSayRequest.text max_length
    private static final String PREF = "voice";

    private final Context ctx;
    private final WearApi api;
    private final SharedPreferences p;
    private TextToSpeech tts;
    private boolean ttsReady;
    private volatile int gen;
    private volatile MediaPlayer playing;

    WearVoice(Context c, WearApi api) {
        ctx = c.getApplicationContext();
        this.api = api;
        p = ctx.getSharedPreferences("wear", Context.MODE_PRIVATE);
        tts = new TextToSpeech(ctx, status -> ttsReady = status == TextToSpeech.SUCCESS);
    }

    boolean on() { return p.getBoolean(PREF, true); }

    void setOn(boolean v) {
        p.edit().putBoolean(PREF, v).apply();
        if (!v) stop();
    }

    /** Speak `answer` on a background thread. A newer speak() or stop() cuts it off. */
    void speak(String answer) {
        if (!on()) return;
        String said = Talk.spoken(answer);
        if (said.isEmpty()) return;
        android.util.Log.i("AitherSpeak", said);
        int at = ++gen;
        new Thread(() -> run(at, pieces(said)), "wear-voice").start();
    }

    void stop() {
        gen++;
        MediaPlayer m = playing;
        playing = null;
        if (m != null) try { m.stop(); m.release(); } catch (Exception e) { /* already done */ }
        if (tts != null && ttsReady) tts.stop();
    }

    void shutdown() {
        stop();
        if (tts != null) { tts.shutdown(); tts = null; }
    }

    private void run(int at, List<String> lines) {
        for (int i = 0; i < lines.size(); i++) {
            if (at != gen) return;
            byte[] wav = fetch(lines.get(i));
            if (at != gen) return;
            if (wav == null || !play(at, wav)) {
                // Aither's voice is unavailable: the watch's own voice reads what is left.
                StringBuilder rest = new StringBuilder();
                for (int j = i; j < lines.size(); j++) rest.append(lines.get(j)).append(' ');
                if (tts != null && ttsReady && at == gen) {
                    tts.speak(rest.toString().trim(), TextToSpeech.QUEUE_FLUSH, null, "wear-answer");
                }
                return;
            }
        }
    }

    private byte[] fetch(String line) {
        try {
            WearApi.Resp r = api.post("/api/genesis/voice-builds/platform-voice/say",
                    new JSONObject().put("text", line), true, 30000);
            String b64 = r.code == 200 ? r.str("audio_base64") : "";
            return b64.isEmpty() ? null : Base64.decode(b64, Base64.DEFAULT);
        } catch (Exception e) {
            return null;
        }
    }

    /** Play one WAV to the end. False when it could not play. */
    private boolean play(int at, byte[] wav) {
        File f = new File(ctx.getCacheDir(), "aither-say.wav");
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
            m.setOnErrorListener((x, w, e) -> { done.countDown(); return true; });
            m.prepare();
            if (at != gen) { m.release(); return true; }
            playing = m;
            m.start();
            done.await(Math.max(10, wav.length / 22050 + 5), TimeUnit.SECONDS);
            return true;
        } catch (Exception e) {
            return false;
        } finally {
            if (playing == m) playing = null;
            try { m.release(); } catch (Exception e) { /* released */ }
        }
    }

    /** Lines of at most LINE characters, cut at a sentence end, else at a space. */
    static List<String> pieces(String text) {
        List<String> out = new ArrayList<>();
        String rest = text.trim();
        while (rest.length() > LINE) {
            String head = rest.substring(0, LINE);
            int cut = Math.max(head.lastIndexOf(". "), Math.max(head.lastIndexOf("! "), head.lastIndexOf("? ")));
            if (cut < LINE / 3) cut = head.lastIndexOf(' ');
            if (cut <= 0) cut = LINE - 1;
            out.add(rest.substring(0, cut + 1).trim());
            rest = rest.substring(cut + 1).trim();
        }
        if (!rest.isEmpty()) out.add(rest);
        return out;
    }
}
