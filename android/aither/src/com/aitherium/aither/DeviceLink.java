package com.aitherium.aither;

import java.util.Locale;

/**
 * Linking a device (a watch, a TV, a laptop's CLI) to the account signed in on this phone,
 * as pure Java so test/DeviceLinkCheck.java runs it on a desktop JVM. The device shows a
 * short code from the device grant (RFC 8628, Identity /auth/device/code) and a QR of
 * {@link #linkUrl}; the phone camera opens that verified App Link in this app (MainActivity
 * routes it to LinkActivity), or the owner types the code in Settings > Link a device.
 *
 * Only a grown-up's account approves: a child's devices come from the guardian's
 * child-device flow, and Identity refuses a child here too (403).
 */
final class DeviceLink {
    private DeviceLink() {}

    /** The page a scanned QR opens: a verified App Link (this app), else the web page,
     *  which pre-fills the box from {@code code} (Veil app/auth/device). */
    static final String LINK_BASE = "https://app.aitherium.com/auth/device?code=";

    /** The QR's content for a code. */
    static String linkUrl(String userCode) {
        String c = normalize(userCode);
        return c == null ? "" : LINK_BASE + c;
    }

    /**
     * The code as Identity keys it (upper case, ABCD-2345), from anything a person might
     * hand over: the bare code typed with or without the dash or spaces, the QR's
     * app.aitherium.com/auth/device?code= link, the IdP's /link?user_code= link, or
     * aither://link?code=. Null when it holds no well-formed code.
     */
    static String codeFrom(String scannedOrTyped) {
        if (scannedOrTyped == null) return null;
        String s = scannedOrTyped.trim();
        if (s.isEmpty()) return null;
        if (s.contains("://")) {
            String lower = s.toLowerCase(Locale.ROOT);
            boolean app = lower.startsWith("aither://link");
            if (!app) {
                if (!lower.startsWith("https://")) return null;
                String rest = lower.substring("https://".length());
                int slash = rest.indexOf('/');
                String host = slash < 0 ? rest : rest.substring(0, slash);
                int colon = host.indexOf(':');
                if (colon >= 0) return null; // no ports: only the real hosts
                if (!(host.equals("aitherium.com") || host.endsWith(".aitherium.com"))) return null;
                String path = slash < 0 ? "/" : rest.substring(slash);
                int q = path.indexOf('?');
                String p = q < 0 ? path : path.substring(0, q);
                if (!(p.equals("/auth/device") || p.equals("/auth/device/") || p.equals("/link") || p.equals("/link/"))) {
                    return null;
                }
            }
            String v = query(s, "user_code");
            if (v == null) v = query(s, "code");
            return normalize(v);
        }
        return normalize(s);
    }

    /** Upper case, spaces and dashes dropped, 8 letters/digits, shown as ABCD-2345. */
    static String normalize(String code) {
        if (code == null) return null;
        StringBuilder b = new StringBuilder();
        for (char ch : code.toCharArray()) {
            if (ch == '-' || ch == ' ' || ch == '‑' || ch == '–') continue;
            char u = Character.toUpperCase(ch);
            if (!((u >= 'A' && u <= 'Z') || (u >= '0' && u <= '9'))) return null;
            b.append(u);
        }
        if (b.length() != 8) return null;
        return b.substring(0, 4) + "-" + b.substring(4);
    }

    /** Why this phone's account may not link a device, or null when it may. */
    static String childRefusal(String profileKind, boolean childDevice) {
        if (childDevice || "child".equals(profileKind)) {
            return "A grown-up in your family links new devices. Ask them to scan the code with their phone.";
        }
        return null;
    }

    /** The approve sheet's question before the server names the device. */
    static String question(String code) {
        return "Sign in the device showing " + code + " to your Aither account?";
    }

    /** After an approve, in words: the server's device name when it gave one. */
    static String approved(String clientName) {
        String n = clientName == null ? "" : clientName.trim();
        return (n.isEmpty() ? "The device" : n) + " is signed in. It can take a few seconds to notice.";
    }

    /** A refused approve, in words. */
    static String refusal(int status, String detail) {
        if (status == 403) return detail == null || detail.isEmpty() ? childRefusal("child", true) : detail;
        if (status == 404 || status == 400) return "That code is wrong or has expired. Get a fresh code on the device and try again.";
        if (status == 401) return "Sign in to Aither on this phone first, then try again.";
        if (status == 0) return "No connection. Check this phone's internet and try again.";
        return "Couldn't link the device (" + status + "). Try again.";
    }

    /** One query value, percent-decoded for the characters a code can hold. */
    private static String query(String url, String key) {
        int q = url.indexOf('?');
        if (q < 0) return null;
        int hash = url.indexOf('#', q);
        String qs = hash < 0 ? url.substring(q + 1) : url.substring(q + 1, hash);
        for (String pair : qs.split("&")) {
            int eq = pair.indexOf('=');
            String k = eq < 0 ? pair : pair.substring(0, eq);
            if (k.equals(key)) {
                String v = eq < 0 ? "" : pair.substring(eq + 1);
                return v.replace("%2D", "-").replace("%2d", "-").replace("+", " ").replace("%20", " ");
            }
        }
        return null;
    }
}
