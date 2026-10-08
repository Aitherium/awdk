package com.aitherium.aither;

import java.util.ArrayList;
import java.util.List;

/** Exit 0 when a streamed answer (WearFeed) is cut into speakable sentences as it arrives. */
public class WearFeedCheck {
    static int bad = 0;

    static void eq(String what, Object got, Object want) {
        if (!String.valueOf(got).equals(String.valueOf(want))) {
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
            bad++;
        }
    }

    static List<String> say(List<WearFeed.Sentence> s) {
        List<String> out = new ArrayList<>();
        for (WearFeed.Sentence x : s) out.add(x.say);
        return out;
    }

    public static void main(String[] a) {
        // the measured stream: a sentence is handed out the moment its end arrives
        WearFeed f = new WearFeed();
        f.segment("initial");
        eq("partial", say(f.token("The tallest mountain on Earth")), "[]");
        eq("no end yet", say(f.token(" is Mount Everest")), "[]");
        eq("end waits for the space", say(f.token(".")), "[]");
        eq("first sentence", say(f.token(" It")), "[The tallest mountain on Earth is Mount Everest.]");
        eq("decimal is not an end", say(f.token(" stands at 8.8 km")), "[]");
        eq("rest on segment_end", say(f.flush()), "[It stands at 8.8 km]");
        eq("shown", f.text(), "The tallest mountain on Earth is Mount Everest. It stands at 8.8 km");

        // offsets point at the sentence on screen (for the spoken-word highlight)
        WearFeed g = new WearFeed();
        g.token("Hello there. ");
        List<WearFeed.Sentence> s = g.token("How are you? ");
        eq("one sentence", s.size(), 1);
        eq("offsets", g.text().substring(s.get(0).start, s.get(0).end), "How are you?");

        // the terminal answer saying the same thing changes nothing and repeats nothing
        eq("same last", say(g.last("Hello there. How are you?")), "[]");
        eq("same gen", g.gen(), 0);

        // a refinement replaces the text; what was already said is not said again
        WearFeed r = new WearFeed();
        r.segment("initial");
        r.token("It is sunny. ");
        r.token("Highs near 70.");
        r.flush();
        r.segment("refinement");
        eq("replaced", r.gen(), 1);
        r.token("It is sunny. ");
        eq("new sentence only", say(r.token("Rain after 6 pm. ")), "[Rain after 6 pm.]");
        eq("refined text", r.text(), "It is sunny. Rain after 6 pm. ");

        // a continuation is appended after a break
        WearFeed c = new WearFeed();
        c.segment("initial");
        c.token("Checking now.");
        eq("flush before continuation", say(c.segment("continuation")), "[Checking now.]");
        c.token("Done.");
        eq("appended", c.text(), "Checking now.\n\nDone.");

        // eager: the terminal answer is only the last segment, it never replaces the stream
        WearFeed e = new WearFeed();
        e.segment("initial");
        e.token("Let me check. ");
        e.flush();
        e.segment("continuation");
        e.token("Everything is up.");
        e.flush();
        eq("eager answer ignored", say(e.last("Everything is up.")), "[]");
        eq("both segments kept", e.text().replace("\n", "|"), "Let me check. ||Everything is up.");

        // a server without eager streaming: only the terminal answer
        WearFeed t = new WearFeed();
        eq("terminal only", say(t.last("One. Two.")), "[One., Two.]");

        // abbreviations do not split, a long first sentence stops at a clause
        WearFeed ab = new WearFeed();
        eq("Mt.", say(ab.token("Mt. Everest is tall. ")), "[Mt. Everest is tall.]");
        WearFeed cl = new WearFeed();
        eq("first clause", say(cl.token("The tallest mountain on Earth, by height above sea level, is ")),
                "[The tallest mountain on Earth,]");
        eq("later clause does not split", say(cl.token("Everest, in Nepal. ")), "[by height above sea level, is Everest, in Nepal.]");

        // markdown is not read out, code is shown but not read
        WearFeed md = new WearFeed();
        eq("stars", say(md.token("**Yes.** ")), "[Yes.]");
        WearFeed code = new WearFeed();
        code.token("Run this:\n");
        eq("fence", say(code.token("```bash\n")), "[The code is on screen.]");
        eq("in fence", say(code.token("ls -la.\n")), "[]");
        eq("fence close", say(code.token("```\n")), "[]");
        eq("after fence", say(code.token("Then check. ")), "[Then check.]");

        System.out.println(bad == 0 ? "OK" : bad + " failed");
        System.exit(bad == 0 ? 0 : 1);
    }
}
