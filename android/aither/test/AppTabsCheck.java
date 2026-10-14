package com.aitherium.aither;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/** Exit 0 when the app shell's tabs, link routing and back rules are what the design says. */
public class AppTabsCheck {
    static int bad = 0;

    static void eq(String what, Object got, Object want) {
        if (!String.valueOf(got).equals(String.valueOf(want))) {
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
            bad++;
        }
    }

    static List<String> ids(String[][] rows) {
        List<String> out = new ArrayList<>();
        for (String[] r : rows) out.add(r[0]);
        return out;
    }

    static void route(boolean child, String path, String want) {
        eq((child ? "child " : "adult ") + path, AppTabs.route(AppTabs.ORIGIN + path, child), want);
    }

    public static void main(String[] a) {
        // the bars, by role
        eq("adult tabs", ids(AppTabs.tabs(false)), Arrays.asList("home", "learn", "family", "aither", "settings"));
        // a child's bar is ONE tab, the classroom (kids-suite S4)
        eq("child tabs", ids(AppTabs.tabs(true)), Arrays.asList("learn"));
        eq("adult home", AppTabs.homeTab(false), "home");
        eq("child home", AppTabs.homeTab(true), "learn");
        eq("home is native", AppTabs.isNative(AppTabs.tab(false, "home")), true);
        eq("settings is native", AppTabs.isNative(AppTabs.tab(false, "settings")), true);
        eq("no child settings tab", AppTabs.tab(true, "settings"), null);
        eq("learn root", AppTabs.rootUrl(false, "learn"), AppTabs.ORIGIN + "/learn/parent/");
        eq("no root for home", AppTabs.rootUrl(false, "home"), "");
        // a child's bar and grid never reach a grown-up surface
        for (String[][] rows : new String[][][] {AppTabs.CHILD_TABS, AppTabs.CHILD_APPS}) {
            for (String[] r : rows) {
                for (String s : r) {
                    if (s.contains("?app=") || s.contains("/learn/parent") || s.startsWith("/chat")
                            || s.contains("shell=")) {
                        System.out.println("FAIL child row " + r[0] + " reaches " + s);
                        bad++;
                    }
                }
            }
        }
        // every app opens in a tab of its own role (or a window, or Settings)
        for (boolean child : new boolean[] {false, true}) {
            for (String[] app : AppTabs.apps(child)) {
                if (!app[5].isEmpty() && AppTabs.tab(child, app[5]) == null) {
                    System.out.println("FAIL app " + app[0] + " opens in missing tab " + app[5]);
                    bad++;
                }
                if (app[5].isEmpty() && app[3].isEmpty()) {
                    System.out.println("FAIL app " + app[0] + " has neither a tab nor a page");
                    bad++;
                }
            }
        }

        // the Home grid per phone: Media Forge for the platform owner only, no Shop on Play
        eq("member grid", ids(AppTabs.apps(false, false, false)).contains("mediaforge"), false);
        eq("owner grid", ids(AppTabs.apps(false, true, false)).contains("mediaforge"), true);
        // a customer makes images in Image Studio (credits), offered to members and the owner
        eq("member image studio", ids(AppTabs.apps(false, false, false)).contains("image-studio"), true);
        eq("owner image studio", ids(AppTabs.apps(false, true, false)).contains("image-studio"), true);
        eq("play image studio", ids(AppTabs.apps(false, false, true)).contains("image-studio"), true);
        eq("child image studio", ids(AppTabs.apps(true, true, false)).contains("image-studio"), false);
        route(false, "/?app=image-studio", "window https://app.aitherium.com/?app=image-studio");
        eq("play grid", ids(AppTabs.apps(false, true, true)).contains("shop"), false);
        eq("direct grid", ids(AppTabs.apps(false, false, false)).contains("shop"), true);
        for (String id : new String[] {"agents", "packs", "mediaforge", "shop"}) {
            eq("child grid " + id, ids(AppTabs.apps(true, true, false)).contains(id), false);
        }
        // a child's helpers: only the agents a grown-up granted (kid lanes), in the Learn tab
        eq("child grid helpers", ids(AppTabs.apps(true, false, true)).contains("helpers"), true);
        route(true, "/learn/agents", "tab learn " + AppTabs.ORIGIN + "/learn/agents");
        eq("platform owner", AppTabs.platformOwner(Arrays.asList("owner"), "platform"), true);
        eq("customer owner", AppTabs.platformOwner(Arrays.asList("owner", "admin"), "tnt_acme"), false);
        eq("super admin", AppTabs.platformOwner(Arrays.asList("Super_Admin"), "tnt_acme"), true);
        eq("member", AppTabs.platformOwner(Arrays.asList("member"), "platform"), false);

        // links: grown-up
        route(false, "/", "tab home");
        route(false, "", "tab home");
        route(false, "/?app=learn", "tab learn");
        route(false, "/?app=family", "tab family");
        route(false, "/?app=greeter", "tab aither");
        route(false, "/?app=hearth", "window " + AppTabs.ORIGIN + "/?app=hearth");
        route(false, "/?app=sprite", "window " + AppTabs.ORIGIN + "/?app=sprite");
        route(false, "/?app=academy", "window " + AppTabs.ORIGIN + "/?app=academy");
        route(false, "/learn/parent/", "tab learn");
        route(false, "/learn/parent", "tab learn");
        route(false, "/learn/join?code=AB12", "tab learn " + AppTabs.ORIGIN + "/learn/join?code=AB12");
        route(false, "/family", "tab family");
        route(false, "/hearth/", "tab family " + AppTabs.ORIGIN + "/hearth/");
        route(false, "/hearth#room", "tab family " + AppTabs.ORIGIN + "/hearth#room");
        route(false, "/family/join?c=1", "tab family " + AppTabs.ORIGIN + "/family/join?c=1");
        route(false, "/chat", "tab aither");
        route(false, "/chat/x", "tab aither " + AppTabs.ORIGIN + "/chat/x");
        route(false, "/classroom/", "window " + AppTabs.ORIGIN + "/classroom/");
        route(false, "/#local-pair=t", "window " + AppTabs.ORIGIN + "/#local-pair=t");
        route(false, "/learnmore", "window " + AppTabs.ORIGIN + "/learnmore");
        eq("apex", AppTabs.route("https://aitherium.com/", false), "tab home");
        eq("garbage", AppTabs.route("http://[bad", false), "tab home");

        // links: child (the launcher's child set and the PWA's ?app= ids)
        // every child page opens in the one classroom tab
        route(true, "/", "tab learn");
        route(true, "/learn", "tab learn");
        route(true, "/learn?talk=1", "tab learn " + AppTabs.ORIGIN + "/learn?talk=1");
        route(true, "/learn/sprite", "tab learn " + AppTabs.ORIGIN + "/learn/sprite");
        route(true, "/learn/sprite#quests", "tab learn " + AppTabs.ORIGIN + "/learn/sprite#quests");
        route(true, "/hearth#room", "tab learn " + AppTabs.ORIGIN + "/hearth#room");
        route(true, "/hearth", "tab learn " + AppTabs.ORIGIN + "/hearth");
        route(true, "/?app=hearth", "tab learn");
        route(true, "/?app=quests", "tab learn " + AppTabs.ORIGIN + "/learn/sprite#quests");
        route(true, "/?app=room", "tab learn " + AppTabs.ORIGIN + "/hearth#room");
        route(true, "/?app=family", "tab learn " + AppTabs.ORIGIN + "/hearth");
        route(true, "/?app=avatar", "tab learn " + AppTabs.ORIGIN + "/learn/sprite");
        route(true, "/?app=control", "tab learn");
        route(true, "/classroom/x", "window " + AppTabs.ORIGIN + "/classroom/x");
        // the Nest tile is the child's own Sprite, never Aither's general chat
        for (String[] app : AppTabs.CHILD_APPS) {
            if (app[0].equals("nest")) eq("nest tile", app[3], "/learn?talk=1");
        }

        // a link outside aitherium.com: the browser for a grown-up; never for a child
        eq("adult off-site", AppTabs.link(false, "https://example.com/x"), AppTabs.Link.BROWSER);
        eq("child off-site", AppTabs.link(true, "https://example.com/x"), AppTabs.Link.ASK_GROWNUP);
        eq("child http", AppTabs.link(true, "http://app.aitherium.com/learn"), AppTabs.Link.ASK_GROWNUP);
        eq("child lookalike", AppTabs.link(true, "https://aitherium.com.evil.example/"), AppTabs.Link.ASK_GROWNUP);
        eq("child lookalike 2", AppTabs.link(true, "https://evilaitherium.com/"), AppTabs.Link.ASK_GROWNUP);
        eq("child mailto", AppTabs.link(true, "mailto:x@example.com"), AppTabs.Link.ASK_GROWNUP);
        eq("child intent", AppTabs.link(true, "intent://scan/#Intent;scheme=zxing;end"), AppTabs.Link.ASK_GROWNUP);
        eq("child garbage", AppTabs.link(true, "http://[bad"), AppTabs.Link.ASK_GROWNUP);
        eq("child ours", AppTabs.link(true, AppTabs.ORIGIN + "/learn"), AppTabs.Link.IN_APP);
        eq("child apex", AppTabs.link(true, "https://aitherium.com/"), AppTabs.Link.IN_APP);
        eq("adult ours", AppTabs.link(false, "https://api.aitherium.com/x"), AppTabs.Link.IN_APP);
        // a child's phone opens only the classroom's own hosts: never the shop or a tenant site
        eq("child shop", AppTabs.link(true, "https://shop.aitherium.com/"), AppTabs.Link.ASK_GROWNUP);
        eq("child tenant", AppTabs.link(true, "https://acme.aitherium.com/"), AppTabs.Link.ASK_GROWNUP);
        eq("child blog", AppTabs.link(true, "https://blog.aitherium.com/x"), AppTabs.Link.ASK_GROWNUP);
        eq("child sign-in", AppTabs.link(true, "https://idp.aitherium.com/login"), AppTabs.Link.IN_APP);
        eq("adult shop", AppTabs.link(false, "https://shop.aitherium.com/"), AppTabs.Link.IN_APP);
        // "I told your grown-up" only when the server stored the card
        String told = "That page is outside Aither. I told your grown-up, so you can look at it together.";
        eq("told 201", AppTabs.askMessage(201, "{\"host\":\"x.org\",\"card_sent\":true,\"told\":true}"), told);
        eq("told spaced", AppTabs.askMessage(201, "{\"told\": true}"), told);
        for (int st : new int[] {-1, 409, 429, 503, 401, 500}) {
            if (AppTabs.askMessage(st, "{\"told\":true}").contains("I told")) {
                System.out.println("FAIL ask " + st + " claims a grown-up was told"); bad++;
            }
        }
        if (AppTabs.askMessage(201, "{\"told\":false}").contains("I told")) {
            System.out.println("FAIL ask told:false claims a grown-up was told"); bad++;
        }
        if (AppTabs.ASKING.contains("told")) { System.out.println("FAIL asking claims told"); bad++; }
        // what the grown-up's card carries: scheme, host, path -- never the query or fragment
        eq("ask body", AppTabs.linkRequestBody("https://User:pw@Example.com/a/b?token=s3cret#frag"),
                "{\"url\":\"https://example.com/a/b\"}");
        eq("ask body unparseable", AppTabs.linkRequestBody("https://example.com/a\"b"), "");
        eq("ask body mailto", AppTabs.linkRequestBody("mailto:x@example.com"), "");
        eq("ask body garbage", AppTabs.linkRequestBody("http://[bad"), "");
        eq("ask body cap", AppTabs.linkRequestBody("https://example.com/" + "a".repeat(400)).length(), 300 + 10);
        // the launcher shortcuts (Shortcuts.java rows, passed in as c:/path or a:/path): a
        // child's opens one of their tabs, a grown-up's never just lands on Home
        for (String arg : a) {
            boolean child = arg.startsWith("c:");
            AppTabs.Route r = AppTabs.route(AppTabs.ORIGIN + arg.substring(2), child);
            if (child && r.window) { System.out.println("FAIL child shortcut " + arg + " opens a window"); bad++; }
            if (!child && r.tab.equals("home")) { System.out.println("FAIL shortcut " + arg + " lands Home"); bad++; }
        }

        // the tab root
        eq("root slash", AppTabs.isRoot(AppTabs.ORIGIN + "/learn/parent", "/learn/parent/"), true);
        eq("root fragment", AppTabs.isRoot(AppTabs.ORIGIN + "/hearth#x", "/hearth#room"), true);
        eq("root deeper", AppTabs.isRoot(AppTabs.ORIGIN + "/learn/parent/kid", "/learn/parent/"), false);
        eq("root query", AppTabs.isRoot(AppTabs.ORIGIN + "/chat?x=1", "/chat"), false);
        eq("root null", AppTabs.isRoot(null, "/chat"), false);

        // Android back: window, page history, tab root, Home, leave
        eq("back window page", AppTabs.back(true, true, true, false, false), AppTabs.Back.WINDOW_BACK);
        eq("back window close", AppTabs.back(true, false, true, false, false), AppTabs.Back.WINDOW_CLOSE);
        eq("back web", AppTabs.back(false, false, true, false, false), AppTabs.Back.WEB_BACK);
        eq("back to root", AppTabs.back(false, false, false, false, false), AppTabs.Back.TAB_ROOT);
        eq("back to home", AppTabs.back(false, false, false, true, false), AppTabs.Back.HOME);
        eq("back exits", AppTabs.back(false, false, false, true, true), AppTabs.Back.EXIT);
        eq("home with history", AppTabs.back(false, false, true, false, true), AppTabs.Back.WEB_BACK);

        // the one sign-in page comes back to the page you were on, never to itself
        eq("sign in from learn", AppTabs.signInUrl(AppTabs.ORIGIN + "/learn/parent/"),
                AppTabs.ORIGIN + "/login?redirect=%2Flearn%2Fparent%2F");
        eq("sign in keeps query", AppTabs.signInUrl(AppTabs.ORIGIN + "/?app=hearth"),
                AppTabs.ORIGIN + "/login?redirect=%2F%3Fapp%3Dhearth");
        eq("sign in from home", AppTabs.signInUrl(null), AppTabs.ORIGIN + "/login");
        eq("sign in from sign in", AppTabs.signInUrl(AppTabs.ORIGIN + "/login?redirect=/chat"), AppTabs.ORIGIN + "/login");
        eq("sign in elsewhere", AppTabs.signInUrl("https://evil.example/x"), AppTabs.ORIGIN + "/login");
        eq("is sign in", AppTabs.isSignIn(AppTabs.ORIGIN + "/login?redirect=%2Fchat"), true);
        eq("not sign in", AppTabs.isSignIn(AppTabs.ORIGIN + "/learn/parent/"), false);

        System.out.println(bad == 0 ? "OK" : bad + " FAILED");
        System.exit(bad == 0 ? 0 : 1);
    }
}
