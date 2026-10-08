package com.aitherium.aither;

import java.nio.charset.StandardCharsets;

/** Exit 0 when the ears (Hear) fall back in order, end-point a clip and gate the WebView mic. */
public class HearCheck {
    static int bad = 0;

    static void eq(String what, Object got, Object want) {
        if (!String.valueOf(got).equals(String.valueOf(want))) {
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
            bad++;
        }
    }

    public static void main(String[] a) {
        // the first ear: on-device when the phone has it, else Aither's recognizer
        eq("android 12 on-device", Hear.route(31, true, false, false), Hear.Route.ON_DEVICE);
        eq("no on-device engine", Hear.route(36, false, false, false), Hear.Route.SERVER);
        eq("android 10", Hear.route(29, true, false, false), Hear.Route.SERVER);
        eq("system before server", Hear.route(36, false, true, false), Hear.Route.SYSTEM);

        // after a failed ear
        eq("on-device -> server", Hear.next(Hear.Route.ON_DEVICE, false, false), Hear.Route.SERVER);
        eq("on-device -> system", Hear.next(Hear.Route.ON_DEVICE, true, false), Hear.Route.SYSTEM);
        eq("server is last", Hear.next(Hear.Route.SERVER, true, true), Hear.Route.SERVER);

        // which recognizer errors move on, and which are the person's own answer
        eq("network moves on", Hear.tryNextEar(2), true);
        eq("language pack moves on", Hear.tryNextEar(Talk.ERROR_LANGUAGE_UNAVAILABLE), true);
        eq("no match is an answer", Hear.tryNextEar(7), false);
        eq("speech timeout is an answer", Hear.tryNextEar(6), false);
        eq("no permission is said", Hear.tryNextEar(9), false);

        // end-pointing
        Hear.Ear quiet = new Hear.Ear();
        eq("quiet keeps listening", quiet.feed(3000, 100), Hear.Step.KEEP);
        eq("nobody spoke", quiet.feed(Hear.NO_SPEECH_MS, 100), Hear.Step.SILENT);
        Hear.Ear talk = new Hear.Ear();
        eq("speech starts", talk.feed(500, 5000), Hear.Step.KEEP);
        eq("short pause keeps", talk.feed(1500, 200), Hear.Step.KEEP);
        eq("long pause ends", talk.feed(500 + Hear.END_SILENCE_MS, 200), Hear.Step.DONE);
        Hear.Ear long_ = new Hear.Ear();
        long_.feed(100, 9000);
        eq("cap ends a long clip", long_.feed(Hear.MAX_CLIP_MS, 9000), Hear.Step.DONE);
        eq("cap under the server's 60 s", Hear.MAX_CLIP_MS < 60_000L, true);

        // the upload body
        byte[] body = Hear.multipart("B", "clip.m4a", "audio/mp4", new byte[] {1, 2, 3});
        String text = new String(body, StandardCharsets.ISO_8859_1);
        eq("part name", text.contains("name=\"audio\"; filename=\"clip.m4a\""), true);
        eq("part type", text.contains("Content-Type: audio/mp4\r\n\r\n"), true);
        eq("closing boundary", text.endsWith("\r\n--B--\r\n"), true);
        eq("content type", Hear.contentType("B"), "multipart/form-data; boundary=B");
        eq("path", Hear.TRANSCRIBE_PATH, "/api/voice/hear");

        // a refusal is always said in words
        for (int code : new int[] {0, 401, 413, 415, 429, 503, 500}) {
            eq("message for " + code, Hear.serverError(code).isEmpty(), false);
        }

        // only an https aitherium.com page may have the mic in the WebView
        eq("app host", Hear.pageMayHear("https://app.aitherium.com/"), true);
        eq("apex", Hear.pageMayHear("https://aitherium.com"), true);
        eq("other subdomain", Hear.pageMayHear("https://learn.aitherium.com"), true);
        eq("plain http", Hear.pageMayHear("http://app.aitherium.com"), false);
        eq("lookalike", Hear.pageMayHear("https://aitherium.com.evil.example"), false);
        eq("suffix trick", Hear.pageMayHear("https://evilaitherium.com"), false);
        eq("userinfo trick", Hear.pageMayHear("https://aitherium.com@evil.example"), false);
        eq("odd port", Hear.pageMayHear("https://app.aitherium.com:8443"), false);
        eq("empty", Hear.pageMayHear(""), false);
        eq("null", Hear.pageMayHear(null), false);

        System.out.println(bad == 0 ? "OK" : bad + " failed");
        System.exit(bad == 0 ? 0 : 1);
    }
}
