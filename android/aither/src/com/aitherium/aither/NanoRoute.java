package com.aitherium.aither;

/**
 * Which model answers one Ask Aither turn: Gemini Nano (Google's model in Android's AICore,
 * on Pixel 9/10 and some others) or Bonsai (LlmService, our own engine on this phone). Pure
 * Java so test/NanoRouteCheck.java runs it on a desktop JVM.
 *
 * Nano takes the short, tool-free turns when it is ready on this phone and the owner left
 * "Use Gemini Nano" on; everything else stays with Bonsai, which runs the tools loop
 * (calendar_read, the page's WebMCP tools under McpPolicy). Nano never sees a tool and never
 * calls one, so the tool policy has nothing to lose by this route. Both run on the phone:
 * the question never leaves it either way.
 *
 * The child gate (Config.localAiBlocked) is checked first and wins over everything: a child's
 * phone gets no assistant from either model.
 */
final class NanoRoute {
    enum Engine { NANO, BONSAI, OFF }

    // ML Kit's FeatureStatus, mirrored so this class needs no ML Kit on the classpath
    static final int UNAVAILABLE = 0;
    static final int DOWNLOADABLE = 1;
    static final int DOWNLOADING = 2;
    static final int AVAILABLE = 3;

    // ML Kit's GenAiException.ErrorCode values this route reacts to
    static final int NOT_AVAILABLE = 8;
    static final int BUSY = 9;
    static final int REQUEST_TOO_LARGE = 12;
    static final int PER_APP_BATTERY_USE_QUOTA_EXCEEDED = 27;
    static final int BACKGROUND_USE_BLOCKED = 30;
    static final int NEEDS_SYSTEM_UPDATE = 604;

    /** Longest question Nano is asked: a short task. The Prompt API takes under 4000 tokens
     *  in all; the system text and the answer need their share of that. */
    static final int MAX_QUESTION = 1200;
    /** After AICore says busy or out of quota, Bonsai answers for this long. */
    static final long COOL_DOWN_MS = 10 * 60_000L;

    private NanoRoute() {}

    /**
     * @param blocked     Config.localAiBlocked(): "" or why this phone runs no assistant
     * @param preferNano  the owner's "Use Gemini Nano" switch
     * @param status      GeminiNano.status() (one of the FeatureStatus values above)
     * @param foreground  Ask Aither is on screen (AICore runs Nano for the top app only)
     * @param toolsTurn   this turn offers tools (a calendar question, page tools)
     * @param coolUntil   Nano answered busy/quota until this time (ms); 0 = never
     */
    static Engine pick(String blocked, boolean preferNano, int status, boolean foreground,
                       boolean toolsTurn, String question, long now, long coolUntil) {
        if (blocked == null || !blocked.isEmpty()) return Engine.OFF;
        if (!preferNano || status != AVAILABLE || !foreground || toolsTurn) return Engine.BONSAI;
        if (question == null || question.trim().isEmpty() || question.length() > MAX_QUESTION) return Engine.BONSAI;
        if (now < coolUntil) return Engine.BONSAI;
        return Engine.NANO;
    }

    /** Nano failed with this ML Kit error code: until when Bonsai should answer instead. */
    static long coolUntil(int errorCode, long now) {
        switch (errorCode) {
            case BUSY:
            case PER_APP_BATTERY_USE_QUOTA_EXCEEDED:
            case BACKGROUND_USE_BLOCKED:
                return now + COOL_DOWN_MS;
            case NOT_AVAILABLE:
            case NEEDS_SYSTEM_UPDATE:
                return now + 6 * COOL_DOWN_MS;
            default:
                return 0; // a one-off failure: this turn falls back, the next one may try again
        }
    }

    /** Offer the one-time download (from Google, through AICore; nothing of the owner's is sent). */
    static boolean offerDownload(String blocked, int status, boolean declined) {
        return blocked != null && blocked.isEmpty() && status == DOWNLOADABLE && !declined;
    }

    /** The one prompt Nano gets: Aither's short instructions, then the question. */
    static String prompt(String question) {
        return "You are Aither, the owner's assistant on their phone. Answer briefly, in plain "
                + "text. If the question needs the owner's calendar, files or anything you "
                + "cannot see, say so instead of guessing.\n\nQuestion: " + question.trim();
    }

    /** What the status reads as in Ask Aither. */
    static String describe(int status) {
        switch (status) {
            case AVAILABLE: return "Gemini Nano is ready on this phone";
            case DOWNLOADING: return "Gemini Nano is downloading";
            case DOWNLOADABLE: return "Gemini Nano can be downloaded to this phone";
            default: return "Gemini Nano is not available on this phone";
        }
    }
}
