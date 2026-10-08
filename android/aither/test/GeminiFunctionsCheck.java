package com.aitherium.aither;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Exit 0 when Aither's app functions (what Gemini may ask) follow the policy: the safe four
 * only, read-only on a child's phone, nothing that runs an action, and the hand-written index
 * files in assets/ (args: app_functions.xml aither_functions.xml) name exactly those ids.
 */
public class GeminiFunctionsCheck {
    static int bad = 0;

    static void eq(String what, Object got, Object want) {
        if (!String.valueOf(got).equals(String.valueOf(want))) {
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
            bad++;
        }
    }

    static List<String> ids(String xml, String tag) {
        List<String> out = new ArrayList<>();
        Matcher m = Pattern.compile("<" + tag + ">([^<]*)</" + tag + ">").matcher(xml);
        while (m.find()) out.add(m.group(1));
        return out;
    }

    public static void main(String[] a) throws Exception {
        List<String> all = Arrays.asList(GeminiFunctions.ALL);
        // who may run what
        for (String id : all) eq("adult may run " + id, GeminiFunctions.allowed(id, false), true);
        eq("child askAither", GeminiFunctions.allowed(GeminiFunctions.ASK_AITHER, true), false);
        eq("child askHearth", GeminiFunctions.allowed(GeminiFunctions.ASK_HEARTH, true), false);
        eq("child openApp", GeminiFunctions.allowed(GeminiFunctions.OPEN_APP, true), true);
        eq("child pendingApprovals", GeminiFunctions.allowed(GeminiFunctions.PENDING_APPROVALS, true), true);
        eq("unknown id", GeminiFunctions.allowed(GeminiFunctions.PREFIX + "approve", false), false);
        eq("null id", GeminiFunctions.allowed(null, false), false);
        // no function approves, denies, runs or sends anything
        for (String id : all) {
            String n = id.substring(GeminiFunctions.PREFIX.length()).toLowerCase();
            for (String verb : new String[] {"approve", "deny", "decide", "run", "send", "execute", "delete"}) {
                if (n.contains(verb)) {
                    System.out.println("FAIL function " + id + " looks like it acts (" + verb + ")");
                    bad++;
                }
            }
        }
        // openApp: the three apps, a child gets only their own pages
        eq("adult learn", GeminiFunctions.appUrl("Learn ", false), AppTabs.ORIGIN + "/?app=learn");
        eq("adult family", GeminiFunctions.appUrl("family", false), AppTabs.ORIGIN + "/?app=family");
        eq("adult hearth", GeminiFunctions.appUrl("hearth", false), AppTabs.ORIGIN + "/?app=hearth");
        eq("child learn", GeminiFunctions.appUrl("learn", true), AppTabs.ORIGIN + "/learn");
        eq("child family", GeminiFunctions.appUrl("family", true), null);
        eq("child hearth", GeminiFunctions.appUrl("hearth", true), AppTabs.ORIGIN + "/hearth");
        eq("unknown app", GeminiFunctions.appUrl("settings", false), null);
        eq("url as app", GeminiFunctions.appUrl("https://evil.example/", false), null);
        // where the app shell takes each one (AppTabs.route): the app itself, never Home by accident
        eq("route adult learn", AppTabs.route(GeminiFunctions.appUrl("learn", false), false), "tab learn");
        eq("route adult family", AppTabs.route(GeminiFunctions.appUrl("family", false), false), "tab family");
        eq("route adult hearth", AppTabs.route(GeminiFunctions.appUrl("hearth", false), false),
                "window " + AppTabs.ORIGIN + "/?app=hearth");
        eq("route child learn", AppTabs.route(GeminiFunctions.appUrl("learn", true), true), "tab learn");
        eq("route child hearth", AppTabs.route(GeminiFunctions.appUrl("hearth", true), true), "tab space");
        // the Hearth draft survives the shell: it opens as a window on the full link
        String draft = GeminiFunctions.hearthUrl("water the plants", false);
        eq("route hearth draft", AppTabs.route(draft, false), "window " + draft);
        // askHearth: a draft in Hearth's box, never on a child's phone, clipped, encoded
        eq("hearth draft", GeminiFunctions.hearthUrl("turn off the porch light", false),
                AppTabs.ORIGIN + "/?app=hearth&ask=turn%20off%20the%20porch%20light");
        eq("hearth on child's phone", GeminiFunctions.hearthUrl("anything", true), null);
        eq("hearth empty", GeminiFunctions.hearthUrl(" \n ", false), null);
        String injected = GeminiFunctions.hearthUrl("x&app=settings#y", false);
        eq("hearth encodes", injected.endsWith("ask=x%26app%3Dsettings%23y"), true);
        StringBuilder huge = new StringBuilder();
        while (huge.length() < 5000) huge.append("abcdefghij");
        eq("hearth clipped", GeminiFunctions.clean(huge.toString(), GeminiFunctions.MAX_HEARTH).length(), GeminiFunctions.MAX_HEARTH);
        eq("control chars", GeminiFunctions.clean("a\u0000b\u001bc\nd", 100), "a b c d");
        // pendingApprovals reads a count, says how fresh it is
        eq("never checked", GeminiFunctions.approvalsLine(0, -1, false).contains("not checked"), true);
        eq("none", GeminiFunctions.approvalsLine(0, 0, false), "Nothing is waiting for your approval (checked just now).");
        eq("two", GeminiFunctions.approvalsLine(2, 5, false).startsWith("2 requests are waiting for your approval (checked 5 min ago)"), true);
        eq("child", GeminiFunctions.approvalsLine(1, 5, true).contains("a grown-up's"), true);
        // the hand-written index files name exactly these ids
        if (a.length == 2) {
            String v1 = new String(Files.readAllBytes(Paths.get(a[0])), StandardCharsets.UTF_8);
            String v2 = new String(Files.readAllBytes(Paths.get(a[1])), StandardCharsets.UTF_8);
            eq("v1 ids", ids(v1, "function_id"), all);
            eq("v2 ids", ids(v2, "id").stream().filter(s -> !"unused".equals(s)).collect(java.util.stream.Collectors.toList()), all);
            eq("v2 functionIds", ids(v2, "functionId"), all);
            for (String p : new String[] {"prompt", "app", "request"}) {
                eq("v2 declares " + p, ids(v2, "name").contains(p), true);
            }
        } else {
            System.out.println("FAIL pass app_functions.xml and aither_functions.xml");
            bad++;
        }
        if (bad > 0) {
            System.out.println(bad + " check(s) failed");
            System.exit(1);
        }
        System.out.println("GeminiFunctionsCheck: ok");
    }
}
