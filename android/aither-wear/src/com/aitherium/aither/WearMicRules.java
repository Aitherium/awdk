package com.aitherium.aither;

/**
 * When the watch stops recording, and the WAV it sends: plain Java (test/WearMicCheck.java).
 */
final class WearMicRules {
    /** Quiet after speech that ends the question (no Done tap: owner, 2026-10-08). */
    static final int TRAILING_MS = 800;
    /** The longest question. */
    static final int MAX_MS = 30000;
    /** Giving up when nobody speaks. */
    static final int NO_SPEECH_MS = 5000;
    /** In a conversation, the quiet after an answer that ends it (owner, 2026-10-08: ~8 s). */
    static final int FOLLOW_UP_NO_SPEECH_MS = 8000;
    /** Under this, a recording is not worth sending. */
    static final int MIN_MS = 400;

    private WearMicRules() {}

    /** Speech/quiet endpointing on frame loudness, with an adaptive noise floor. */
    static final class Endpoint {
        private float floor = -1;
        private boolean speech;
        private int elapsed, quiet, voiced;
        private final int noSpeechMs;

        Endpoint() { this(NO_SPEECH_MS); }

        /** {@code noSpeechMs}: how long to wait for the first word before giving up. */
        Endpoint(int noSpeechMs) { this.noSpeechMs = noSpeechMs; }

        /** One frame of {@code ms}; true when recording should stop. */
        boolean frame(float rms, int ms) {
            elapsed += ms;
            // the floor is seeded low: someone who starts talking at once must not teach it
            // their own voice as "quiet" (then nothing is ever loud and Talk never ends)
            if (floor < 0) floor = Math.min(rms, 800f);
            else if (elapsed <= 200) floor = Math.min(floor, rms);
            boolean loud = rms > Math.max(450f, floor * 2.5f);
            if (!loud) floor = floor * 0.95f + rms * 0.05f;
            if (loud) {
                voiced += ms;
                quiet = 0;
                if (voiced >= 120) speech = true;
            } else {
                quiet += ms;
                if (!speech) voiced = 0;
            }
            if (elapsed >= MAX_MS) return true;
            if (!speech) return elapsed >= noSpeechMs;
            return quiet >= TRAILING_MS;
        }

        boolean heardSpeech() { return speech; }
    }

    static float rms(short[] s, int n) {
        if (n <= 0) return 0;
        double sum = 0;
        for (int i = 0; i < n; i++) sum += (double) s[i] * s[i];
        return (float) Math.sqrt(sum / n);
    }

    static boolean worthSending(int pcmBytes, int rate) {
        return pcmBytes / 2 * 1000L / rate >= MIN_MS;
    }

    /** A 16-bit mono PCM WAV around {@code pcm}. */
    static byte[] wav(byte[] pcm, int rate) {
        byte[] out = new byte[44 + pcm.length];
        put(out, 0, "RIFF");
        le32(out, 4, 36 + pcm.length);
        put(out, 8, "WAVEfmt ");
        le32(out, 16, 16);
        le16(out, 20, 1);
        le16(out, 22, 1);
        le32(out, 24, rate);
        le32(out, 28, rate * 2);
        le16(out, 32, 2);
        le16(out, 34, 16);
        put(out, 36, "data");
        le32(out, 40, pcm.length);
        System.arraycopy(pcm, 0, out, 44, pcm.length);
        return out;
    }

    /**
     * One multipart/form-data body carrying {@code audio} as the field POST /api/voice/hear
     * reads ({@code audio}, not {@code file}).
     */
    static byte[] multipart(String boundary, String filename, String mime, byte[] audio) {
        java.nio.charset.Charset utf8 = java.nio.charset.StandardCharsets.UTF_8;
        byte[] head = ("--" + boundary + "\r\n"
                + "Content-Disposition: form-data; name=\"audio\"; filename=\"" + filename + "\"\r\n"
                + "Content-Type: " + mime + "\r\n\r\n").getBytes(utf8);
        byte[] tail = ("\r\n--" + boundary + "--\r\n").getBytes(utf8);
        byte[] out = new byte[head.length + audio.length + tail.length];
        System.arraycopy(head, 0, out, 0, head.length);
        System.arraycopy(audio, 0, out, head.length, audio.length);
        System.arraycopy(tail, 0, out, head.length + audio.length, tail.length);
        return out;
    }

    private static void put(byte[] b, int at, String s) {
        for (int i = 0; i < s.length(); i++) b[at + i] = (byte) s.charAt(i);
    }

    private static void le32(byte[] b, int at, int v) {
        for (int i = 0; i < 4; i++) b[at + i] = (byte) (v >> (8 * i));
    }

    private static void le16(byte[] b, int at, int v) {
        b[at] = (byte) v;
        b[at + 1] = (byte) (v >> 8);
    }
}
