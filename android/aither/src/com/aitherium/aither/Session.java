package com.aitherium.aither;

import android.webkit.CookieManager;

import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Is this phone signed in to AitherOS? One answer for every tab, the household check-in and
 * Settings, from the one session they all share: the Domain=.aitherium.com
 * aither_auth_token cookie in this app's WebView storage.
 *
 * Only Identity can sign someone out: a 401/403 from the app's own GET /api/profile (the
 * route every AitherOS page asks), or no session cookie at all. Anything else (a 5xx, no
 * network, the fleet down overnight on 2026-10-06) is UNKNOWN and changes nothing.
 */
final class Session {
    enum State { IN, OUT, UNKNOWN }

    static final String COOKIE = "aither_auth_token";
    static final String PROBE = AppTabs.ORIGIN + "/api/profile";

    /** What the last check found (any caller in this process). */
    static volatile State last = State.UNKNOWN;
    /** Since the tabs last loaded with a session, something found none or no answer (an
     *  outage, a sign-out): the next IN reloads them (MainActivity.applySession). */
    static volatile boolean troubled;
    /** Did the last IN answer name the platform owner (AppTabs.platformOwner)? null = no
     *  answer yet. Gates the owner-only Home apps. */
    static volatile Boolean owner;

    private Session() {}

    /** Pure, for test/SessionCheck: the answer for a cookie jar and an HTTP status
     *  (-1 = no answer at all). */
    static State verdict(String cookies, int code) {
        if (!hasSession(cookies)) return State.OUT;
        if (code == 200) return State.IN;
        if (code == 401 || code == 403) return State.OUT;
        return State.UNKNOWN;
    }

    static boolean hasSession(String cookies) {
        if (cookies == null) return false;
        for (String kv : cookies.split(";")) {
            String s = kv.trim();
            if (s.startsWith(COOKIE + "=") && s.length() > COOKIE.length() + 1) return true;
        }
        return false;
    }

    /** Ask the server, with the WebView's cookies. Blocking: call off the main thread. */
    static State check() {
        String cookies = CookieManager.getInstance().getCookie(AppTabs.ORIGIN);
        int code = -1;
        if (hasSession(cookies)) {
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(PROBE).openConnection();
                c.setConnectTimeout(10000);
                c.setReadTimeout(15000);
                c.setRequestProperty("Cookie", cookies);
                c.setRequestProperty("Accept", "application/json");
                code = c.getResponseCode();
                if (code == 200) owner = owner(c);
                c.disconnect();
            } catch (Exception e) { /* no answer: UNKNOWN */ }
        }
        return note(verdict(cookies, code));
    }

    /** The profile's roles and tenant_id, read as AppTabs.platformOwner; null if unreadable. */
    private static Boolean owner(HttpURLConnection c) {
        try (java.io.InputStream in = c.getInputStream()) {
            org.json.JSONObject j = new org.json.JSONObject(new String(in.readAllBytes(),
                    java.nio.charset.StandardCharsets.UTF_8));
            java.util.List<String> roles = new java.util.ArrayList<>();
            org.json.JSONArray a = j.optJSONArray("roles");
            for (int i = 0; a != null && i < a.length(); i++) roles.add(a.optString(i));
            if (!j.optString("role").isEmpty()) roles.add(j.optString("role"));
            return AppTabs.platformOwner(roles, j.optString("tenant_id", j.optString("tenant")));
        } catch (Exception e) {
            return null;
        }
    }

    /** Record an answer another call learned (the check-in's 401 is Identity's word too). */
    static State note(State s) {
        last = s;
        if (s != State.IN) troubled = true;
        return s;
    }
}
