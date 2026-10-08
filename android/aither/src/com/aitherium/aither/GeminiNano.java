package com.aitherium.aither;

import android.content.Context;

/**
 * Gemini Nano as a local model tier, beside Bonsai (LlmService). Google ships Nano inside
 * Android's AICore and lets apps reach it only through the ML Kit GenAI Prompt API, a Play
 * services library this app's build (no Gradle, no AndroidX) does not carry. So the engine is
 * a seam: {@link Engine} is what Ask Aither needs, and the ML Kit implementation
 * (optional/gemini-nano/MlKitNano.java) is loaded by name when a build includes it. Without it
 * every phone reads as UNAVAILABLE and Bonsai answers, as before.
 *
 * Nothing here sends the question anywhere: Nano runs in AICore on the phone. (ML Kit itself
 * reports usage diagnostics to Google; see optional/gemini-nano/README.md.)
 */
final class GeminiNano {
    /** The ML Kit engine's class, present only in a build that includes it. */
    static final String IMPL = "com.aitherium.aither.MlKitNano";

    /** Streamed text as it is made (any thread). */
    interface Stream { void text(String chunk); }

    /** Download progress (any thread). */
    interface Progress {
        void bytes(long done);
        void done();
        void failed(int code);
    }

    /** A Nano failure with ML Kit's GenAiException error code (NanoRoute reacts to it). */
    static final class Failure extends Exception {
        final int code;
        Failure(int code, String message) {
            super(message);
            this.code = code;
        }
    }

    /** What an engine offers. Implementations block; never call them on the main thread. */
    interface Engine {
        /** One of NanoRoute's FeatureStatus values. */
        int status() throws Failure;
        void download(Progress p);
        /** The whole answer; chunks also go to {@code onText} as they come, if it is set. */
        String generate(String prompt, Stream onText) throws Failure;
    }

    private static Engine engine;
    private static boolean looked;
    private static int lastStatus = NanoRoute.UNAVAILABLE;
    private static long statusAt;
    private static long coolUntil;

    private GeminiNano() {}

    /** The engine, or null when this build has none. */
    static synchronized Engine engine(Context c) {
        if (!looked) {
            looked = true;
            try {
                engine = (Engine) Class.forName(IMPL).getDeclaredConstructor(Context.class)
                        .newInstance(c.getApplicationContext());
            } catch (Throwable t) { // absent from this build, or no Play services: no Nano
                engine = null;
            }
        }
        return engine;
    }

    /** FeatureStatus, cached for a minute (a check is an IPC to AICore). Blocks. */
    static int status(Context c) {
        Engine e = engine(c);
        if (e == null) return NanoRoute.UNAVAILABLE;
        synchronized (GeminiNano.class) {
            if (System.currentTimeMillis() - statusAt < 60_000L) return lastStatus;
        }
        int s;
        try {
            s = e.status();
        } catch (Failure | RuntimeException f) {
            s = NanoRoute.UNAVAILABLE;
        }
        synchronized (GeminiNano.class) {
            lastStatus = s;
            statusAt = System.currentTimeMillis();
        }
        return s;
    }

    static synchronized long coolUntil() { return coolUntil; }

    /** Nano failed: remember for how long Bonsai should answer instead. */
    static synchronized void failed(int code) {
        coolUntil = Math.max(coolUntil, NanoRoute.coolUntil(code, System.currentTimeMillis()));
        statusAt = 0; // look again next time
    }

    static synchronized void forgetStatus() { statusAt = 0; }
}
