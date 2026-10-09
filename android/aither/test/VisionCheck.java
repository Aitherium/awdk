package com.aitherium.aither;

import java.util.List;

/** Exit 0 when the picture route (this phone, the download offer, Aither online) and the
 *  words on the wire are what the design says. */
public class VisionCheck {
    static int bad = 0;

    static void eq(String what, Object got, Object want) {
        if (!String.valueOf(got).equals(String.valueOf(want))) {
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
            bad++;
        }
    }

    static final long GB = 1L << 30;
    static final long FREE = 4 * GB, TOTAL = 12 * GB, DISK = 32 * GB;

    static Vision.Route pick(String blocked, boolean engine, boolean installed, boolean declined,
                             long avail, long total, long disk, boolean hot, boolean signedIn) {
        return Vision.pick(blocked, engine, installed, declined, avail, total, disk, hot, signedIn);
    }

    public static void main(String[] a) {
        // installed and the phone can run it: on this phone, signed in or not
        eq("installed", pick("", true, true, false, FREE, TOTAL, DISK, false, true), Vision.Route.LOCAL);
        eq("installed, offline", pick("", true, true, false, FREE, TOTAL, DISK, false, false), Vision.Route.LOCAL);
        // not installed: offered once, and only on a phone that can keep and run it
        eq("offer", pick("", true, false, false, FREE, TOTAL, DISK, false, true), Vision.Route.OFFER);
        eq("declined", pick("", true, false, true, FREE, TOTAL, DISK, false, true), Vision.Route.HOSTED);
        eq("small phone", pick("", true, false, false, FREE, 4 * GB, DISK, false, true), Vision.Route.HOSTED);
        eq("full disk", pick("", true, false, false, FREE, TOTAL, 600L << 20, false, true), Vision.Route.HOSTED);
        eq("no engine in this build", pick("", false, false, false, FREE, TOTAL, DISK, false, true), Vision.Route.HOSTED);
        // the vouching gate: never on a child's or an unvouched phone; null fails closed
        eq("child's phone", pick("not on a child's phone", true, true, false, FREE, TOTAL, DISK, false, true), Vision.Route.HOSTED);
        eq("child's phone, no offer", pick("not on a child's phone", true, false, false, FREE, TOTAL, DISK, false, true), Vision.Route.HOSTED);
        eq("null gate", pick(null, true, true, false, FREE, TOTAL, DISK, false, true), Vision.Route.HOSTED);
        // installed but not runnable right now: hot, or too little free memory
        eq("hot", pick("", true, true, false, FREE, TOTAL, DISK, true, true), Vision.Route.HOSTED);
        eq("low memory", pick("", true, true, false, 900L << 20, TOTAL, DISK, false, true), Vision.Route.HOSTED);
        eq("low memory, signed out", pick("", true, true, false, 900L << 20, TOTAL, DISK, false, false), Vision.Route.NONE);
        eq("nothing", pick("not on a child's phone", true, false, false, FREE, TOTAL, DISK, false, false), Vision.Route.NONE);
        // the reasons the screen shows
        eq("why child", Vision.whyNotLocal("not on a child's phone", true, true, FREE, TOTAL, DISK, false), "not on a child's phone");
        eq("why ok", Vision.whyNotLocal("", true, true, FREE, TOTAL, DISK, false), "");
        eq("why hot", Vision.whyNotLocal("", true, true, FREE, TOTAL, DISK, true), "the phone is hot");
        eq("why not downloaded", Vision.whyNotLocal("", true, false, FREE, TOTAL, DISK, false), "the picture model is not downloaded");
        // the pinned download: two files, never bundled, 546 MB
        eq("size", Vision.DOWNLOAD_BYTES, 545590272L);
        eq("size label", Vision.sizeMb(), "546 MB");
        eq("pinned revision", Vision.BASE.contains("/resolve/" + Vision.REVISION + "/"), true);
        eq("sha model", Vision.MODEL_SHA256.matches("[0-9a-f]{64}"), true);
        eq("sha proj", Vision.PROJ_SHA256.matches("[0-9a-f]{64}"), true);
        eq("licence", Vision.LICENSE, "Apache-2.0");
        // words
        eq("bare picture asks the default", Vision.question("  "), Vision.DEFAULT_QUESTION);
        eq("own words", Vision.question(" what breed? "), "what breed?");
        int[] s = Vision.scaled(4000, 3000, 1024);
        eq("scaled w", s[0], 1024);
        eq("scaled h", s[1], 768);
        int[] small = Vision.scaled(640, 480, 1024);
        eq("small kept", small[0] + "x" + small[1], "640x480");
        eq("portrait", Vision.scaled(3000, 4000, 1024)[1], 1024);
        String body = Vision.chatBody("say \"hi\"\n", "QUJD", 300);
        eq("image part", body.contains("{\"type\":\"image_url\",\"image_url\":{\"url\":\"data:image/jpeg;base64,QUJD\"}}"), true);
        eq("escaped question", body.contains("\"text\":\"say \\\"hi\\\"\"}"), true);
        eq("model", body.startsWith("{\"model\":\"smolvlm-500m\""), true);
        // the hosted answer arrives as server-sent events
        List<String[]> ev = Vision.sse("event: session_start\ndata: {\"type\":\"session_start\"}\n\n"
                + ": heartbeat\n\nevent: answer\r\ndata: {\"type\":\"answer\",\r\ndata: \"answer\":\"a dog\"}\r\n\r\n"
                + "event: complete\ndata: {\"type\":\"complete\"}");
        eq("events", ev.size(), 3);
        eq("answer event", ev.get(1)[0], "answer");
        eq("multi-line data", ev.get(1)[1], "{\"type\":\"answer\",\n\"answer\":\"a dog\"}");
        eq("unterminated last", ev.get(2)[0], "complete");
        if (bad > 0) {
            System.out.println(bad + " check(s) failed");
            System.exit(1);
        }
        System.out.println("VisionCheck: ok");
    }
}
