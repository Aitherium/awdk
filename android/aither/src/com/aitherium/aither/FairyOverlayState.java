package com.aitherium.aither;

import java.util.ArrayList;
import java.util.List;

/**
 * The floating fairy's state, shared by the web OS fairy and this phone's overlay.
 *
 * The web fairy (the web OS fairy component) carries the owner's
 * waiting decision cards: it subscribes to the relay {@code #decisions} push and, while that
 * stream is down, reconciles against {@code GET /api/decisions?status=open}. #18476 made the
 * web fairy ALSO re-read that snapshot whenever the tab returns, because a reconnect can say
 * "connected" without replaying the channel. The Android {@link FairyOverlayService} is the
 * same fairy outside the app: it reads the SAME snapshot with the WebView's own session
 * cookies and reconciles on every network reconnect and on a timer, so the phone cannot show
 * a different count from the desk.
 *
 * This class is PURE Java (no Android types) so a desktop JVM runs its tests
 * (awdk/android/aither/test/FairyOverlayStateCheck.java).
 *
 *   cards      how many cards are open (the badge count)
 *   attention  at least one open card is critical or high urgency (the fairy is "needs you")
 *   speaking   Aither is talking right now (mirrors the web fairy's speaking flash)
 */
public final class FairyOverlayState {
    public final int cards;
    public final boolean attention;
    public final boolean speaking;

    public FairyOverlayState(int cards, boolean attention, boolean speaking) {
        this.cards = Math.max(0, cards);
        this.attention = attention && this.cards > 0;
        this.speaking = speaking;
    }

    public static FairyOverlayState empty() {
        return new FairyOverlayState(0, false, false);
    }

    public FairyOverlayState withCards(int cards, boolean attention) {
        return new FairyOverlayState(cards, attention, speaking);
    }

    public FairyOverlayState withSpeaking(boolean speaking) {
        return new FairyOverlayState(cards, attention, speaking);
    }

    /** What the floating fairy reads: the sprite, plus the count when cards wait. */
    public String label() {
        return cards > 0 ? "🧚 " + cards : "🧚";
    }

    /** The accessibility description; names the count and whether the person is needed. */
    public String description() {
        StringBuilder s = new StringBuilder("Aither fairy: tap to open Aither, drag to move");
        if (cards > 0) s.append(", ").append(cards).append(cards == 1 ? " card waiting" : " cards waiting");
        if (attention) s.append(", needs you");
        if (speaking) s.append(", speaking");
        return s.toString();
    }

    /**
     * Parse {@code GET /api/decisions?status=open} — the snapshot shape
     * {@code {"decisions":[{"id":..,"status":"open","urgency":"high",..}]}}.
     *
     * A card counts when it has no status or its status is "open"; a closed card (answered,
     * expired) does not. Attention is any open card whose urgency is critical or high. A body
     * that is missing, null or not the expected shape yields zero cards, never an exception:
     * an unreachable snapshot must not fake a reading (the web keeps its last cards instead).
     */
    public static FairyOverlayState parse(String json, boolean speaking) {
        int cards = 0;
        boolean attention = false;
        if (json != null) {
            int key = json.indexOf("\"decisions\"");
            if (key >= 0) {
                int start = json.indexOf('[', key);
                if (start >= 0) {
                    int end = matchEnd(json, start, '[', ']');
                    String body = json.substring(start + 1, end < 0 ? json.length() : end);
                    for (String obj : topLevelObjects(body)) {
                        String status = field(obj, "status");
                        if (status == null || status.isEmpty() || status.equals("open")) {
                            cards++;
                            String urgency = field(obj, "urgency");
                            if ("critical".equals(urgency) || "high".equals(urgency)) attention = true;
                        }
                    }
                }
            }
        }
        return new FairyOverlayState(cards, attention, speaking);
    }

    /** The top-level {@code {...}} spans of an array body, as raw text. */
    static List<String> topLevelObjects(String body) {
        List<String> out = new ArrayList<>();
        int p = 0;
        while (p < body.length()) {
            int o = body.indexOf('{', p);
            if (o < 0) break;
            int c = matchEnd(body, o, '{', '}');
            if (c < 0) break;
            out.add(body.substring(o, c + 1));
            p = c + 1;
        }
        return out;
    }

    /** Index of the closer matching the opener at {@code open}, or -1. Skips quoted strings. */
    static int matchEnd(String s, int open, char openCh, char closeCh) {
        int depth = 0;
        boolean inStr = false;
        for (int i = open; i < s.length(); i++) {
            char ch = s.charAt(i);
            if (inStr) {
                if (ch == '\\') i++;
                else if (ch == '"') inStr = false;
                continue;
            }
            if (ch == '"') inStr = true;
            else if (ch == openCh) depth++;
            else if (ch == closeCh && --depth == 0) return i;
        }
        return -1;
    }

    /** The string value of {@code "name"} in {@code obj}, or null. First match at any depth. */
    static String field(String obj, String name) {
        String needle = "\"" + name + "\"";
        int k = obj.indexOf(needle);
        if (k < 0) return null;
        int colon = obj.indexOf(':', k + needle.length());
        if (colon < 0) return null;
        int q = obj.indexOf('"', colon + 1);
        if (q < 0) return null;
        StringBuilder v = new StringBuilder();
        for (int i = q + 1; i < obj.length(); i++) {
            char ch = obj.charAt(i);
            if (ch == '\\' && i + 1 < obj.length()) {
                char n = obj.charAt(++i);
                v.append(n == 'n' ? '\n' : n == 't' ? '\t' : n);
            } else if (ch == '"') {
                return v.toString();
            } else {
                v.append(ch);
            }
        }
        return null;
    }
}
