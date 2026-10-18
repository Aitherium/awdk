package com.aitherium.aither;

import java.util.ArrayList;
import java.util.List;

/** Exit 0 when the calendar push carries only bounded event fields and the Connected line reads right. */
public class HearthSourcesCheck {
    static int bad = 0;

    static void eq(String what, Object got, Object want) {
        if (!String.valueOf(got).equals(String.valueOf(want))) {
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
            bad++;
        }
    }

    public static void main(String[] a) {
        List<HearthSources.Event> ev = new ArrayList<>();
        ev.add(new HearthSources.Event(1000, 2000, false, "Dentist \"Dr\" \\ x\n", "Main St"));
        ev.add(new HearthSources.Event(5000, 4000, true, null, " "));
        eq("body", HearthSources.body(ev, "Pixel", -420),
                "{\"device\":\"Pixel\",\"tz_offset_min\":-420,\"events\":["
                        + "{\"start\":1000,\"end\":2000,\"all_day\":false,\"title\":\"Dentist \\\"Dr\\\" \\\\ x\",\"location\":\"Main St\"},"
                        + "{\"start\":5000,\"end\":5000,\"all_day\":true,\"title\":\"\"}]}");
        // bounded: never more than MAX_EVENTS, titles clipped
        List<HearthSources.Event> many = new ArrayList<>();
        for (int i = 0; i < 400; i++) many.add(new HearthSources.Event(i, i, false, "t".repeat(500), null));
        String big = HearthSources.body(many, "", 0);
        eq("event cap", big.split("\"start\"").length - 1, HearthSources.MAX_EVENTS);
        eq("title cap", big.contains("t".repeat(201)), false);
        eq("line both", HearthSources.line("phone", "david@aitherium.com"),
                "Calendar: phone ✓ · Email: david@aitherium.com ✓");
        eq("line none", HearthSources.line("", null), "Calendar: not connected · Email: not connected");
        eq("line feed", HearthSources.line("feed", ""), "Calendar: connected ✓ · Email: not connected");
        if (bad > 0) System.exit(1);
        System.out.println("OK");
    }
}
