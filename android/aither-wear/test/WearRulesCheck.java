package com.aitherium.aither;

import java.util.Arrays;
import java.util.List;
import java.util.Map;

/** Exit 0 when the watch's rules (WearRules) list, bind, guard and sign in as the phone does. */
public class WearRulesCheck {
    static int bad = 0;

    static void eq(String what, Object got, Object want) {
        if (!String.valueOf(got).equals(String.valueOf(want))) {
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
            bad++;
        }
    }

    static final String D1 = "a".repeat(64);
    static final String D2 = "b".repeat(64);

    static ApprovalCard approval(String id, String digest, String state, String mine, boolean urgent) {
        return new ApprovalCard(id, "approval", "Unlock the front door", "Sam asked", urgent, false,
                digest, state, 0, 2, mine);
    }

    public static void main(String[] a) {
        // the inbox: pages in change order, a later view of a card replaces the earlier one
        Map<String, ApprovalCard> byId = WearRules.byId();
        WearRules.merge(byId, Arrays.asList(
                approval("0000000000000001", D1, "pending", "", false),
                approval("0000000000000002", D2, "pending", "", false),
                null,
                approval("not-an-id", D1, "pending", "", false),
                new ApprovalCard("0000000000000003", "family_note", "Hi", "", false, false, "", "", 0, 0, "")));
        eq("kept by id", byId.size(), 3);
        WearRules.merge(byId, Arrays.asList(approval("0000000000000001", D1, "pending", "yes", false)));
        eq("replaced, not added", byId.size(), 3);

        List<ApprovalCard> w = WearRules.waiting(byId);
        eq("only what I can still answer", w.size(), 1);
        eq("the open one", w.get(0).noticeId, "0000000000000002");

        WearRules.merge(byId, Arrays.asList(
                approval("0000000000000004", D1, "pending", "", true),
                approval("0000000000000005", D1, "pending", "", false),
                approval("0000000000000006", D1, "expired", "", true),
                approval("0000000000000007", D1, "approved", "", false)));
        w = WearRules.waiting(byId);
        eq("waiting count", w.size(), 3);
        eq("urgent first", w.get(0).noticeId, "0000000000000004");
        eq("then newest", w.get(1).noticeId, "0000000000000005");
        eq("then older", w.get(2).noticeId, "0000000000000002");

        eq("count 0", WearRules.countLine(0), "Nothing is waiting for you");
        eq("count 1", WearRules.countLine(1), "1 thing is waiting for you");
        eq("count 3", WearRules.countLine(3), "3 things are waiting for you");

        eq("full page reads on", WearRules.more(10, 1), true);
        eq("short page stops", WearRules.more(4, 1), false);
        eq("page budget", WearRules.more(10, WearRules.MAX_PAGES), false);

        // the answer: exactly the shown card's id + digest, and only when it can be answered
        ApprovalCard open = approval("00000000000000ab", D2.toUpperCase(), "pending", "", false);
        eq("approve body", WearRules.answer(open, true),
                "{\"notice_id\":\"00000000000000ab\",\"digest\":\"" + D2 + "\",\"allow\":true}");
        eq("deny body", WearRules.answer(open, false),
                "{\"notice_id\":\"00000000000000ab\",\"digest\":\"" + D2 + "\",\"allow\":false}");
        eq("same as the phone", WearRules.answer(open, true), ApprovalCard.decideBody("00000000000000ab", D2, true));
        eq("answered already", WearRules.answer(approval("00000000000000ab", D2, "pending", "no", false), true), null);
        eq("settled", WearRules.answer(approval("00000000000000ab", D2, "approved", "", false), true), null);
        eq("bad digest", WearRules.answer(approval("00000000000000ab", "abc", "pending", "", false), true), null);
        eq("not an approval", WearRules.answer(new ApprovalCard("00000000000000ab", "info", "t", "b",
                false, false, D2, "pending", 0, 1, ""), true), null);
        eq("no card", WearRules.answer(null, true), null);

        // the guard: only a watch that can lock (so it knows it left the wrist), and is unlocked
        eq("secure + unlocked", WearRules.guard(true, false), null);
        eq("locked", WearRules.guard(true, true), "Unlock your watch to answer.");
        eq("no screen lock", WearRules.guard(false, false) != null, true);
        eq("no screen lock, locked", WearRules.guard(false, true) != null, true);

        // sign-in (Identity: pending is 200, terminal is 400)
        eq("bare code grouped", WearRules.showCode("abcd1234"), "ABCD-1234");
        eq("grouped code kept", WearRules.showCode(" WXYZ-0001 "), "WXYZ-0001");
        eq("null code", WearRules.showCode(null), "");
        eq("pending polls", WearRules.keepPolling(200), true);
        eq("slow_down polls", WearRules.keepPolling(429), true);
        eq("blip polls", WearRules.keepPolling(502), true);
        eq("offline polls", WearRules.keepPolling(0), true);
        eq("expired stops", WearRules.keepPolling(400), false);
        eq("refused stops", WearRules.keepPolling(403), false);
        eq("interval floor", WearRules.nextInterval(1, 200, 0), 5);
        eq("server interval", WearRules.nextInterval(5, 200, 8), 8);
        eq("slow_down widens", WearRules.nextInterval(5, 429, 5), 10);
        eq("widen cap", WearRules.nextInterval(60, 429, 0), 60);
        eq("expired words", WearRules.signInError(400, "expired_token"), "That code expired. Try again.");

        // the session: only Identity refusing it signs the watch out; an outage keeps it
        eq("refresh 401 refuses", WearRules.sessionRefused(401), true);
        eq("refresh 403 refuses", WearRules.sessionRefused(403), true);
        eq("identity down keeps", WearRules.sessionRefused(502), false);
        eq("offline keeps", WearRules.sessionRefused(0), false);
        eq("alive keeps", WearRules.sessionRefused(200), false);
        long day = 24L * 3600_000L, every = 20L * 3600_000L;
        eq("never renewed is due", WearRules.renewDue(0, 10 * day, every), true);
        eq("fresh not due", WearRules.renewDue(10 * day, 10 * day + 3600_000L, every), false);
        eq("old is due", WearRules.renewDue(10 * day, 11 * day, every), true);
        eq("clock went back is due", WearRules.renewDue(10 * day, 9 * day, every), true);

        // a conversation ends on a bare goodbye, never on a question that mentions one
        for (String bye : new String[] {"Thanks.", "thank you", "That's all", "that’s all.", "Stop", "ok thanks",
                "Never mind", "goodbye Aither", "No thanks", "I'm done"}) {
            eq("ends: " + bye, WearRules.endsConversation(bye), true);
        }
        for (String ask : new String[] {"Thanks, and what's the weather?", "stop the music in the kitchen",
                "how do I say thanks in French", "", "done yet?"}) {
            eq("keeps: " + ask, WearRules.endsConversation(ask), false);
        }
        eq("holding", WearRules.holding("Let me check my memory…"), true);
        eq("holding dots", WearRules.holding("Looking that up..."), true);
        eq("not holding", WearRules.holding("Mount Everest is 8,849 m tall."), false);
        eq("closed is over", WearRules.turnOver(true, 0, false, ""), true);
        eq("answered is over", WearRules.turnOver(false, 1, false, "It is 3 pm."), true);
        eq("open segment waits", WearRules.turnOver(false, 1, true, "It is 3 pm."), false);
        eq("promise waits", WearRules.turnOver(false, 1, false, "Let me check my memory…"), false);
        eq("nothing yet waits", WearRules.turnOver(false, 0, false, ""), false);

        // listening safety: calls, side conversations, the re-open cap, child push-to-talk
        for (int mode = 1; mode <= 6; mode++) eq("call mode " + mode, WearRules.inCallMode(mode), true);
        eq("normal mode", WearRules.inCallMode(0), false);
        for (String to : new String[] {"what time is it", "and the day", "can you set a timer", "Aither lights off",
                "tell me a joke", "is it raining?"}) {
            eq("addressed: " + to, WearRules.addressedToAither(to), true);
        }
        for (String side : new String[] {"lol", "yeah", "he's so annoying", "mom can I go out", "they left already",
                "and then she said no", "wait", ""}) {
            eq("side talk: " + side, WearRules.addressedToAither(side), false);
        }
        eq("cap", WearRules.MAX_AUTO_RELISTENS, 3);
        eq("adult loops", WearRules.mayLoop(false, false), true);
        eq("child push-to-talk", WearRules.mayLoop(true, false), false);
        eq("child with grant", WearRules.mayLoop(true, true), true);
        eq("child profile", WearRules.isChildProfile("", "child", ""), true);
        eq("guardian link", WearRules.isChildProfile("", "", "guardian_link"), true);
        eq("adult profile", WearRules.isChildProfile("", "", "password"), false);

        for (String v : new String[] {"What am I looking at?", "read this for me", "count these", "what colour is it"}) {
            eq("visual: " + v, WearRules.visualQuestion(v), true);
        }
        for (String v : new String[] {"what time is it", "fleet status", "", "tell me a story"}) {
            eq("not visual: " + v, WearRules.visualQuestion(v), false);
        }
        System.out.println(bad == 0 ? "OK" : bad + " FAILED");
        System.exit(bad == 0 ? 0 : 1);
    }
}
