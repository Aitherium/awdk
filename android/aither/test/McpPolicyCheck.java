package com.aitherium.aither;

import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;

/** Exit 0 when McpPolicy offers the phone's agent only allowed tools of an aitherium.com page. */
public class McpPolicyCheck {
    static int bad;

    static void expect(boolean ok, String what) {
        if (!ok) { System.out.println("FAIL " + what); bad++; }
    }

    public static void main(String[] a) {
        String[][] access = {
            {"get_site_overview", "READ"}, {"search_site", "READ"}, {"read_page", "READ"},
            {"page_read_dom", "READ"}, {"page_read_selection", "READ"}, {"web_search", "READ"},
            {"page_context", "READ"}, {"get_thread", "READ"},
            {"page_fill_form", "CONFIRM"}, {"page_storage_get", "CONFIRM"}, {"page_download", "CONFIRM"},
            {"page_clipboard_read", "CONFIRM"}, {"page_clipboard_write", "CONFIRM"},
            {"navigate_to", "CONFIRM"}, {"speak", "CONFIRM"}, {"deep_research", "CONFIRM"},
            {"studio_delete_everything", "NONE"}, {"calendar_read", "NONE"}, {"", "NONE"},
            {"Page_Read_Dom", "NONE"}, {"page_read_dom\n", "NONE"}, {"x".repeat(65), "NONE"},
        };
        for (String[] c : access) {
            expect(McpPolicy.access(c[0]).name().equals(c[1]), "access " + c[0] + " -> " + McpPolicy.access(c[0]));
        }
        expect(McpPolicy.access(null) == McpPolicy.Access.NONE, "access null");
        // the read and confirm lists never overlap, and no write tool is read-only
        for (String n : McpPolicy.READ) expect(!McpPolicy.CONFIRM.contains(n), "both lists: " + n);
        for (String n : new String[] {"page_fill_form", "page_storage_get", "page_download",
                "page_clipboard_read", "page_clipboard_write"}) {
            expect(!McpPolicy.READ.contains(n), "write tool runs unasked: " + n);
        }

        String[][] pages = {
            {"https://app.aitherium.com/search", "true"},
            {"https://aitherium.com/", "true"},
            {"https://app.aitherium.com:443/x", "true"},
            {"http://app.aitherium.com/", "false"},
            {"https://app.aitherium.com.evil.com/", "false"},
            {"https://evilaitherium.com/", "false"},
            {"https://user@app.aitherium.com/", "false"},
            {"https://app.aitherium.com:8443/", "false"},
            {"file:///sdcard/x.html", "false"},
            {"javascript:alert(1)", "false"},
            {"about:blank", "false"},
            {"", "false"},
        };
        for (String[] c : pages) {
            expect(McpPolicy.ourPage(c[0]) == Boolean.parseBoolean(c[1]), "ourPage " + c[0]);
        }
        expect(!McpPolicy.ourPage(null), "ourPage null");

        Set<String> reg = new HashSet<>(Arrays.asList("page_read_dom", "page_fill_form", "calendar_read", "made_up"));
        String ok = "https://app.aitherium.com/";
        expect(McpPolicy.callable("page_read_dom", reg, ok), "registered read tool");
        expect(McpPolicy.callable("page_fill_form", reg, ok), "registered confirm tool");
        expect(!McpPolicy.callable("read_page", reg, ok), "a tool the page did not register");
        expect(!McpPolicy.callable("made_up", reg, ok), "a registered tool not on either list");
        expect(!McpPolicy.callable("calendar_read", reg, ok), "a page shadowing a native tool");
        expect(!McpPolicy.callable("page_read_dom", reg, "https://evil.com/"), "a page off our origin");
        expect(!McpPolicy.callable("page_read_dom", null, ok), "no registry");

        expect(McpPolicy.result("hi").equals("hi"), "short result");
        expect(McpPolicy.result(null).startsWith("("), "empty result");
        String big = McpPolicy.result("y".repeat(10_000));
        expect(big.startsWith("y".repeat(McpPolicy.MAX_RESULT)) && big.endsWith("characters]")
                && big.length() < McpPolicy.MAX_RESULT + 60, "clipped result announces the cut");
        expect(McpPolicy.clip("abcdef", 3).equals("abc") && McpPolicy.clip(null, 3).isEmpty(), "clip");

        System.out.println(bad == 0 ? "OK" : bad + " FAILED");
        System.exit(bad == 0 ? 0 : 1);
    }
}
