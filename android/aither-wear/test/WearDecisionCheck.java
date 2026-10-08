package com.aitherium.aither;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/** Exit 0 when the watch's decision rules (WearDecision) hold. */
public class WearDecisionCheck {
    static int bad = 0;

    static void eq(String what, Object got, Object want) {
        if (!String.valueOf(got).equals(String.valueOf(want))) {
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
            bad++;
        }
    }

    static WearDecision d(String id, String kind, String urgency, int nOpts) {
        String[][] o = new String[nOpts][];
        for (int i = 0; i < nOpts; i++) o[i] = new String[] {"k" + i, "Option " + i};
        return new WearDecision(id, "t", "", kind, urgency, "", o);
    }

    public static void main(String[] a) {
        eq("id ok", WearDecision.validId("d-zgst"), true);
        eq("id path", WearDecision.validId("../x"), false);
        eq("id query", WearDecision.validId("a?b=1"), false);
        eq("id empty", WearDecision.validId(""), false);

        eq("options on watch", d("a", "decision", "normal", 2).onWatch(), true);
        eq("credential to phone", d("a", "credential", "normal", 2).onWatch(), false);
        eq("free text to phone", d("a", "decision", "normal", 0).onWatch(), false);
        eq("too many options", d("a", "decision", "normal", 5).onWatch(), false);

        eq("passkey", WearDecision.outcome(403, "Allow"), "This one needs your passkey. Answer on your phone.");
        eq("ok", WearDecision.outcome(200, "Allow"), "Answered: Allow");
        eq("offline", WearDecision.outcome(0, "Allow"), "No connection. Try again.");
        eq("page", d("d-zgst", "decision", "", 1).page(), "https://app.aitherium.com/decide?id=d-zgst");

        List<WearDecision> all = new ArrayList<>(Arrays.asList(
                d("n1", "decision", "normal", 1), d("h1", "decision", "high", 1), d("n2", "decision", "", 1)));
        List<String> ids = new ArrayList<>();
        for (WearDecision x : WearDecision.order(all)) ids.add(x.id);
        eq("urgent first", ids, "[h1, n1, n2]");
        List<WearDecision> many = new ArrayList<>();
        for (int i = 0; i < 20; i++) many.add(d("x" + i, "decision", "", 1));
        eq("capped", WearDecision.order(many).size(), WearDecision.SHOWN);

        System.out.println(bad == 0 ? "OK" : bad + " failed");
        System.exit(bad == 0 ? 0 : 1);
    }
}
