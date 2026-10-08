package com.aitherium.aither;

import java.util.Arrays;
import java.util.Collections;

/** Exit 0 when Ask Aither's voice rules (Talk) listen on-device only and read answers cleanly. */
public class TalkCheck {
    static int bad = 0;

    static void eq(String what, Object got, Object want) {
        if (!String.valueOf(got).equals(String.valueOf(want))) {
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
            bad++;
        }
    }

    public static void main(String[] a) {
        // listening: only the on-device recognizer, which Android has from 12 (API 31)
        eq("android 12 on-device", Talk.canListen(31, true), true);
        eq("android 16 on-device", Talk.canListen(36, true), true);
        eq("no on-device engine", Talk.canListen(36, false), false);
        eq("android 11", Talk.canListen(30, true), false);
        eq("android 10", Talk.canListen(29, true), false);

        // what was heard
        eq("best guess", Talk.heard(Arrays.asList("what's on my calendar", "what's on my calender")),
                "what's on my calendar");
        eq("blank first", Talk.heard(Arrays.asList("  ", null, " call  mom ")), "call mom");
        eq("nothing", Talk.heard(Collections.emptyList()), "");
        eq("null", Talk.heard(null), "");

        // how an answer sounds
        eq("plain", Talk.spoken("You have two meetings."), "You have two meetings.");
        eq("bold", Talk.spoken("You have **two** meetings."), "You have two meetings.");
        eq("italic", Talk.spoken("It is _really_ late."), "It is really late.");
        eq("inner marks stay", Talk.spoken("2*3 is six, see snake_case."), "2*3 is six, see snake_case.");
        eq("list", Talk.spoken("Today:\n- standup\n* lunch\n1. dentist"), "Today: standup lunch dentist");
        eq("heading", Talk.spoken("## Plan\nRest."), "Plan Rest.");
        eq("md link", Talk.spoken("See [the guide](https://aitherium.com/docs)."), "See the guide.");
        eq("bare url", Talk.spoken("Open https://aitherium.com/x?y=1 now."), "Open a link now.");
        eq("url ends a sentence", Talk.spoken("Open https://aitherium.com/x. Then sign in."), "Open a link. Then sign in.");
        eq("url in brackets", Talk.spoken("The page (https://aitherium.com/x) has it."), "The page (a link) has it.");
        eq("code block", Talk.spoken("Run:\n```\nrm -rf /tmp/x\n```\nDone."),
                "Run: (the code is on screen) Done.");
        eq("open fence", Talk.spoken("Run:\n```\nls"), "Run: (the code is on screen)");
        eq("inline code", Talk.spoken("Use `adb devices`."), "Use adb devices.");
        eq("null answer", Talk.spoken(null), "");

        // a long answer stops at a whole sentence, under the TTS limit
        StringBuilder lng = new StringBuilder();
        while (lng.length() < 5000) lng.append("This is one sentence. ");
        String s = Talk.spoken(lng.toString());
        eq("long fits", s.length() <= 4000, true);
        eq("long ends whole", s.endsWith("sentence. The rest is on screen."), true);
        StringBuilder words = new StringBuilder();
        while (words.length() < 5000) words.append("word ");
        String w = Talk.spoken(words.toString());
        eq("no sentence fits", w.length() <= 4000, true);
        eq("no sentence cut at a word", w.endsWith("word… the rest is on screen."), true);

        // recognizer errors read as words, never a bare code for the common ones
        for (int code : new int[] {3, 6, 7, 8, 9, 12, Talk.ERROR_LANGUAGE_UNAVAILABLE}) {
            if (Talk.errorText(code).contains("(" + code + ")")) {
                System.out.println("FAIL error " + code + " has no words");
                bad++;
            }
        }
        eq("other error", Talk.errorText(5).contains("(5)"), true);

        System.out.println(bad == 0 ? "OK" : bad + " FAILED");
        System.exit(bad == 0 ? 0 : 1);
    }
}
