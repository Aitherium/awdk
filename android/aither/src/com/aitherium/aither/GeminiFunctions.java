package com.aitherium.aither;

import java.io.UnsupportedEncodingException;
import java.net.URLEncoder;

/**
 * What Aither lets Gemini (or any agent Android allows to run app functions) do, and how:
 * the policy behind AitherFunctionService, pure Java so test/GeminiFunctionsCheck.java runs it
 * on a desktop JVM. assets/aither_functions.xml and assets/app_functions.xml declare the same
 * ids; the check fails when they drift.
 *
 * Four functions, none of which runs anything on its own:
 *  - askAither: open Ask Aither with a question (the on-device assistant answers it there).
 *  - openApp: open Learn, Family or Hearth.
 *  - pendingApprovals: how many household approvals wait for this account (read-only,
 *    from the inbox this phone last read; no network call).
 *  - askHearth: open Hearth with the request written in its box. The person sends it, and
 *    Hearth's own rules apply as for anything typed there: an action that leaves the home
 *    waits for an approval card. Nothing is sent and no device action runs from here.
 *
 * A child's phone gets the two read-only ones (pendingApprovals; openApp to the child's own
 * Learn and room). askAither and askHearth are refused there and switched off in Android's
 * index (AitherFunctionService.sync), so Gemini does not even offer them.
 */
final class GeminiFunctions {
    static final String PREFIX = "com.aitherium.aither.AitherFunctions#";
    static final String ASK_AITHER = PREFIX + "askAither";
    static final String OPEN_APP = PREFIX + "openApp";
    static final String PENDING_APPROVALS = PREFIX + "pendingApprovals";
    static final String ASK_HEARTH = PREFIX + "askHearth";
    static final String[] ALL = {ASK_AITHER, OPEN_APP, PENDING_APPROVALS, ASK_HEARTH};
    /** Only these run on a child's phone: they read or open the child's own pages. */
    static final String[] CHILD = {OPEN_APP, PENDING_APPROVALS};

    /** The Hearth box takes this much (hearth-widget.tsx, maxLength). */
    static final int MAX_HEARTH = 600;
    static final int MAX_QUESTION = 1200;

    /** openApp's targets: name, adult path, child path ("" = not on a child's phone). */
    static final String[][] APPS = {
        {"learn", "/?app=learn", "/learn"},
        {"family", "/?app=family", ""},
        {"hearth", "/?app=hearth", "/hearth"},
    };

    private GeminiFunctions() {}

    static boolean known(String id) {
        for (String s : ALL) if (s.equals(id)) return true;
        return false;
    }

    /** May `id` run on this phone? Unknown ids never; on a child's phone only CHILD. */
    static boolean allowed(String id, boolean child) {
        if (!known(id)) return false;
        if (!child) return true;
        for (String s : CHILD) if (s.equals(id)) return true;
        return false;
    }

    /** The page openApp opens, or null for an unknown app or one a child does not get. */
    static String appUrl(String app, boolean child) {
        String a = app == null ? "" : app.trim().toLowerCase(java.util.Locale.ROOT);
        for (String[] r : APPS) {
            if (!r[0].equals(a)) continue;
            String path = child ? r[2] : r[1];
            return path.isEmpty() ? null : AppTabs.ORIGIN + path;
        }
        return null;
    }

    /** Text from an agent, made safe to show: no control characters, trimmed, clipped. */
    static String clean(String s, int max) {
        if (s == null) return "";
        String t = s.replaceAll("[\\p{Cntrl}&&[^\n]]", " ").replace('\n', ' ').trim();
        return t.length() <= max ? t : t.substring(0, max).trim();
    }

    /** The Hearth page with the request in its box, or null (child's phone, empty request). */
    static String hearthUrl(String request, boolean child) {
        if (child) return null;
        String r = clean(request, MAX_HEARTH);
        if (r.isEmpty()) return null;
        try {
            return AppTabs.ORIGIN + "/?app=hearth&ask=" + URLEncoder.encode(r, "UTF-8").replace("+", "%20");
        } catch (UnsupportedEncodingException e) {
            return null;
        }
    }

    /** pendingApprovals' answer. checkedAgoMin < 0: this phone has not read the inbox yet. */
    static String approvalsLine(int open, long checkedAgoMin, boolean child) {
        if (checkedAgoMin < 0) return "Aither has not checked your home's approvals on this phone yet.";
        String when = checkedAgoMin < 1 ? "just now" : checkedAgoMin < 120 ? checkedAgoMin + " min ago"
                : (checkedAgoMin / 60) + " h ago";
        if (open <= 0) return "Nothing is waiting for " + (child ? "a grown-up's" : "your") + " approval (checked " + when + ").";
        return open + (open == 1 ? " request is" : " requests are") + " waiting for "
                + (child ? "a grown-up's approval (checked " + when + ")."
                        : "your approval (checked " + when + "). Open Hearth in Aither to answer.");
    }
}
