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

    // ------------------------------------------------------------------ conversation

    /** Words that end a conversation when they are the whole turn ("thanks", "that's all"). */
    private static final java.util.regex.Pattern BYE = java.util.regex.Pattern.compile(
            "^(ok(ay)?[ ,]+)?(stop|stop listening|cancel|never ?mind|thanks?|thank you|thanks a lot|thank you so much"
            + "|that'?s all|that is all|that'?s it|i'?m done|we'?re done|done|goodbye|good ?bye|bye|bye bye|no thanks?)"
            + "( aither)?$");

    /** Does this heard turn end the conversation (instead of being asked)? */
    static boolean endsConversation(String heard) {
        if (heard == null) return false;
        String s = heard.toLowerCase(java.util.Locale.ROOT).replace('’', '\'')
                .replaceAll("[.!?,]+$", "").replaceAll("[.!?]", "").trim();
        return !s.isEmpty() && BYE.matcher(s).matches();
    }

    // ------------------------------------------------------------------ listening safety
    // Owner, 2026-10-08: "what if alexander is talking on his phone". Same rules as Veil's
    // lib/voice-loop.ts (addressedToAither, MAX_AUTO_RELISTENS) so the watch and the hub agree.

    /** Re-opens of the mic without a tap before the conversation waits for one. */
    static final int MAX_AUTO_RELISTENS = 3;

    /** AudioManager modes that mean a call (ringing, cellular, VoIP, screening, redirect). */
    static boolean inCallMode(int mode) {
        return mode >= 1 && mode <= 6; // RINGTONE 1, IN_CALL 2, IN_COMMUNICATION 3, CALL_SCREENING 4, *_REDIRECT 5/6
    }

    /** May the mic re-open by itself? A child account only with the guardian's "handsfree" grant. */
    static boolean mayLoop(boolean child, boolean handsFreeGranted) {
        return !child || handsFreeGranted;
    }

    /** Is this profile a family child account (Identity metadata.account_type / guardian link)? */
    static boolean isChildProfile(String accountType, String metaAccountType, String authMethod) {
        return "child".equals(accountType) || "child".equals(metaAccountType) || "guardian_link".equals(authMethod);
    }

    private static final java.util.Set<String> REQUEST_START = new java.util.HashSet<>(java.util.Arrays.asList(
            "what", "what's", "whats", "who", "who's", "whos", "how", "how's", "when", "where", "where's", "why",
            "which", "is", "are", "am", "was", "can", "could", "would", "will", "should", "shall", "do", "does",
            "did", "tell", "show", "play", "set", "remind", "open", "turn", "start", "give", "find", "search",
            "look", "read", "help", "explain", "say", "make", "add", "call", "send", "check", "list", "repeat",
            "again", "more", "next", "go", "translate", "spell", "define", "let", "let's", "lets", "i", "i'd",
            "i'm", "my", "me", "please", "hey", "hi", "hello", "yes", "sure"));
    private static final java.util.Set<String> LEAD_GLUE = new java.util.HashSet<>(java.util.Arrays.asList(
            "and", "so", "also", "then", "ok", "okay", "um", "uh", "oh", "well", "but", "now", "alright"));
    private static final java.util.Set<String> THIRD_PERSON = new java.util.HashSet<>(java.util.Arrays.asList(
            "he", "he's", "hes", "she", "she's", "shes", "they", "they're", "theyre", "his", "her", "their",
            "him", "them", "mom", "mum", "dad", "mommy", "daddy", "bro", "dude"));

    /**
     * On a mic the conversation re-opened BY ITSELF: does this turn speak TO Aither? A side
     * conversation ("she said she's coming at five", "lol") is dropped and the loop ends. A turn
     * the person started with a tap never goes through this.
     */
    static boolean addressedToAither(String heard) {
        if (heard == null) return false;
        String raw = heard.trim();
        String[] w = raw.toLowerCase(java.util.Locale.ROOT).replace('’', '\'')
                .replaceAll("[^a-z0-9' ]+", " ").trim().split("\\s+");
        if (w.length == 0 || w[0].isEmpty()) return false;
        for (String x : w) if (x.equals("aither") || x.equals("ether")) return true;
        int i = 0;
        while (i < w.length - 1 && LEAD_GLUE.contains(w[i])) i++;
        String first = w[i];
        if (THIRD_PERSON.contains(first)) return false;
        boolean asksYou = false;
        for (int k = 0; k < Math.min(6, w.length); k++) {
            if (w[k].equals("you") || w[k].equals("your") || w[k].equals("you're") || w[k].equals("youre")) asksYou = true;
        }
        boolean question = raw.endsWith("?");
        if (w.length - i < 2 && !question) return false;
        return i > 0 || REQUEST_START.contains(first) || asksYou || question;
    }

    /** "Let me check…": a first answer that promises more, so the turn is not over yet. */
    private static final java.util.regex.Pattern HOLDING = java.util.regex.Pattern.compile(
            "(?is)^ *(let me (check|look|see|think|find|pull)|one (moment|sec)|give me a (moment|sec)|hang on"
            + "|checking|looking (that|it) up|just a (moment|sec)).*");

    static boolean holding(String answer) {
        if (answer == null) return false;
        String s = answer.trim();
        if (s.isEmpty()) return true;
        return HOLDING.matcher(s).matches() && s.length() < 120
                || s.endsWith("…") || s.endsWith("...");
    }

    /**
     * Is this answer's turn over, so the watch can listen again? Yes when the stream closed, or
     * when a segment has ended, none is open, and what was said does not promise more.
     */
    static boolean turnOver(boolean closed, int segmentsEnded, boolean segmentOpen, String shown) {
        if (closed) return true;
        return segmentsEnded > 0 && !segmentOpen && !holding(shown);
    }

    /** The watch has no camera: a question that needs eyes ("what am I looking at", "read
     *  this", "count these") goes to the owner's live phone camera when it is on. */
    private static final java.util.regex.Pattern VISUAL = java.util.regex.Pattern.compile(
            "\\b(look(ing)? at|see|seeing|in front of|read (this|that|it)|what('?s| is) (this|that)|"
            + "count (these|those|them)|what colou?r|how many|which one|does this look)\\b",
            java.util.regex.Pattern.CASE_INSENSITIVE);

    static boolean visualQuestion(String q) {
        return q != null && VISUAL.matcher(q).find();
    }

    /** What the watch says when nothing can see for it. */
    static final String NO_EYES = "I can't see from the watch. Turn on the live camera in Aither on "
            + "your phone, then ask again.";
}
