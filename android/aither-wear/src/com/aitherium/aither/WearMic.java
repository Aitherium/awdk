package com.aitherium.aither;

import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;

import java.io.ByteArrayOutputStream;

/**
 * Voice input on a watch without an on-device recognizer (the Pixel Watch has none): the
 * watch records one question and AitherOS's own speech-to-text hears it (POST
 * /api/voice/hear: any signed-in caller, child accounts included; /api/voice/transcribe is
 * developer-plan only and answered a kid's watch 403). The audio goes to Aither and nowhere
 * else: Google's recognizer is never used (Talk's rule).
 *
 * Recording stops on its own after {@link WearMicRules#TRAILING_MS} of quiet once speech has
 * started, at {@link WearMicRules#MAX_MS}, or when {@link #stop} is called (the screen's
 * "Done" button). 16 kHz mono 16-bit PCM, sent as a WAV.
 */
final class WearMic {
    static final int RATE = 16000;
    static final String HEAR_PATH = "/api/voice/hear";

    interface Listener {
        /** 0..1 loudness of the last frame, for the listening ring. */
        void level(float v);
        /** Speech has ended; the audio is on its way to be heard. */
        void sending();
    }

    private volatile boolean stopped;
    /** The last recording heard speech (false: nobody spoke, and the caller just closes). */
    volatile boolean heardSpeech;
    /** Stopped by a tap (send now) rather than by the endpoint. */
    private volatile boolean tapped;

    void stop() { tapped = true; stopped = true; }

    /**
     * Record and transcribe (blocking; call off the UI thread). Returns the words, or "" with
     * why in err[0]. The caller must hold RECORD_AUDIO.
     */
    String hear(WearApi api, Listener l, String[] err) {
        stopped = false;
        tapped = false;
        heardSpeech = false;
        byte[] pcm = record(l, err);
        if (pcm == null) return "";
        if (!heardSpeech && !tapped) return ""; // nobody spoke within NO_SPEECH_MS: close quietly
        if (!WearMicRules.worthSending(pcm.length, RATE)) {
            err[0] = "I didn't hear anything. Tap Try again.";
            return "";
        }
        l.sending();
        try {
            WearApi.Resp r = api.postAudio(HEAR_PATH, WearMicRules.wav(pcm, RATE), "watch.wav",
                    "audio/wav", 60000);
            String text = r.code == 200 ? r.str("text").trim() : "";
            if (text.isEmpty()) {
                err[0] = r.code == 401 ? "Signed out. Sign in again."
                        : r.code == 0 ? "No connection. Tap Try again."
                        : r.code == 200 ? "I didn't catch that. Tap Try again."
                        : "Couldn't hear that (" + r.code + "). Tap Try again.";
            }
            return text;
        } catch (Exception e) {
            err[0] = "Couldn't hear that. Tap Try again.";
            return "";
        }
    }

    private byte[] record(Listener l, String[] err) {
        int min = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        if (min <= 0) {
            err[0] = "The microphone isn't available.";
            return null;
        }
        AudioRecord rec;
        try {
            rec = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, RATE,
                    AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, Math.max(min, RATE));
        } catch (SecurityException e) {
            err[0] = "Aither needs the microphone to listen.";
            return null;
        }
        if (rec.getState() != AudioRecord.STATE_INITIALIZED) {
            rec.release();
            err[0] = "The microphone is busy.";
            return null;
        }
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        short[] frame = new short[RATE / 50]; // 20 ms
        byte[] bytes = new byte[frame.length * 2];
        WearMicRules.Endpoint ep = new WearMicRules.Endpoint();
        try {
            rec.startRecording();
            while (!stopped) {
                int n = rec.read(frame, 0, frame.length);
                if (n <= 0) break;
                for (int i = 0; i < n; i++) {
                    bytes[i * 2] = (byte) frame[i];
                    bytes[i * 2 + 1] = (byte) (frame[i] >> 8);
                }
                out.write(bytes, 0, n * 2);
                float rms = WearMicRules.rms(frame, n);
                l.level(Math.min(1f, rms / 6000f));
                if (ep.frame(rms, n * 1000 / RATE)) break;
            }
            heardSpeech = ep.heardSpeech();
        } finally {
            try { rec.stop(); } catch (Exception e) { /* not started */ }
            rec.release();
        }
        return out.toByteArray();
    }
}
