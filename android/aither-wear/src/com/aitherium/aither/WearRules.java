package com.aitherium.aither;

import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * What the watch decides, as pure Java so test/WearRulesCheck.java runs it on a desktop JVM:
 * the "Waiting for you" list, the ONLY payload an Approve/Deny tap may send, when the watch
 * may answer at all, and how the sign-in poll behaves.
 *
 * The inbox is the phone's own (POST /api/push/inbox, lib/hearth/notices.py): pages of up to
 * ten notices in change order, so a card seen twice is replaced by its later view. An answer
 * is ApprovalCard.decideBody of the card as it was shown (the phone's binding, shared source),
 * and only for a card this account can still answer; the server refuses a stale digest, a
 * second vote, a foreign card and a child's vote, and the Hearth re-checks the digest.
 */
final class WearRules {
    private WearRules() {}

    /** The inbox's page size (store.inbox limit) and how many pages one refresh reads. */
    static final int PAGE = 10;
    static final int MAX_PAGES = 5;

    /** Fold one inbox page into the cards by id: a later view of a card replaces the earlier. */
    static void merge(Map<String, ApprovalCard> into, List<ApprovalCard> page) {
        if (page == null) return;
        for (ApprovalCard c : page) {
            if (c == null || !ApprovalCard.validId(c.noticeId)) continue;
            into.remove(c.noticeId); // re-insert: the map keeps change order
            into.put(c.noticeId, c);
        }
    }

    /** Read another page: the last one was full and the refresh is under its page budget. */
    static boolean more(int lastPageSize, int pagesRead) {
        return lastPageSize >= PAGE && pagesRead < MAX_PAGES;
    }

    /** "Waiting for you": open approvals this account has not answered, urgent first, newest first. */
    static List<ApprovalCard> waiting(Collection<ApprovalCard> cards) {
        List<ApprovalCard> urgent = new ArrayList<>();
        List<ApprovalCard> rest = new ArrayList<>();
        for (ApprovalCard c : cards) {
            if (c == null || !c.answerable()) continue;
            (c.urgent ? urgent : rest).add(0, c);
        }
        urgent.addAll(rest);
        return urgent;
    }

    static List<ApprovalCard> waiting(Map<String, ApprovalCard> byId) {
        return waiting(byId.values());
    }

    static Map<String, ApprovalCard> byId() { return new LinkedHashMap<>(); }

    /** The line above the list. */
    static String countLine(int n) {
        if (n <= 0) return "Nothing is waiting for you";
        return n == 1 ? "1 thing is waiting for you" : n + " things are waiting for you";
    }

    /** The decide body for a tap on {@code card}, or null when the card cannot be answered. */
    static String answer(ApprovalCard card, boolean allow) {
        if (card == null || !card.answerable()) return null;
        return ApprovalCard.decideBody(card.noticeId, card.digest, allow);
    }

    /**
     * Why the watch may not answer right now, or null when it may. The phone's rule
     * (setAuthenticationRequired): only an unlocked device answers. A watch with no screen
     * lock never locks when it leaves a wrist, so it cannot prove it is still being worn and
     * answers nothing; with a lock, Wear OS locks it on removal.
     */
    static String guard(boolean secure, boolean locked) {
        if (!secure) return "Set a screen lock on this watch to answer. Without one it can't tell it's on your wrist.";
        if (locked) return "Unlock your watch to answer.";
        return null;
    }

    // ------------------------------------------------------------------ sign-in (RFC 8628)

    /** The code as the watch shows it: upper case, grouped like ABCD-1234 when it is bare. */
    static String showCode(String userCode) {
        if (userCode == null) return "";
        String s = userCode.trim().toUpperCase(java.util.Locale.ROOT);
        if (s.length() == 8 && s.indexOf('-') < 0) s = s.substring(0, 4) + "-" + s.substring(4);
        return s;
    }

    /** Keep polling after this answer? 200 pending, 429 slow_down and 0/5xx (a blip) do. */
    static boolean keepPolling(int status) {
        return status == 200 || status == 429 || status == 0 || status >= 500;
    }

    /** Seconds to the next poll: the server's interval (at least 5), +5 on every slow_down. */
    static int nextInterval(int current, int status, int serverInterval) {
        int base = Math.max(5, Math.max(current, serverInterval));
        return status == 429 ? Math.min(base + 5, 60) : base;
    }

    /** A terminal sign-in answer in words. */
    static String signInError(int status, String error) {
        if ("expired_token".equals(error)) return "That code expired. Try again.";
        if ("access_denied".equals(error)) return "Sign-in was declined on the phone.";
        if (status == 0) return "No connection. Check the watch's Wi-Fi or LTE and try again.";
        return "Sign-in didn't finish (" + (error == null || error.isEmpty() ? String.valueOf(status) : error) + "). Try again.";
    }

    /** Identity's refresh answer that means the session is gone (not "could not ask"). */
    static boolean sessionRefused(int code) {
        return code == 401 || code == 403;
    }

    /** Renew the sliding session now? (never renewed counts as due) */
    static boolean renewDue(long renewedAt, long now, long every) {
        return renewedAt <= 0 || now - renewedAt >= every || now < renewedAt;
    }
}
