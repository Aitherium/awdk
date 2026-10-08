package com.aitherium.aither;

/**
 * When the watch stops recording, and the WAV it sends: plain Java (test/WearMicCheck.java).
 */
final class WearMicRules {
    /** Quiet after speech that ends the question. */
    static final int TRAILING_MS = 900;
    /** The longest question. */
    static final int MAX_MS = 12000;
    /** Giving up when nobody speaks. */
    static final int NO_SPEECH_MS = 5000;
    /** Under this, a recording is not worth sending. */
    static final int MIN_MS = 400;

    private WearMicRules() {}

    /** Speech/quiet endpointing on frame loudness, with an adaptive noise floor. */
    static final class Endpoint {
        private float floor = -1;
        private boolean speech;
        private int elapsed, quiet, voiced;

        /** One frame of {@code ms}; true when recording should stop. */
        boolean frame(float rms, int ms) {
            elapsed += ms;
            if (floor < 0) floor = rms;
            boolean loud = rms > Math.max(500f, floor * 3f);
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
            if (!speech) return elapsed >= NO_SPEECH_MS;
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
