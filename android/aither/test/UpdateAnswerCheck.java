package com.aitherium.aither;

/** Exit 0 when "Check for an update" always answers: ready, up to date, or why it could not tell. */
public class UpdateAnswerCheck {
    static int bad = 0;

    static void eq(String what, Object got, Object want) {
        if (!String.valueOf(got).equals(String.valueOf(want))) {
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
            bad++;
        }
    }

    public static void main(String[] a) {
        eq("ready", UpdateAnswer.of(true, "0.4.5", ""), "Version 0.4.5 is ready to install.");
        eq("up to date", UpdateAnswer.of(false, "0.4.4", "up to date (0.4.4)"), "Up to date (0.4.4).");
        eq("a reason", UpdateAnswer.of(false, "", "the release feed did not answer"), "The release feed did not answer.");
        eq("no state", UpdateAnswer.of(false, "", ""), UpdateAnswer.COULD_NOT);
        eq("null state", UpdateAnswer.of(false, null, null), UpdateAnswer.COULD_NOT);
        eq("already a sentence", UpdateAnswer.of(false, "", "nothing newer."), "Nothing newer.");
        if (bad > 0) System.exit(1);
        System.out.println("UpdateAnswerCheck ok");
    }
}
