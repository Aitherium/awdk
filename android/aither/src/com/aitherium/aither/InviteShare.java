package com.aitherium.aither;

import java.net.URI;

/**
 * What window.AitherApp.share() may hand to Android's share sheet (FamilyNotices.Bridge).
 * The WebView has no navigator.share, so a family invite is shared through the app. Only
 * an aitherium.com or app.aitherium.com https link is shared, and the message is built
 * here as "<text> <url>", so a page can never make the app send arbitrary text.
 * Plain Java (no Android imports) so test/InviteShareCheck.java runs on a desktop JVM.
 */
final class InviteShare {
    static final int MAX_TEXT = 300;
    static final int MAX_TITLE = 100;

    private InviteShare() {}

    /** https://aitherium.com/... or https://app.aitherium.com/..., default port, no user info. */
    static boolean shareable(String url) {
        if (url == null || url.length() > 2048) return false;
        try {
            URI u = new URI(url);
            String h = u.getHost();
            return "https".equals(u.getScheme()) && u.getRawUserInfo() == null && u.getPort() == -1
                    && h != null && (h.equals("aitherium.com") || h.equals("app.aitherium.com"));
        } catch (Exception e) {
            return false;
        }
    }

    /** The shared message, or null when the link is refused. */
    static String message(String text, String url) {
        if (!shareable(url)) return null;
        String t = clip(text, MAX_TEXT);
        return t.isEmpty() ? url : t + " " + url;
    }

    static String clip(String s, int max) {
        String t = s == null ? "" : s.replaceAll("\\s+", " ").trim();
        return t.length() > max ? t.substring(0, max) : t;
    }
}
