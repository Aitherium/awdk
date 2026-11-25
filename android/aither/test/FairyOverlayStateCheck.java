package com.aitherium.aither;

/** Exit 0 when the floating fairy reads the SAME decision snapshot the web fairy does
 *  (open-card count, urgency, speaking) and never fakes a reading from a bad body. */
public class FairyOverlayStateCheck {
    static int bad = 0;

    static void eq(String what, Object got, Object want) {
        if (!String.valueOf(got).equals(String.valueOf(want))) {
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
            bad++;
        }
    }

    public static void main(String[] a) {
        FairyOverlayState zero = FairyOverlayState.empty();
        eq("empty count", zero.cards, 0);
        eq("empty label", zero.label(), "🧚");
        eq("empty not attention", zero.attention, false);
        eq("empty description", zero.description(), "Aither fairy: tap to open Aither, drag to move");

        String snap = "{\"decisions\":["
                + "{\"id\":\"c1\",\"status\":\"open\",\"urgency\":\"critical\",\"title\":\"Approve {x}\"},"
                + "{\"id\":\"c2\",\"status\":\"open\",\"urgency\":\"normal\"},"
                + "{\"id\":\"c3\",\"status\":\"answered\",\"urgency\":\"critical\"}"
                + "]}";
        FairyOverlayState two = FairyOverlayState.parse(snap, false);
        eq("two open", two.cards, 2);
        eq("critical needs you", two.attention, true);
        eq("label count", two.label(), "🧚 2");
        eq("description names needs you", two.description(),
                "Aither fairy: tap to open Aither, drag to move, 2 cards waiting, needs you");

        FairyOverlayState high = FairyOverlayState.parse(
                "{\"decisions\":[{\"status\":\"open\",\"urgency\":\"high\"}]}", false);
        eq("high needs you", high.attention, true);
        eq("high count", high.cards, 1);

        FairyOverlayState calm = FairyOverlayState.parse(
                "{\"decisions\":[{\"status\":\"open\",\"urgency\":\"normal\"},{\"status\":\"open\",\"urgency\":\"low\"}]}", false);
        eq("normal/low not attention", calm.attention, false);
        eq("normal/low count", calm.cards, 2);

        // a closed card alone is not a card; the fairy returns to plain
        FairyOverlayState closed = FairyOverlayState.parse(
                "{\"decisions\":[{\"status\":\"answered\"}]}", false);
        eq("closed count", closed.cards, 0);
        eq("closed label", closed.label(), "🧚");
        eq("closed not attention", closed.attention, false);

        // no status at all counts as open (the snapshot lists open cards)
        eq("no status open", FairyOverlayState.parse("{\"decisions\":[{\"id\":\"x\"}]}", false).cards, 1);

        // malformed / unreachable bodies never fake a reading
        eq("null body", FairyOverlayState.parse(null, false).cards, 0);
        eq("empty body", FairyOverlayState.parse("", false).cards, 0);
        eq("not json", FairyOverlayState.parse("<html>503</html>", false).cards, 0);
        eq("empty decisions", FairyOverlayState.parse("{\"decisions\":[]}", false).cards, 0);
        eq("null decisions", FairyOverlayState.parse("{\"decisions\":null}", false).cards, 0);

        // a brace inside a string does not split the card
        eq("brace in string", FairyOverlayState.parse(
                "{\"decisions\":[{\"id\":\"a}b\",\"status\":\"open\"}]}", false).cards, 1);

        // speaking rides the same state (AitherSay's playback), label unchanged
        FairyOverlayState saying = calm.withSpeaking(true);
        eq("speaking flag", saying.speaking, true);
        eq("speaking keeps count", saying.cards, 2);
        eq("speaking label", saying.label(), "🧚 2");
        eq("speaking description", saying.description(),
                "Aither fairy: tap to open Aither, drag to move, 2 cards waiting, speaking");
        eq("attention needs a card", new FairyOverlayState(0, true, false).attention, false);

        if (bad == 0) System.out.println("FairyOverlayStateCheck: OK");
        System.exit(bad == 0 ? 0 : 1);
    }
}
