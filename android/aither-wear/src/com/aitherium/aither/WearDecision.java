package com.aitherium.aither;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/**
 * One open decision card (an agent's ask: GET /api/decisions?status=open), as the watch shows
 * it. A card with options is answered from the watch by tapping one (POST
 * /api/decisions/{id}/answer); a card without options (free text) or asking for a credential
 * is handed to the phone, as is any answer the server refuses with 403 (it needs a passkey):
 * the watch shows "Answer on your phone" with the card's page, app.aitherium.com/decide?id=.
 * The rules here are plain Java (test/WearDecisionCheck.java); parsing needs org.json.
 */
final class WearDecision {
    static final String DECIDE = "https://app.aitherium.com/decide?id=";
    static final int SHOWN = 8;

    final String id, title, summary, kind, urgency, defaultKey;
    final String[][] options; // key, label

    WearDecision(String id, String title, String summary, String kind, String urgency,
                 String defaultKey, String[][] options) {
        this.id = id;
        this.title = title;
        this.summary = summary;
        this.kind = kind;
        this.urgency = urgency;
        this.defaultKey = defaultKey;
        this.options = options;
    }

    /** Ids are short tokens ("d-zgst"); anything else never reaches a URL path. */
    static boolean validId(String id) {
        return id != null && id.matches("[A-Za-z0-9_-]{1,64}");
    }

    /** Can the watch answer it with a tap? Not a credential, not free text, at most 4 options. */
    boolean onWatch() {
        return !"credential".equals(kind) && options.length > 0 && options.length <= 4;
    }

    boolean urgent() {
        return "high".equals(urgency) || "critical".equals(urgency);
    }

    String page() {
        return DECIDE + id;
    }

    /** What the watch says after an answer, from the HTTP status. */
    static String outcome(int code, String label) {
        if (code == 200) return "Answered: " + label;
        if (code == 403) return "This one needs your passkey. Answer on your phone.";
        if (code == 409) return "Already answered.";
        if (code == 404) return "This ask is gone.";
        if (code == 401) return "Signed out. Sign in again.";
        if (code == 0) return "No connection. Try again.";
        return "Couldn't answer (" + code + ").";
    }

    /** Urgent first, then newest first (the list arrives newest first), at most SHOWN. */
    static List<WearDecision> order(List<WearDecision> all) {
        List<WearDecision> out = new ArrayList<>();
        for (WearDecision d : all) if (d.urgent()) out.add(d);
        for (WearDecision d : all) if (!d.urgent()) out.add(d);
        return out.size() > SHOWN ? new ArrayList<>(out.subList(0, SHOWN)) : out;
    }

    static List<WearDecision> parseAll(JSONArray a) {
        List<WearDecision> out = new ArrayList<>();
        for (int i = 0; a != null && i < a.length(); i++) {
            JSONObject d = a.optJSONObject(i);
            if (d == null || !validId(d.optString("id")) || !"open".equals(d.optString("status", "open"))) continue;
            JSONArray o = d.optJSONArray("options");
            int n = o == null ? 0 : o.length();
            String[][] opts = new String[n][];
            for (int k = 0; k < n; k++) {
                JSONObject x = o.optJSONObject(k);
                String key = x == null ? "" : x.optString("key");
                opts[k] = new String[] {key, x == null ? key : x.optString("label", key)};
            }
            out.add(new WearDecision(d.optString("id"), d.optString("title"), d.optString("summary"),
                    d.optString("kind"), d.optString("urgency"), d.optString("default_key"), opts));
        }
        return out;
    }
}
