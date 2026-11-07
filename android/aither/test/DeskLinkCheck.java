package com.aitherium.aither;

import java.nio.charset.StandardCharsets;
import java.util.Base64;

/**
 * DeskLink (what the page may send down the USB cable, and how the hello is carried) on a
 * desktop JVM.
 *   javac -d out src/com/aitherium/aither/DeskLink.java test/DeskLinkCheck.java
 *   java -cp out com.aitherium.aither.DeskLinkCheck
 */
public final class DeskLinkCheck {
    private static int fails;

    private static void eq(String what, Object got, Object want) {
        boolean ok = want == null ? got == null : want.equals(got);
        if (!ok) {
            fails++;
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
        }
    }

    public static void main(String[] args) {
        eq("port", DeskLink.BASE, "http://127.0.0.1:47937");
        // the four calls
        eq("info", DeskLink.pathAllowed("GET", "/link/info"), true);
        eq("hello", DeskLink.pathAllowed("POST", "/link/hello"), true);
        eq("do", DeskLink.pathAllowed("POST", "/link/do"), true);
        eq("revoke", DeskLink.pathAllowed("POST", "/link/revoke"), true);
        // everything else
        eq("do via GET", DeskLink.pathAllowed("GET", "/link/do"), false);
        eq("info via POST", DeskLink.pathAllowed("POST", "/link/info"), false);
        eq("lower method", DeskLink.pathAllowed("post", "/link/do"), false);
        eq("commands", DeskLink.pathAllowed("POST", "/commands/run"), false);
        eq("dotdot", DeskLink.pathAllowed("POST", "/link/../commands/run"), false);
        eq("dotdot tail", DeskLink.pathAllowed("POST", "/link/do/.."), false);
        eq("query", DeskLink.pathAllowed("GET", "/link/info?x=1"), false);
        eq("trailing slash", DeskLink.pathAllowed("POST", "/link/do/"), false);
        eq("no slash", DeskLink.pathAllowed("POST", "link/do"), false);
        eq("absolute url", DeskLink.pathAllowed("POST", "http://evil.example/link/do"), false);
        eq("put", DeskLink.pathAllowed("PUT", "/link/do"), false);
        eq("null", DeskLink.pathAllowed(null, null), false);
        eq("info unsigned", DeskLink.signed("/link/info"), false);
        eq("do signed", DeskLink.signed("/link/do"), true);

        // header: base64url, no padding, round trip (incl. non-ASCII and chars that differ from base64)
        String hello = "{\"auth\":\"device\",\"device_id\":\"kvh-é\",\"relay\":\"desk-link:pc-1\",\"sig\":\"??>>\"}";
        String h = DeskLink.helloHeader(hello);
        eq("no padding", h.contains("="), false);
        eq("url alphabet", h.matches("[A-Za-z0-9_-]+"), true);
        eq("round trip", new String(Base64.getUrlDecoder().decode(h), StandardCharsets.UTF_8), hello);
        eq("known", DeskLink.helloHeader("??>"), "Pz8-");

        // desk ids and audience
        eq("desk ok", DeskLink.validDeskId("Desk-PC_1.local:0"), true);
        eq("desk 96", DeskLink.validDeskId("a".repeat(96)), true);
        eq("desk 97", DeskLink.validDeskId("a".repeat(97)), false);
        eq("desk empty", DeskLink.validDeskId(""), false);
        eq("desk null", DeskLink.validDeskId(null), false);
        eq("desk slash", DeskLink.validDeskId("pc/1"), false);
        eq("desk space", DeskLink.validDeskId("pc 1"), false);
        eq("desk newline", DeskLink.validDeskId("pc1\n"), false);
        eq("audience keeps case", DeskLink.audience("Desk-PC"), "desk-link:Desk-PC");

        // the signer page: the holder's whitelist on the holder's origin
        eq("asset page", DeskLink.assetName("https://appassets.androidplatform.net/desk-link.html"), "desk-link.html");
        eq("asset query", DeskLink.assetName("https://appassets.androidplatform.net/kvdevice.js?v=2"), "kvdevice.js");
        eq("asset dir", DeskLink.assetName("https://appassets.androidplatform.net/../x.html"), null);
        eq("asset other", DeskLink.assetName("https://evil.example/desk-link.html"), null);
        eq("asset type", DeskLink.assetName("https://appassets.androidplatform.net/app.json"), null);

        // the /link/info field (phone-link.cjs serves exactly this name)
        eq("desk id field", DeskLink.DESK_ID_FIELD, "desk_id");
        // three transports, one set of calls
        eq("order", String.join(",", DeskLink.TRANSPORTS), "cable,mesh,remote");
        eq("cable url", DeskLink.url("cable", null, "/link/do"), "http://127.0.0.1:47937/link/do");
        eq("mesh url", DeskLink.url("mesh", "100.64.0.31", "/link/do"), "http://100.64.0.31:47938/link/do");
        eq("remote url", DeskLink.url("remote", "100.64.0.31", "/link/hello"),
                "https://tunnel.aitherium.com/tunnel/desk-link/100.64.0.31/link/hello");
        eq("no mesh addr", DeskLink.url("mesh", "", "/link/do"), null);
        eq("lan addr", DeskLink.url("remote", "192.168.1.5", "/link/do"), null);
        eq("not tailnet", DeskLink.validMeshAddr("100.128.0.1"), false);
        eq("octet range", DeskLink.validMeshAddr("100.64.0.300"), false);
        eq("injected", DeskLink.validMeshAddr("100.64.0.31/../../x"), false);
        eq("unknown transport", DeskLink.url("bluetooth", "100.64.0.31", "/link/do"), null);

        if (fails > 0) {
            System.out.println(fails + " FAILED");
            System.exit(1);
        }
        System.out.println("DeskLinkCheck: all ok");
    }
}
