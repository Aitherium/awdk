package com.aitherium.aither;

/** Exit 0 when the watch's recording rules (WearMicRules) hold. */
public class WearMicCheck {
    static int bad = 0;

    static void eq(String what, Object got, Object want) {
        if (!String.valueOf(got).equals(String.valueOf(want))) {
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
            bad++;
        }
    }

    /** Frames of 20 ms until the endpoint says stop; the elapsed ms, or -1. */
    static int run(WearMicRules.Endpoint e, float[] script) {
        int t = 0;
        for (float rms : script) {
            t += 20;
            if (e.frame(rms, 20)) return t;
        }
        return -1;
    }

    static float[] seq(Object... parts) {
        java.util.List<Float> out = new java.util.ArrayList<>();
        for (int i = 0; i < parts.length; i += 2) {
            float v = ((Number) parts[i]).floatValue();
            int ms = (Integer) parts[i + 1];
            for (int k = 0; k < ms / 20; k++) out.add(v);
        }
        float[] f = new float[out.size()];
        for (int i = 0; i < f.length; i++) f[i] = out.get(i);
        return f;
    }

    public static void main(String[] a) {
        // speech, then quiet: stops TRAILING_MS after the speech
        int t = run(new WearMicRules.Endpoint(), seq(100, 300, 4000, 1500, 100, 3000));
        eq("stops after trailing quiet", t, 300 + 1500 + WearMicRules.TRAILING_MS);

        // a short pause inside the question does not end it
        t = run(new WearMicRules.Endpoint(), seq(100, 200, 4000, 800, 100, 500, 4000, 800, 100, 3000));
        eq("pause kept", t, 200 + 800 + 500 + 800 + WearMicRules.TRAILING_MS);

        // nobody speaks: gives up
        WearMicRules.Endpoint quiet = new WearMicRules.Endpoint();
        eq("no speech", run(quiet, seq(100, 8000)), WearMicRules.NO_SPEECH_MS);
        eq("no speech heard", quiet.heardSpeech(), false);

        // a click is not speech
        WearMicRules.Endpoint click = new WearMicRules.Endpoint();
        run(click, seq(100, 200, 5000, 40, 100, 6000));
        eq("click", click.heardSpeech(), false);

        // never longer than MAX_MS
        eq("cap", run(new WearMicRules.Endpoint(), seq(100, 100, 4000, 20000)), WearMicRules.MAX_MS);

        // the WAV header
        byte[] w = WearMicRules.wav(new byte[3200], 16000);
        eq("riff", new String(w, 0, 4), "RIFF");
        eq("len", w.length, 44 + 3200);
        eq("rate", (w[24] & 0xFF) | ((w[25] & 0xFF) << 8), 16000);
        eq("worth 100ms", WearMicRules.worthSending(3200, 16000), false);
        eq("worth 1s", WearMicRules.worthSending(32000, 16000), true);

        System.out.println(bad == 0 ? "OK" : bad + " failed");
        System.exit(bad == 0 ? 0 : 1);
    }
}
