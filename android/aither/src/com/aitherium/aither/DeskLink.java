package com.aitherium.aither;

import java.nio.charset.StandardCharsets;
import java.util.Base64;

/**
 * The phone half of the USB desk link, the part with no Android in it (test/DeskLinkCheck
 * runs it on a desktop JVM). The owner's PC runs {@code adb reverse tcp:47937 tcp:<port>},
 * so on this phone {@link #BASE} reaches the desk's phone-link server over the cable.
 *
 * There is no new token: every call but /link/info carries this phone's existing device
 * hello (kvdevice.js, the key enrolled as its Identity node's seal_pubkey) for the audience
 * {@link #audience}. kvdevice lower-cases the relay it signs, so the desk compares the
 * audience lower-cased too.
 */
final class DeskLink {
    static final int PORT = 47937;
    static final String BASE = "http://127.0.0.1:" + PORT;
    static final String HEADER = "X-Aither-Device-Hello";
    static final String ORIGIN = "https://appassets.androidplatform.net/";

    private DeskLink() {}

    /** The four calls the page may make, by exact method and path; nothing else leaves. */
    static boolean pathAllowed(String method, String path) {
        if (method == null || path == null) return false;
        switch (path) {
            case "/link/info": return "GET".equals(method);
            case "/link/hello":
            case "/link/do":
            case "/link/revoke": return "POST".equals(method);
            default: return false;
        }
    }

    /** Only /link/info goes unsigned: it is how the phone learns which desk it is talking to. */
    static boolean signed(String path) {
        return !"/link/info".equals(path);
    }

    /** The signed hello JSON, as the header carries it: base64url of its UTF-8, no padding. */
    static String helloHeader(String helloJson) {
        return Base64.getUrlEncoder().withoutPadding()
                .encodeToString(helloJson.getBytes(StandardCharsets.UTF_8));
    }

    static boolean validDeskId(String deskId) {
        return deskId != null && deskId.matches("[A-Za-z0-9._:-]{1,96}");
    }

    /** What the hello is for. Exactly this; the signature covers it lower-cased. */
    static String audience(String deskId) {
        return "desk-link:" + deskId;
    }

    /** The bundled asset a signer-page URL names, or null: the holder's whitelist, same origin. */
    static String assetName(String url) {
        if (url == null || !url.startsWith(ORIGIN)) return null;
        String name = url.substring(ORIGIN.length());
        int q = name.indexOf('?');
        if (q >= 0) name = name.substring(0, q);
        return name.matches("[a-z0-9-]+\\.(html|js)") ? name : null;
    }

    // ------------------------------------------------------------ transports
    // One session, three ways to the desk, tried in this order. The calls, the signed hello
    // and the desk's allowlist are identical on all three; only the URL differs.

    /** The field /link/info names the desk by (phone-link.cjs pins the same name). */
    static final String DESK_ID_FIELD = "desk_id";
    static final int MESH_PORT = 47938;
    static final String REMOTE_BASE = "https://tunnel.aitherium.com/tunnel/desk-link/";
    static final String[] TRANSPORTS = {"cable", "mesh", "remote"};

    /** A tailnet (100.64.0.0/10) IPv4, as the desk reports it. Anything else is refused. */
    static boolean validMeshAddr(String a) {
        if (a == null || !a.matches("100\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}")) return false;
        String[] p = a.split("\\.");
        for (String x : p) if (Integer.parseInt(x) > 255) return false;
        int second = Integer.parseInt(p[1]);
        return second >= 64 && second <= 127;
    }

    /** The URL for one call on one transport, or null when that transport has no address. */
    static String url(String transport, String meshAddr, String path) {
        if ("cable".equals(transport)) return BASE + path;
        if (!validMeshAddr(meshAddr)) return null;
        if ("mesh".equals(transport)) return "http://" + meshAddr + ":" + MESH_PORT + path;
        if ("remote".equals(transport)) return REMOTE_BASE + meshAddr + path;
        return null;
    }
}
