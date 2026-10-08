package com.aitherium.aither;

/** Exit 0 (and print OK) when a share into Ask Aither becomes the draft the design says. */
public class ShareDraftCheck {
    static int bad = 0;

    static void eq(String what, String got, String want) {
        if (!want.equals(got)) {
            System.out.println("FAIL " + what + ": got [" + got + "], want [" + want + "]");
            bad++;
        }
    }

    public static void main(String[] a) {
        eq("body only", ShareDraft.of("", "  What is a mesh?  "), "What is a mesh?");
        eq("subject + body", ShareDraft.of("Sci-Fi Greeting", "Greetings, traveler of stars."),
                "Sci-Fi Greeting\n\nGreetings, traveler of stars.");
        eq("subject repeated in body", ShareDraft.of("Hello", "Hello there"), "Hello there");
        eq("subject only", ShareDraft.of("Just a title", ""), "Just a title");
        eq("nothing", ShareDraft.of(null, null), "");
        eq("blank", ShareDraft.of("  ", "\n\t"), "");
        StringBuilder big = new StringBuilder();
        while (big.length() < ShareDraft.MAX_CHARS + 500) big.append("lorem ");
        eq("capped", String.valueOf(ShareDraft.of("", big.toString()).length()),
                String.valueOf(ShareDraft.MAX_CHARS));
        if (bad > 0) System.exit(1);
        System.out.println("OK");
    }
}
