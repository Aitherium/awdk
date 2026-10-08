package com.aitherium.aither;

/** Exit 0 when an approval notification's buttons can only send the card they were posted with. */
public class ApprovalCardCheck {
    static int bad = 0;

    static void eq(String what, Object got, Object want) {
        if (!String.valueOf(got).equals(String.valueOf(want))) {
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
            bad++;
        }
    }

    public static void main(String[] a) {
        String id = "0123456789abcdef";
        String dig = "ab".repeat(32);
        ApprovalCard card = new ApprovalCard(id, "approval", "Hearth needs your OK",
                "send an email to Sam", true, false, dig.toUpperCase(), "pending", 0, 2, "");

        // the decide payload carries exactly id + digest + choice, nothing else
        eq("approve body", ApprovalCard.decideBody(id, dig, true),
                "{\"notice_id\":\"" + id + "\",\"digest\":\"" + dig + "\",\"allow\":true}");
        eq("deny body", ApprovalCard.decideBody(id, dig, false),
                "{\"notice_id\":\"" + id + "\",\"digest\":\"" + dig + "\",\"allow\":false}");
        // malformed or injected extras never leave the phone
        eq("short id", ApprovalCard.decideBody("0123", dig, true), null);
        eq("injected id", ApprovalCard.decideBody(id + "\",\"x\":\"", dig, true), null);
        eq("short digest", ApprovalCard.decideBody(id, "abcd", true), null);
        eq("null digest", ApprovalCard.decideBody(id, null, true), null);
        eq("injected digest", ApprovalCard.decideBody(id, dig.substring(2) + "\"}", true), null);

        // a button's extras must still describe this card
        eq("matches self", card.matches(id, dig), true);
        eq("matches upper", card.matches(id, dig.toUpperCase()), true);
        eq("other notice", card.matches("fedcba9876543210", dig), false);
        eq("changed card", card.matches(id, "cd".repeat(32)), false);
        eq("no digest", card.matches(id, null), false);

        // buttons only on an open card this account has not answered
        eq("answerable", card.answerable(), true);
        eq("answered", new ApprovalCard(id, "approval", "", "", true, false, dig, "pending", 1, 2, "yes").answerable(), false);
        eq("settled", new ApprovalCard(id, "approval", "", "", true, false, dig, "approved", 1, 1, "").answerable(), false);
        eq("note has no buttons", new ApprovalCard(id, "notice", "", "", false, false, "", "", 0, 0, "").answerable(), false);
        eq("bad digest is not an approval", new ApprovalCard(id, "approval", "", "", true, false, "zz", "pending", 0, 1, "").answerable(), false);

        // quorum on the card
        eq("1 of 2", ApprovalCard.statusLine("pending", 1, 2, "yes"), "Approved by you · 1 of 2");
        eq("waiting", ApprovalCard.statusLine("pending", 1, 2, ""), "Waiting · 1 of 2");
        eq("approved", ApprovalCard.statusLine("approved", 2, 2, "yes"), "Approved · 2 of 2");
        eq("ran", ApprovalCard.statusLine("done", 2, 2, "yes", "", "6:41 pm"), "Approved · 2 of 2 · ran 6:41 pm");
        eq("ran no time", ApprovalCard.statusLine("done", 1, 1, "yes"), "Approved · 1 of 1");
        eq("denied by you", ApprovalCard.statusLine("denied", 0, 2, "no"), "Denied by you");
        eq("denied by name", ApprovalCard.statusLine("denied", 1, 2, "yes", "Sam", ""), "Denied by Sam");
        eq("denied", ApprovalCard.statusLine("denied", 1, 2, ""), "Denied");
        eq("my no, 1-of-2 still open", ApprovalCard.statusLine("pending", 0, 1, "no"), "Denied by you · 0 of 1");
        eq("expired", ApprovalCard.statusLine("expired", 0, 2, ""), "Expired");
        eq("failed", ApprovalCard.statusLine("failed", 1, 1, "yes"), "Approved · 1 of 1 · did not run");
        // 18:41:00 UTC -> "6:41 pm"; midnight -> 12
        eq("clock pm", ApprovalCard.clockText(1791484860, java.time.ZoneOffset.UTC), "6:41 pm");
        eq("clock 12am", ApprovalCard.clockText(1791417600, java.time.ZoneOffset.UTC), "12:00 am");
        ApprovalCard ran = new ApprovalCard(id, "approval", "", "", true, false, dig, "done", 2, 2, "yes", "", 1791484860);
        eq("settled replaces quietly", ran.settled() && !ran.answerable(), true);
        eq("card line has a time", ran.statusLine().startsWith("Approved · 2 of 2 · ran "), true);

        // channels: approvals never wait for quiet hours
        eq("approval channel", card.channel(), "approvals");
        eq("quiet note", new ApprovalCard(id, "notice", "", "", false, true, "", "", 0, 0, "").channel(), "household_quiet");
        eq("note", new ApprovalCard(id, "notice", "", "", false, false, "", "", 0, 0, "").channel(), "household");

        // the two buttons of one card never share a PendingIntent; the card keeps its id
        eq("distinct codes", card.requestCode(true) != card.requestCode(false), true);
        eq("stable id", card.notifyId(), new ApprovalCard(id, "approval", "x", "y", true, false, dig, "denied", 0, 1, "no").notifyId());

        // tapping opens the notice's own same-origin link; never another origin
        eq("decide link", ApprovalCard.openPath("/decide?id=d-1", id), "/decide?id=d-1");
        eq("no link", ApprovalCard.openPath("", id), "/hearth/?notice=" + id);
        eq("hearth link", ApprovalCard.openPath("/hearth/#family", id), "/hearth/?notice=" + id);
        eq("other origin", ApprovalCard.openPath("//evil.example/", id), "/hearth/?notice=" + id);
        eq("absolute url", ApprovalCard.openPath("https://evil.example/", id), "/hearth/?notice=" + id);
        eq("space", ApprovalCard.openPath("/a b", id), "/hearth/?notice=" + id);

        // refusals in words
        eq("401", ApprovalCard.refusal(401, ""), "Sign in to Aither to answer. Tap to open it.");
        eq("changed", ApprovalCard.refusal(409, "card_changed"), "This card changed. Tap to see it in Aither.");
        eq("again", ApprovalCard.refusal(409, "already_voted"), "Already answered.");
        eq("offline", ApprovalCard.refusal(0, ""), "Not sent: no connection. Tap to answer in Aither.");

        if (bad > 0) {
            System.out.println(bad + " check(s) failed");
            System.exit(1);
        }
        System.out.println("ApprovalCard OK");
    }
}
