package com.aitherium.aither;

import java.io.ByteArrayOutputStream;
import java.net.URI;
import java.nio.charset.StandardCharsets;

/**
 * How the phone and the watch listen: the order of the ears, which recognizer failures move
 * on to the next ear, when a recording is finished, what is sent to Aither's own speech
 * recognizer and what a person is told when it refuses. Pure Java, so test/HearCheck.java runs
 * it on a desktop JVM; the watch compiles it too (build.py WEAR_SHARED).
 *
 * The ears, best first (route):
 *   ON_DEVICE      Android's on-device recognizer (12+, Talk.canListen): audio stays here.
 *   SYSTEM         the phone's default recognizer service (SpeechRecognizer.createSpeechRecognizer).
 *   SYSTEM_ACTIVITY the system's voice-input screen (RecognizerIntent.ACTION_RECOGNIZE_SPEECH).
 *   SERVER         record a short clip (ServerEar) and ask Aither's recognizer,
 *                  POST /api/voice/hear, with the signed-in session.
 * SERVER is always there, so listening never quietly turns into "type instead": a refusal
 * (429 busy, 503 down) is said in words.
 */
final class Hear {
    private Hear() {}

    enum Route { ON_DEVICE, SYSTEM, SYSTEM_ACTIVITY, SERVER }

    static final String TRANSCRIBE_PATH = "/api/voice/hear";
    /** The server takes at most 60 s; stop a little before so the clip is never refused. */
    static final long MAX_CLIP_MS = 55_000L;

    /** The first ear this device has. */
    static Route route(int sdk, boolean onDevice, boolean systemService, boolean systemActivity) {
        if (Talk.canListen(sdk, onDevice)) return Route.ON_DEVICE;
        if (systemService) return Route.SYSTEM;
        if (systemActivity) return Route.SYSTEM_ACTIVITY;
        return Route.SERVER;
    }

    /** The ear after `failed`, given what this device has. SERVER is the last. */
    static Route next(Route failed, boolean systemService, boolean systemActivity) {
        switch (failed) {
            case ON_DEVICE:
                if (systemService) return Route.SYSTEM;
                // fall through
            case SYSTEM:
                if (systemActivity) return Route.SYSTEM_ACTIVITY;
                // fall through
            default:
                return Route.SERVER;
        }
    }

    /**
     * Does a recognizer error (SpeechRecognizer.ERROR_*) mean "this ear cannot hear here, try
     * the next one"? Silence and no-match are the person's answer, not the ear's failure;
     * permission and busy are said as they are.
     */
    static boolean tryNextEar(int error) {
        switch (error) {
            case 1:  // ERROR_NETWORK_TIMEOUT
            case 2:  // ERROR_NETWORK
            case 4:  // ERROR_SERVER
            case 10: // ERROR_TOO_MANY_REQUESTS
            case 11: // ERROR_SERVER_DISCONNECTED
            case 12: // ERROR_LANGUAGE_NOT_SUPPORTED
            case Talk.ERROR_LANGUAGE_UNAVAILABLE: // the pack is downloading: hear now another way
                return true;
            default:
                return false;
        }
    }

    // ---------------------------------------------------------------- recording end-pointing

    /** What ServerEar does after one microphone level sample. */
    enum Step { KEEP, DONE, SILENT }

    /** A recorder level (getMaxAmplitude, 0..32767) above this is speech. */
    static final int SPEECH_LEVEL = 1800;
    /** This long without speech after some speech: the person finished. */
    static final long END_SILENCE_MS = 1600L;
    /** This long with no speech at all: nobody spoke (the conversation's silence timeout). */
    static final long NO_SPEECH_MS = 7000L;

    /**
     * End-pointing for a recording, fed one level sample at a time. DONE = upload what was
     * said; SILENT = nobody spoke, upload nothing.
     */
    static final class Ear {
        private long spokeAt = -1;
        private boolean spoke;

        Step feed(long sinceStartMs, int level) {
            if (level >= SPEECH_LEVEL) {
                spoke = true;
                spokeAt = sinceStartMs;
            }
            if (sinceStartMs >= MAX_CLIP_MS) return spoke ? Step.DONE : Step.SILENT;
            if (!spoke) return sinceStartMs >= NO_SPEECH_MS ? Step.SILENT : Step.KEEP;
            return sinceStartMs - spokeAt >= END_SILENCE_MS ? Step.DONE : Step.KEEP;
        }

        boolean spoke() { return spoke; }
    }

    // ---------------------------------------------------------------- the upload

    /** A multipart/form-data body with one part, `audio`, holding the clip. */
    static byte[] multipart(String boundary, String filename, String mime, byte[] audio) {
        ByteArrayOutputStream o = new ByteArrayOutputStream(audio.length + 256);
        String head = "--" + boundary + "\r\n"
                + "Content-Disposition: form-data; name=\"audio\"; filename=\"" + filename + "\"\r\n"
                + "Content-Type: " + mime + "\r\n\r\n";
        o.write(head.getBytes(StandardCharsets.UTF_8), 0, head.length());
        o.write(audio, 0, audio.length);
        byte[] tail = ("\r\n--" + boundary + "--\r\n").getBytes(StandardCharsets.UTF_8);
        o.write(tail, 0, tail.length);
        return o.toByteArray();
    }

    static String contentType(String boundary) {
        return "multipart/form-data; boundary=" + boundary;
    }

    /**
     * What a person reads when Aither's recognizer did not answer with words (status 0 = no
     * connection). Never empty: a refusal is shown, not swallowed.
     */
    static String serverError(int status) {
        switch (status) {
            case 0:
                return "No connection, so Aither couldn't listen. Check the network and try again.";
            case 401:
            case 403:
                return "Sign in to Aither to talk to it.";
            case 413:
                return "That was too long to hear. Keep it under a minute.";
            case 415:
            case 422:
                return "Aither couldn't read that recording. Try again.";
            case 429:
                return "Aither is hearing a lot right now. Try again in a moment.";
            case 503:
                return "Aither's listening is down right now. Try again soon, or type instead.";
            default:
                return "Aither couldn't hear that (" + status + "). Try again, or type instead.";
        }
    }

    // ---------------------------------------------------------------- which pages hear the mic

    /**
     * May a page in the phone app's WebView get the microphone (getUserMedia)? Only an https
     * aitherium.com page: the apex, app., or any other *.aitherium.com. Everything else is
     * denied, whatever it asks.
     */
    static boolean pageMayHear(String origin) {
        if (origin == null || origin.isEmpty()) return false;
        try {
            URI u = new URI(origin);
            String h = u.getHost();
            if (!"https".equalsIgnoreCase(u.getScheme()) || h == null) return false;
            if (u.getUserInfo() != null) return false;
            if (u.getPort() != -1 && u.getPort() != 443) return false;
            h = h.toLowerCase(java.util.Locale.ROOT);
            return h.equals("aitherium.com") || h.endsWith(".aitherium.com");
        } catch (Exception e) {
            return false;
        }
    }
}
