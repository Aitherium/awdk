package com.aitherium.aither;

import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;

/**
 * DeviceLink (the code a scanned QR or a typed box carries, and who may approve it) and
 * Qr (the watch's code), on a desktop JVM. With a directory argument it also writes each
 * QR as a PGM so dev/tests can decode it with OpenCV: an encoder that only looks right
 * proves nothing.
 *   javac -d out src/.../DeviceLink.java src/.../Qr.java test/DeviceLinkCheck.java
 *   java -cp out com.aitherium.aither.DeviceLinkCheck [pgm-dir]
 */
public final class DeviceLinkCheck {
    private static int fails;

    private static void eq(String what, Object got, Object want) {
        boolean ok = want == null ? got == null : want.equals(got);
        if (!ok) {
            fails++;
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
        }
    }

    static final String[] QR_TEXTS = {
        "https://app.aitherium.com/auth/device?code=U585-J6ZD",
        "https://app.aitherium.com/auth/device?code=ABCD-2345",
        "HELLO",
        "https://idp.aitherium.com/link?user_code=WXYZ-9876&next=%2Fhearth%2F&from=watch-pixel-4",
    };

    public static void main(String[] args) throws IOException {
        // codes from what a person hands over
        eq("bare", DeviceLink.codeFrom("u585-j6zd"), "U585-J6ZD");
        eq("no dash", DeviceLink.codeFrom("U585J6ZD"), "U585-J6ZD");
        eq("spaces", DeviceLink.codeFrom(" u585 j6zd "), "U585-J6ZD");
        eq("qr link", DeviceLink.codeFrom("https://app.aitherium.com/auth/device?code=U585-J6ZD"), "U585-J6ZD");
        eq("idp link", DeviceLink.codeFrom("https://idp.aitherium.com/link?user_code=u585-j6zd"), "U585-J6ZD");
        eq("idp link encoded", DeviceLink.codeFrom("https://idp.aitherium.com/link?user_code=U585%2DJ6ZD"), "U585-J6ZD");
        eq("app scheme", DeviceLink.codeFrom("aither://link?code=U585J6ZD"), "U585-J6ZD");
        eq("trailing slash", DeviceLink.codeFrom("https://app.aitherium.com/auth/device/?code=U585-J6ZD"), "U585-J6ZD");
        // refused: not ours, wrong page, bad code
        eq("foreign host", DeviceLink.codeFrom("https://evil.example/auth/device?code=U585-J6ZD"), null);
        eq("lookalike host", DeviceLink.codeFrom("https://aitherium.com.evil.example/link?user_code=U585-J6ZD"), null);
        eq("port", DeviceLink.codeFrom("https://app.aitherium.com:8443/auth/device?code=U585-J6ZD"), null);
        eq("http", DeviceLink.codeFrom("http://app.aitherium.com/auth/device?code=U585-J6ZD"), null);
        eq("other page", DeviceLink.codeFrom("https://app.aitherium.com/hearth?code=U585-J6ZD"), null);
        eq("short", DeviceLink.codeFrom("U585-J6Z"), null);
        eq("symbols", DeviceLink.codeFrom("U585-J6Z!"), null);
        eq("empty", DeviceLink.codeFrom("  "), null);
        eq("null", DeviceLink.codeFrom(null), null);
        eq("link url", DeviceLink.linkUrl("u585j6zd"), "https://app.aitherium.com/auth/device?code=U585-J6ZD");
        eq("link round trip", DeviceLink.codeFrom(DeviceLink.linkUrl("ABCD-2345")), "ABCD-2345");
        // who approves
        eq("owner may", DeviceLink.childRefusal("owner", false), null);
        eq("unknown may", DeviceLink.childRefusal("", false), null);
        eq("child kind refused", DeviceLink.childRefusal("child", false) != null, true);
        eq("child device refused", DeviceLink.childRefusal("", true) != null, true);
        eq("403 in words", DeviceLink.refusal(403, "").startsWith("A grown-up"), true);
        eq("404 in words", DeviceLink.refusal(404, "").contains("expired"), true);
        eq("approved name", DeviceLink.approved("Aither on Wear OS"), "Aither on Wear OS is signed in. It can take a few seconds to notice.");

        // QR: sizes are the standard's, and the grids are written for decoding
        eq("v1 size", Qr.encode("HELLO").length, 21);
        int v = (Qr.encode(QR_TEXTS[0]).length - 17) / 4;
        eq("link fits v4-M", v, 4);
        eq("v4-M capacity", Qr.dataCodewords(4), 64);
        eq("v10-M capacity", Qr.dataCodewords(10), 216);
        if (args.length > 0) {
            for (int i = 0; i < QR_TEXTS.length; i++) pgm(Qr.encode(QR_TEXTS[i]), args[0] + "/qr" + i + ".pgm");
        }
        if (fails > 0) {
            System.out.println(fails + " failed");
            System.exit(1);
        }
        System.out.println("DeviceLinkCheck OK");
    }

    /** The grid with a 4-module quiet zone, 8 px a module, as a binary PGM. */
    private static void pgm(boolean[][] m, String path) throws IOException {
        int scale = 8, quiet = 4, n = m.length, px = (n + 2 * quiet) * scale;
        try (FileOutputStream o = new FileOutputStream(path)) {
            o.write(("P5\n" + px + " " + px + "\n255\n").getBytes(StandardCharsets.US_ASCII));
            byte[] row = new byte[px];
            for (int y = 0; y < px; y++) {
                int my = y / scale - quiet;
                for (int x = 0; x < px; x++) {
                    int mx = x / scale - quiet;
                    boolean dark = my >= 0 && my < n && mx >= 0 && mx < n && m[my][mx];
                    row[x] = (byte) (dark ? 0 : 255);
                }
                o.write(row);
            }
        }
    }
}
