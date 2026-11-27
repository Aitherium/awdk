package com.aitherium.aither;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * The floating fairy's state, shared by the web OS fairy and this phone's overlay.
 *
 * Reads the SAME open-decisions snapshot ({@code GET /api/decisions?status=open}) and
 * running-sessions snapshot ({@code GET /api/decisions/sessions}) that the desktop
 * fairies and web OS read, so tapping the Android edge orb shows live Antigravity &
 * fleet sessions and answerable decision cards right over any app.
 *
 * Pure Java (no Android types) so a desktop JVM runs its tests
 * (awdk/android/aither/test/FairyOverlayStateCheck.java).
 */
public final class FairyOverlayState {
    public static final class OptionItem {
        public final String key;
        public final String label;

        public OptionItem(String key, String label) {
            this.key = key == null ? "" : key;
            this.label = (label == null || label.isEmpty()) ? this.key : label;
        }
    }

    public static final class DecisionItem {
        public final String id;
        public final String title;
        public final String summary;
        public final String urgency;
        public final String agent;
        public final List<OptionItem> options;

        public DecisionItem(String id, String title, String summary, String urgency,
                            String agent, List<OptionItem> options) {
            this.id = id == null ? "" : id;
            this.title = title == null ? "" : title;
            this.summary = summary == null ? "" : summary;
            this.urgency = urgency == null ? "normal" : urgency;
            this.agent = agent == null ? "" : agent;
            this.options = options == null
                    ? Collections.emptyList()
                    : Collections.unmodifiableList(new ArrayList<>(options));
        }

        public boolean isUrgent() {
            return "critical".equals(urgency) || "high".equals(urgency);
        }
    }

    public static final class SessionItem {
        public final String id;
        public final String title;
        public final String harness;
        public final String status;
        public final String detail;

        public SessionItem(String id, String title, String harness, String status, String detail) {
            this.id = id == null ? "" : id;
            this.title = title == null ? "" : title;
            this.harness = harness == null ? "" : harness;
            this.status = status == null ? "idle" : status;
            this.detail = detail == null ? "" : detail;
        }

        public boolean isWaiting() {
            String s = status.toLowerCase(java.util.Locale.ROOT);
            return s.contains("wait") || s.contains("block");
        }

        public boolean isWorking() {
            String s = status.toLowerCase(java.util.Locale.ROOT);
            return "working".equals(s) || "busy".equals(s) || "running".equals(s) || "active".equals(s);
        }
    }

    public static final class RolodexItem {
        public final String id;
        public final String title;
        public final String kind;
        public final String target;
        public final String deck;
        public final boolean favorite;

        public RolodexItem(String id, String title, String kind, String target, String deck, boolean favorite) {
            this.id = id == null ? "" : id;
            this.title = title == null ? "" : title;
            this.kind = "url".equals(kind) ? "url" : "app";
            this.target = target == null ? "" : target;
            this.deck = (deck == null || deck.isEmpty()) ? "Custom" : deck;
            this.favorite = favorite;
        }

        /** Safe launch URL for this phone: OS apps open in the AitherOS WebView, https URLs open directly. */
        public String launchUrl(String origin) {
            if ("app".equals(kind)) {
                return origin + "/?host=android-os&app=" + target;
            }
            if (target.startsWith("/") && !target.startsWith("//")) {
                return origin + target;
            }
            if (target.startsWith("https://")) {
                return target;
            }
            return origin + "/?host=android-os";
        }
    }

    public final int cards;
    public final boolean attention;
    public final boolean speaking;
    public final List<DecisionItem> items;
    public final List<SessionItem> sessions;
    public final List<RolodexItem> rolodex;

    public FairyOverlayState(int cards, boolean attention, boolean speaking) {
        this(cards, attention, speaking, Collections.emptyList(), Collections.emptyList(), Collections.emptyList());
    }

    public FairyOverlayState(int cards, boolean attention, boolean speaking,
                             List<DecisionItem> items, List<SessionItem> sessions) {
        this(cards, attention, speaking, items, sessions, Collections.emptyList());
    }

    public FairyOverlayState(int cards, boolean attention, boolean speaking,
                             List<DecisionItem> items, List<SessionItem> sessions,
                             List<RolodexItem> rolodex) {
        this.cards = Math.max(0, cards);
        this.attention = attention && this.cards > 0;
        this.speaking = speaking;
        this.items = items == null
                ? Collections.emptyList()
                : Collections.unmodifiableList(new ArrayList<>(items));
        this.sessions = sessions == null
                ? Collections.emptyList()
                : Collections.unmodifiableList(new ArrayList<>(sessions));
        this.rolodex = rolodex == null
                ? Collections.emptyList()
                : Collections.unmodifiableList(new ArrayList<>(rolodex));
    }

    public static FairyOverlayState empty() {
        return new FairyOverlayState(0, false, false);
    }

    public FairyOverlayState withCards(int cards, boolean attention) {
        return new FairyOverlayState(cards, attention, speaking, items, sessions, rolodex);
    }

    public FairyOverlayState withSpeaking(boolean speaking) {
        return new FairyOverlayState(cards, attention, speaking, items, sessions, rolodex);
    }

    public FairyOverlayState withSessions(List<SessionItem> nextSessions) {
        return new FairyOverlayState(cards, attention, speaking, items, nextSessions, rolodex);
    }

    public FairyOverlayState withRolodex(List<RolodexItem> nextRolodex) {
        return new FairyOverlayState(cards, attention, speaking, items, sessions, nextRolodex);
    }

    /** Count of sessions currently waiting on the owner or actively working. */
    public int activeSessionCount() {
        int n = 0;
        for (SessionItem s : sessions) {
            if (s.isWaiting() || s.isWorking()) n++;
        }
        return n;
    }

    /** True when any session is waiting on the owner's input/approval. */
    public boolean anySessionWaiting() {
        for (SessionItem s : sessions) {
            if (s.isWaiting()) return true;
        }
        return false;
    }

    /** What the floating fairy reads: the sprite, plus the count when cards wait. */
    public String label() {
        return cards > 0 ? "🧚 " + cards : "🧚";
    }

    /** Compact count badge for the luminous orb UI (empty when 0, "9+" above 9). */
    public String badgeText() {
        if (cards <= 0) return "";
        return cards > 9 ? "9+" : String.valueOf(cards);
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
     * Parse {@code GET /api/decisions?status=open} — keeps full card titles, summaries,
     * agents, and answer options for the floating overlay HUD.
     */
    public static FairyOverlayState parse(String json, boolean speaking) {
        int cards = 0;
        boolean attention = false;
        List<DecisionItem> items = new ArrayList<>();
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
                            if (items.size() < 12) {
                                String id = field(obj, "id");
                                String title = field(obj, "title");
                                String summary = field(obj, "summary");
                                String agent = field(obj, "agent");
                                List<OptionItem> opts = parseOptions(obj);
                                items.add(new DecisionItem(
                                        id == null ? "" : id,
                                        title == null || title.isEmpty() ? "Decision waiting" : title,
                                        summary == null ? "" : summary,
                                        urgency == null ? "normal" : urgency,
                                        agent == null ? "" : agent,
                                        opts));
                            }
                        }
                    }
                }
            }
        }
        return new FairyOverlayState(cards, attention, speaking, items, Collections.emptyList());
    }

    private static List<OptionItem> parseOptions(String obj) {
        List<OptionItem> out = new ArrayList<>();
        int k = obj.indexOf("\"options\"");
        if (k < 0) return out;
        int start = obj.indexOf('[', k);
        if (start < 0) return out;
        int end = matchEnd(obj, start, '[', ']');
        String body = obj.substring(start + 1, end < 0 ? obj.length() : end);
        for (String optObj : topLevelObjects(body)) {
            String key = field(optObj, "key");
            String label = field(optObj, "label");
            if (key != null && !key.isEmpty()) {
                out.add(new OptionItem(key, label == null || label.isEmpty() ? key : label));
                if (out.size() >= 4) break;
            }
        }
        return out;
    }

    /**
     * Parse {@code GET /api/decisions/sessions} — active/recent Antigravity, Claude,
     * and awsh sessions, ordered with waiting first, then working, then recent idle.
     */
    public static List<SessionItem> parseSessions(String json) {
        List<SessionItem> waiting = new ArrayList<>();
        List<SessionItem> working = new ArrayList<>();
        List<SessionItem> idle = new ArrayList<>();
        if (json == null) return Collections.emptyList();
        int key = json.indexOf("\"sessions\"");
        if (key < 0) return Collections.emptyList();
        int start = json.indexOf('[', key);
        if (start < 0) return Collections.emptyList();
        int end = matchEnd(json, start, '[', ']');
        String body = json.substring(start + 1, end < 0 ? json.length() : end);
        for (String obj : topLevelObjects(body)) {
            String id = field(obj, "id");
            if (id == null || id.isEmpty()) continue;
            String title = field(obj, "title");
            if (title == null || title.isEmpty()) title = field(obj, "name");
            if (title == null || title.isEmpty()) title = id.length() > 8 ? id.substring(0, 8) : id;
            String harness = field(obj, "harness_label");
            if (harness == null || harness.isEmpty()) harness = field(obj, "harness");
            if (harness == null) harness = "";
            String status = field(obj, "status");
            if (status == null || status.isEmpty()) status = "idle";
            String detail = field(obj, "waiting_for");
            if (detail == null || detail.isEmpty()) detail = field(obj, "last_activity_summary");
            if (detail == null || detail.isEmpty() || "(transcript not found)".equals(detail)) {
                detail = field(obj, "last_prompt");
            }
            if (detail == null) detail = "";
            SessionItem item = new SessionItem(id, title, harness, status, detail);
            if (item.isWaiting()) waiting.add(item);
            else if (item.isWorking()) working.add(item);
            else idle.add(item);
        }
        List<SessionItem> merged = new ArrayList<>();
        for (SessionItem s : waiting) { if (merged.size() < 10) merged.add(s); }
        for (SessionItem s : working) { if (merged.size() < 10) merged.add(s); }
        for (SessionItem s : idle)    { if (merged.size() < 10) merged.add(s); }
        return merged;
    }

    /** Parse favorites from {@code GET /api/settings/preferences} ({@code preferences.rolodex}). */
    public static List<RolodexItem> parseRolodex(String json) {
        if (json == null || json.isEmpty()) return defaultRolodex();
        int key = json.indexOf("\"rolodex\"");
        if (key < 0) return defaultRolodex();
        int arrStart = json.indexOf('[', key + 9);
        if (arrStart < 0) return defaultRolodex();
        int arrEnd = matchEnd(json, arrStart, '[', ']');
        if (arrEnd < 0) return defaultRolodex();
        List<String> objs = topLevelObjects(json.substring(arrStart + 1, arrEnd));
        List<RolodexItem> favs = new ArrayList<>();
        List<RolodexItem> rest = new ArrayList<>();
        for (String obj : objs) {
            String title = field(obj, "title");
            String target = field(obj, "target");
            if (title == null || title.isEmpty() || target == null || target.isEmpty()) continue;
            String kind = field(obj, "kind");
            String id = field(obj, "id");
            String deck = field(obj, "deck");
            boolean fav = obj.contains("\"favorite\":true") || obj.contains("\"favorite\": true");
            RolodexItem item = new RolodexItem(id, title, kind, target, deck, fav);
            if (fav) favs.add(item);
            else rest.add(item);
        }
        List<RolodexItem> out = new ArrayList<>();
        for (RolodexItem r : favs) { if (out.size() < 12) out.add(r); }
        for (RolodexItem r : rest) { if (out.size() < 12) out.add(r); }
        return out.isEmpty() ? defaultRolodex() : out;
    }

    /** Fallback quick-launch deck when preferences.rolodex has not been customized yet. */
    public static List<RolodexItem> defaultRolodex() {
        List<RolodexItem> out = new ArrayList<>();
        out.add(new RolodexItem("rolodex", "Rolodex", "app", "rolodex", "Play", true));
        out.add(new RolodexItem("darkmatters", "Dark Matters", "app", "darkmatters", "Play", true));
        out.add(new RolodexItem("saga-studio", "Saga", "app", "saga", "Play", true));
        out.add(new RolodexItem("mediaforge", "Media Forge", "app", "mediaforge", "Create", true));
        out.add(new RolodexItem("world-studio", "World Studio", "url", "https://app.aitherium.com/studio?mode=world", "Create", true));
        out.add(new RolodexItem("swarm-board", "Swarm", "url", "https://app.aitherium.com/swarm", "Ops", true));
        return out;
    }

    /** Encode a string safely for a minimal JSON payload. */
    public static String jsonQuote(String raw) {
        String s = raw == null ? "" : raw;
        StringBuilder out = new StringBuilder(s.length() + 2);
        out.append('"');
        for (int i = 0; i < s.length(); i++) {
            char ch = s.charAt(i);
            if (ch == '"') out.append("\\\"");
            else if (ch == '\\') out.append("\\\\");
            else if (ch == '\n') out.append("\\n");
            else if (ch == '\r') out.append("\\r");
            else if (ch == '\t') out.append("\\t");
            else if (ch >= 0x20) out.append(ch);
        }
        out.append('"');
        return out.toString();
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
        // Guard against null / number / object values before the next string.
        int p = colon + 1;
        while (p < obj.length() && Character.isWhitespace(obj.charAt(p))) p++;
        if (p >= obj.length() || obj.charAt(p) != '"') return null;
        StringBuilder v = new StringBuilder();
        for (int i = p + 1; i < obj.length(); i++) {
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
