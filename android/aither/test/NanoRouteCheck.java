package com.aitherium.aither;

/** Exit 0 when Ask Aither's model choice (Gemini Nano or Bonsai) is what the design says. */
public class NanoRouteCheck {
    static int bad = 0;

    static void eq(String what, Object got, Object want) {
        if (!String.valueOf(got).equals(String.valueOf(want))) {
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
            bad++;
        }
    }

    static NanoRoute.Engine pick(String blocked, boolean prefer, int status, boolean fg, boolean tools, String q, long cool) {
        return NanoRoute.pick(blocked, prefer, status, fg, tools, q, 1_000_000L, cool);
    }

    public static void main(String[] a) {
        int ok = NanoRoute.AVAILABLE;
        String q = "Write a two-line birthday note for Sam";
        eq("ready, short, no tools", pick("", true, ok, true, false, q, 0), NanoRoute.Engine.NANO);
        // the child gate wins over everything, Nano included
        eq("child's phone", pick("not on a child's phone", true, ok, true, false, q, 0), NanoRoute.Engine.OFF);
        eq("unvouched phone", pick("connect this phone to your workspace first", true, ok, true, false, q, 0), NanoRoute.Engine.OFF);
        eq("null gate fails closed", pick(null, true, ok, true, false, q, 0), NanoRoute.Engine.OFF);
        // tools stay with Bonsai: Nano is never offered calendar_read or a page tool
        eq("tools turn", pick("", true, ok, true, true, q, 0), NanoRoute.Engine.BONSAI);
        // Nano only when it is there, on screen, wanted and the question is short
        eq("owner switched it off", pick("", false, ok, true, false, q, 0), NanoRoute.Engine.BONSAI);
        eq("downloadable", pick("", true, NanoRoute.DOWNLOADABLE, true, false, q, 0), NanoRoute.Engine.BONSAI);
        eq("downloading", pick("", true, NanoRoute.DOWNLOADING, true, false, q, 0), NanoRoute.Engine.BONSAI);
        eq("unavailable", pick("", true, NanoRoute.UNAVAILABLE, true, false, q, 0), NanoRoute.Engine.BONSAI);
        eq("background", pick("", true, ok, false, false, q, 0), NanoRoute.Engine.BONSAI);
        StringBuilder longQ = new StringBuilder();
        while (longQ.length() <= NanoRoute.MAX_QUESTION) longQ.append("word ");
        eq("long question", pick("", true, ok, true, false, longQ.toString(), 0), NanoRoute.Engine.BONSAI);
        eq("empty question", pick("", true, ok, true, false, "  ", 0), NanoRoute.Engine.BONSAI);
        // busy / quota / background: Bonsai for the cool-down, then Nano again
        long now = 1_000_000L;
        long cool = NanoRoute.coolUntil(NanoRoute.BUSY, now);
        eq("busy cools down", cool, now + NanoRoute.COOL_DOWN_MS);
        eq("quota cools down", NanoRoute.coolUntil(NanoRoute.PER_APP_BATTERY_USE_QUOTA_EXCEEDED, now) > now, true);
        eq("background cools down", NanoRoute.coolUntil(NanoRoute.BACKGROUND_USE_BLOCKED, now) > now, true);
        eq("gone cools longer", NanoRoute.coolUntil(NanoRoute.NOT_AVAILABLE, now) > cool, true);
        eq("one-off failure", NanoRoute.coolUntil(NanoRoute.REQUEST_TOO_LARGE, now), 0);
        eq("in cool-down", pick("", true, ok, true, false, q, cool), NanoRoute.Engine.BONSAI);
        eq("after cool-down", NanoRoute.pick("", true, ok, true, false, q, cool, cool), NanoRoute.Engine.NANO);
        // the download offer: once, never on a blocked phone
        eq("offer", NanoRoute.offerDownload("", NanoRoute.DOWNLOADABLE, false), true);
        eq("declined", NanoRoute.offerDownload("", NanoRoute.DOWNLOADABLE, true), false);
        eq("offer on child's phone", NanoRoute.offerDownload("not on a child's phone", NanoRoute.DOWNLOADABLE, false), false);
        eq("offer when ready", NanoRoute.offerDownload("", ok, false), false);
        eq("prompt carries question", NanoRoute.prompt("  hi  ").endsWith("Question: hi"), true);
        if (bad > 0) {
            System.out.println(bad + " check(s) failed");
            System.exit(1);
        }
        System.out.println("NanoRouteCheck: ok");
    }
}
