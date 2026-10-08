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
        eq("cap", run(new WearMicRules.Endpoint(), seq(100, 100, 4000, 40000)), WearMicRules.MAX_MS);

        // talking from the very first frame still ends by itself (the floor is not the voice)
        t = run(new WearMicRules.Endpoint(), seq(3000, 1500, 150, 3000));
        eq("talk at once ends", t, 1500 + WearMicRules.TRAILING_MS);

        // a noisy room (floor ~900) still hears speech and its end
        t = run(new WearMicRules.Endpoint(), seq(900, 400, 4000, 1200, 900, 3000));
        eq("noisy room ends", t, 400 + 1200 + WearMicRules.TRAILING_MS);

        // the WAV header
        byte[] w = WearMicRules.wav(new byte[3200], 16000);
        eq("riff", new String(w, 0, 4), "RIFF");
        eq("len", w.length, 44 + 3200);
        eq("rate", (w[24] & 0xFF) | ((w[25] & 0xFF) << 8), 16000);
        eq("worth 100ms", WearMicRules.worthSending(3200, 16000), false);
        eq("worth 1s", WearMicRules.worthSending(32000, 16000), true);

        // the clip rides as multipart field "audio" (POST /api/voice/hear), bytes untouched
        byte[] clip = {0, 1, (byte) 0xff, 13, 10};
        byte[] mp = WearMicRules.multipart("b0", "watch.wav", "audio/wav", clip);
        String s = new String(mp, java.nio.charset.StandardCharsets.ISO_8859_1);
        eq("multipart head", s.startsWith("--b0\r\nContent-Disposition: form-data; name=\"audio\"; "
                + "filename=\"watch.wav\"\r\nContent-Type: audio/wav\r\n\r\n"), true);
        eq("multipart tail", s.endsWith("\r\n--b0--\r\n"), true);
        int at = s.indexOf("\r\n\r\n") + 4;
        eq("multipart bytes", java.util.Arrays.equals(
                java.util.Arrays.copyOfRange(mp, at, at + clip.length), clip), true);

        System.out.println(bad == 0 ? "OK" : bad + " failed");
        System.exit(bad == 0 ? 0 : 1);
    }
}
