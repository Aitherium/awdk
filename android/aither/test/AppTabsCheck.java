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

    static void nav(String what, boolean child, String url, boolean gesture, boolean redirect,
                    String tab, AppTabs.Nav want) {
        eq("nav " + what, AppTabs.navigate(child, url, gesture, redirect, tab), want);
    }

    static void route(boolean child, String path, String want) {
        eq((child ? "child " : "adult ") + path, AppTabs.route(AppTabs.ORIGIN + path, child), want);
    }

    static void os(String url, String want) {
        eq("os " + url, AppTabs.route(url, false), "tab os " + want);
    }

    public static void main(String[] a) {
        // a grown-up's phone IS the AitherOS desktop: ONE surface, no native bar (0.4.0)
        eq("adult surface", ids(AppTabs.tabs(false)), Arrays.asList("os"));
        eq("adult home", AppTabs.homeTab(false), "os");
        eq("os home", AppTabs.OS_HOME, "https://app.aitherium.com/?host=android-os");
        eq("os root", AppTabs.rootUrl(false, "os"), AppTabs.OS_HOME);
        eq("no adult tabs left", AppTabs.tab(false, "home") == null && AppTabs.tab(false, "family") == null
                && AppTabs.tab(false, "aither") == null && AppTabs.tab(false, "settings") == null, true);
        // the catalogue search still works (the screen checks read the catalogue)
        eq("search all", AppTabs.search(AppTabs.ADULT_APPS, "").length, AppTabs.ADULT_APPS.length);
        eq("search label", ids(AppTabs.search(AppTabs.ADULT_APPS, "hearth")), Arrays.asList("hearth"));
        eq("search words", ids(AppTabs.search(AppTabs.ADULT_APPS, "  IMAGE   credits ")), Arrays.asList("image-studio"));
        eq("search none", AppTabs.search(AppTabs.ADULT_APPS, "zzz").length, 0);
        // a child's bar is ONE tab, the classroom (kids-suite S4)
        eq("child tabs", ids(AppTabs.tabs(true)), Arrays.asList("learn"));
        eq("child home", AppTabs.homeTab(true), "learn");
        eq("no child settings tab", AppTabs.tab(true, "settings"), null);
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
        // every child app opens in the child's tab (or a window); a grown-up's open in the OS
        for (boolean child : new boolean[] {true}) {
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

        // ONE images tile per person (2026-10-18 app audit): the owner's Media Forge, everyone
        // else's Image Studio; never both, never neither
        eq("member grid", ids(AppTabs.apps(false, false, false)).contains("mediaforge"), false);
        eq("owner grid", ids(AppTabs.apps(false, true, false)).contains("mediaforge"), true);
        eq("member image studio", ids(AppTabs.apps(false, false, false)).contains("image-studio"), true);
        eq("owner image studio", ids(AppTabs.apps(false, true, false)).contains("image-studio"), false);
        for (boolean owner : new boolean[] {false, true}) {
            java.util.List<String> g = ids(AppTabs.apps(false, owner, false));
            eq("one images tile, owner=" + owner,
                    (g.contains("mediaforge") ? 1 : 0) + (g.contains("image-studio") ? 1 : 0), 1);
        }
        // My account opens the live settings page, never the retired /profile
        eq("account path", AppTabs.ACCOUNT_PATH, "/workspace/settings?tab=profile");
        eq("play image studio", ids(AppTabs.apps(false, false, true)).contains("image-studio"), true);
        eq("child image studio", ids(AppTabs.apps(true, true, false)).contains("image-studio"), false);
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

        // links: grown-up. Every one loads into the ONE WebView, the OS (never a window)
        String O = AppTabs.ORIGIN;
        String H = AppTabs.OS_HOME;
        os(O + "/", H);
        os(O, H);
        os("https://aitherium.com/", H);
        os("https://www.aitherium.com/", H);
        os("http://[bad", H);
        os(null, H);
        os("https://example.com/?app=hearth", H); // never someone else's page
        os("http://app.aitherium.com/chat", H); // never plain http
        // the OS's own ?app= ids, with host=android-os first and everything else kept
        os(O + "/?app=hearth", H + "&app=hearth");
        os(O + "/?app=learn", H + "&app=learn");
        os(O + "/?app=family", H + "&app=family");
        os(O + "/?app=greeter", H + "&app=greeter");
        os(O + "/?app=image-studio", H + "&app=image-studio");
        os(O + "/?app=agents&tab=mine", H + "&app=agents&tab=mine");
        os(O + "/?host=android&app=sprite", H + "&app=sprite"); // one host, ours
        os(O + "/?host=android-os&app=sprite", H + "&app=sprite");
        os(O + "/?shell=aither-desktop", H + "&shell=aither-desktop");
        os(O + "/#local-pair=t", H + "#local-pair=t");
        os("https://aitherium.com/?app=hearth", H + "&app=hearth");
        // a page that IS an OS app opens that app's window
        os(O + "/chat", H + "&app=aeon");
        os(O + "/learn", H + "&app=learn");
        os(O + "/learn/parent/", H + "&app=learn");
        os(O + "/learn/parent", H + "&app=learn");
        os(O + "/hearth/", H + "&app=family");
        os(O + "/family", H + "&app=family");
        os(O + "/classroom/", H + "&app=classroom");
        os(O + "/spaces/", H + "&app=spaces");
        os(O + "/shop", H + "&app=shop");
        // anything else on the app loads as itself: a query, a fragment or a deeper page is kept
        os(O + "/learn/join?code=AB12", O + "/learn/join?code=AB12");
        os(O + "/hearth#room", O + "/hearth#room");
        os(O + "/family/join?c=1", O + "/family/join?c=1");
        os(O + "/chat/x", O + "/chat/x");
        os(O + "/chat?mode=talk", O + "/chat?mode=talk");
        os(O + "/learnmore", O + "/learnmore");
        os(O + "/workspace/settings?tab=profile", O + "/workspace/settings?tab=profile");
        os("https://aitherium.com/shop/x", O + "/shop/x"); // the apex is a copy of the app
        os("https://docs.aitherium.com/x", "https://docs.aitherium.com/x");
        // every OS page id is unique per page, and every page is a real path
        java.util.Set<String> pages = new java.util.HashSet<>();
        for (String[] r : AppTabs.ADULT_OS_PAGES) {
            if (!pages.add(r[0])) { System.out.println("FAIL two OS pages: " + r[0]); bad++; }
            if (!r[0].startsWith("/") || r[0].endsWith("/")) { System.out.println("FAIL OS page " + r[0]); bad++; }
        }
        eq("is os page", AppTabs.isOsPage(H + "&app=hearth"), true);
        eq("is os page bare", AppTabs.isOsPage(O + "/"), true);
        eq("not os page", AppTabs.isOsPage(O + "/hearth#room"), false);
        eq("not os page apex", AppTabs.isOsPage("https://aitherium.com/"), false);
        eq("not os page null", AppTabs.isOsPage(null), false);

        // Back on the OS: only a literal true is handled; false, null, a timeout leave from
        // the OS, and a page that is not the OS walks its history or goes to the OS home
        eq("back js", AppTabs.OS_BACK_JS,
                "(window.aitherOs&&window.aitherOs.back)?window.aitherOs.back():false");
        eq("back timeout", AppTabs.OS_BACK_TIMEOUT_MS, 500);
        eq("handled true", AppTabs.osBackHandled("true"), true);
        eq("handled spaced", AppTabs.osBackHandled(" true\n"), true);
        for (String r : new String[] {"false", "null", null, "", "\"true\"", "1", "{}", "undefined", "True"}) {
            eq("not handled " + r, AppTabs.osBackHandled(r), false);
        }
        eq("os back handled", AppTabs.osBack("true", H, true), AppTabs.OsBack.HANDLED);
        eq("os back handled off-os", AppTabs.osBack("true", O + "/hearth#room", true), AppTabs.OsBack.HANDLED);
        eq("os back exits at home", AppTabs.osBack("false", H, true), AppTabs.OsBack.EXIT);
        eq("os back timeout exits", AppTabs.osBack(null, H + "&app=hearth", false), AppTabs.OsBack.EXIT);
        eq("os back null exits", AppTabs.osBack("null", O + "/", true), AppTabs.OsBack.EXIT);
        eq("off-os page history", AppTabs.osBack("false", O + "/hearth#room", true), AppTabs.OsBack.WEB_BACK);
        eq("off-os page to os", AppTabs.osBack(null, O + "/learn/join?code=1", false), AppTabs.OsBack.OS_HOME);
        eq("sign-in to os", AppTabs.osBack("false", O + "/login", false), AppTabs.OsBack.OS_HOME);

        // the user agent: AitherOS for a grown-up's phone, never a child's; Play right after
        // the version, where the web's Play check (AitherAndroid/<v> Play) looks
        eq("ua", AppTabs.uaToken("0.4.0", false, true), " AitherAndroid/0.4.0 AitherOS");
        eq("ua play", AppTabs.uaToken("0.4.0", true, true), " AitherAndroid/0.4.0 Play AitherOS");
        eq("ua child", AppTabs.uaToken("0.4.0", false, false), " AitherAndroid/0.4.0");
        eq("ua child play", AppTabs.uaToken("0.4.0", true, false), " AitherAndroid/0.4.0 Play");
        java.util.regex.Pattern play = java.util.regex.Pattern.compile("AitherAndroid/\\S+ Play\\b");
        eq("ua play seen", play.matcher(AppTabs.uaToken("0.4.0", true, true)).find(), true);
        eq("ua direct not play", play.matcher(AppTabs.uaToken("0.4.0", false, true)).find(), false);

        // the safe areas reach the page as CSS px, under the web's own variable names
        String js = AppTabs.insetsJs(24, 0, 48, 0);
        for (String want : new String[] {"--sat", "--sar", "--sab", "--sal", "--aither-inset-",
                "env(safe-area-inset-top", "aither:insets", "[24,0,48,0]"}) {
            if (!js.contains(want)) { System.out.println("FAIL insets js lacks " + want); bad++; }
        }

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
        // the child state is sticky: a household "child" profile alone is a child's phone,
        // and a page (a signed-out one reports k=false) may only turn child mode ON
        eq("profile child", AppTabs.childPhone(false, "child"), true);
        eq("page child", AppTabs.childPhone(true, ""), true);
        eq("adult phone", AppTabs.childPhone(false, "member"), false);
        eq("page cannot downgrade", AppTabs.afterPageReport(true, false), true);
        eq("page may upgrade", AppTabs.afterPageReport(false, true), true);
        eq("adult stays adult", AppTabs.afterPageReport(false, false), false);
        eq("signed-out page on a child phone",
                AppTabs.link(AppTabs.childPhone(AppTabs.afterPageReport(true, false), ""),
                        "https://example.com/x"), AppTabs.Link.ASK_GROWNUP);
        eq("child http", AppTabs.link(true, "http://app.aitherium.com/learn"), AppTabs.Link.ASK_GROWNUP);
        eq("child lookalike", AppTabs.link(true, "https://aitherium.com.evil.example/"), AppTabs.Link.ASK_GROWNUP);
        eq("child lookalike 2", AppTabs.link(true, "https://evilaitherium.com/"), AppTabs.Link.ASK_GROWNUP);
        eq("child mailto", AppTabs.link(true, "mailto:x@example.com"), AppTabs.Link.ASK_GROWNUP);
        eq("child intent", AppTabs.link(true, "intent://scan/#Intent;scheme=zxing;end"), AppTabs.Link.ASK_GROWNUP);
        eq("child garbage", AppTabs.link(true, "http://[bad"), AppTabs.Link.ASK_GROWNUP);
        eq("child ours", AppTabs.link(true, AppTabs.ORIGIN + "/learn"), AppTabs.Link.IN_APP);
        // the apex is a marketing export, not the classroom: a child's tap asks a grown-up
        eq("child apex", AppTabs.link(true, "https://aitherium.com/"), AppTabs.Link.ASK_GROWNUP);
        eq("child www", AppTabs.link(true, "https://www.aitherium.com/"), AppTabs.Link.ASK_GROWNUP);
        // api. and idp. are fetched or handled natively, never navigated to by a child
        eq("child api", AppTabs.link(true, "https://api.aitherium.com/x"), AppTabs.Link.ASK_GROWNUP);
        eq("child classroom host only", AppTabs.CHILD_HOSTS.toString(), "[app.aitherium.com]");
        eq("adult ours", AppTabs.link(false, "https://api.aitherium.com/x"), AppTabs.Link.IN_APP);
        // a child's phone opens only the classroom's own hosts: never the shop or a tenant site
        eq("child shop", AppTabs.link(true, "https://shop.aitherium.com/"), AppTabs.Link.ASK_GROWNUP);
        eq("child tenant", AppTabs.link(true, "https://acme.aitherium.com/"), AppTabs.Link.ASK_GROWNUP);
        eq("child blog", AppTabs.link(true, "https://blog.aitherium.com/x"), AppTabs.Link.ASK_GROWNUP);
        eq("child idp", AppTabs.link(true, "https://idp.aitherium.com/login"), AppTabs.Link.ASK_GROWNUP);
        eq("child sign-in", AppTabs.link(true, AppTabs.ORIGIN + "/learn/connect"), AppTabs.Link.IN_APP);
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
            // a grown-up's pin opens an OS app, never just the OS home
            if (!child && (r.window || !r.url.startsWith(AppTabs.OS_HOME + "&app="))) {
                System.out.println("FAIL shortcut " + arg + " -> " + r + ": not an OS app"); bad++;
            }
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
        // a child's phone never opens the adult form: its door is the kid screen (2026-10-16)
        eq("child sign in", AppTabs.signInUrl(true, AppTabs.ORIGIN + "/learn"), AppTabs.ORIGIN + "/learn/connect");
        eq("child sign in from anywhere", AppTabs.signInUrl(true, null), AppTabs.ORIGIN + "/learn/connect");
        eq("adult sign in unchanged", AppTabs.signInUrl(false, null), AppTabs.ORIGIN + "/login");
        eq("kid door is sign in", AppTabs.isSignIn(AppTabs.ORIGIN + "/learn/connect"), true);
        eq("learn is not sign in", AppTabs.isSignIn(AppTabs.ORIGIN + "/learn"), false);

        // ---- the WebView navigation policy (docs/plans/2026-10-17-mobile-shell.md)
        // the one WebView: an app page loads where it is; a tap on an OS app's page (or the
        // apex's copy) is routed to its OS URL; the manual and the journal are Custom Tabs
        nav("deep page in place", false, O + "/chat/x", true, false, "os", AppTabs.Nav.LOAD);
        nav("family page in place", false, O + "/learn/parent/kid", true, false, "os", AppTabs.Nav.LOAD);
        nav("os in place", false, H + "&app=hearth", true, false, "os", AppTabs.Nav.LOAD);
        nav("os app page routed", false, O + "/shop", true, false, "os", AppTabs.Nav.ROUTE);
        nav("chat page routed", false, O + "/chat", true, false, "os", AppTabs.Nav.ROUTE);
        nav("bare app id routed", false, O + "/?app=hearth", true, false, "os", AppTabs.Nav.ROUTE);
        nav("apex boot routed", false, "https://aitherium.com/", true, false, "os", AppTabs.Nav.ROUTE);
        nav("apex page routed", false, "https://aitherium.com/shop/x", true, false, "os", AppTabs.Nav.ROUTE);
        nav("manual", false, "https://docs.aitherium.com/", true, false, "os", AppTabs.Nav.CUSTOM_TAB);
        nav("journal", false, "https://blog.aitherium.com/x", true, false, "os", AppTabs.Nav.CUSTOM_TAB);
        // only a TAP moves: a redirect or a script navigation loads where it is
        nav("redirect never routed", false, O + "/shop", true, true, "os", AppTabs.Nav.LOAD);
        nav("script never routed", false, O + "/shop", false, false, "os", AppTabs.Nav.LOAD);
        nav("apex redirect", false, "https://aitherium.com/", false, true, "os", AppTabs.Nav.LOAD);
        // sign-in: our hand-off hosts and the OAuth providers' pages stay in the app
        nav("idp in place", false, "https://idp.aitherium.com/authorize?x=1", true, false, "aither", AppTabs.Nav.LOAD);
        nav("api callback", false, "https://api.aitherium.com/auth/callback", false, true, "", AppTabs.Nav.LOAD);
        nav("github oauth", false, "https://github.com/login/oauth/authorize?client_id=x", false, true, "", AppTabs.Nav.LOAD);
        nav("github session", false, "https://github.com/session", true, false, "", AppTabs.Nav.LOAD);
        nav("linkedin oauth", false, "https://www.linkedin.com/oauth/v2/authorization", false, true, "", AppTabs.Nav.LOAD);
        // everything else on the web is a Custom Tab (never a page inside a tab)
        nav("github repo", false, "https://github.com/Aitherium/awdk", true, false, "aither", AppTabs.Nav.CUSTOM_TAB);
        nav("off-site", false, "https://example.com/x", true, false, "aither", AppTabs.Nav.CUSTOM_TAB);
        nav("tenant site", false, "https://acme.aitherium.com/", true, false, "aither", AppTabs.Nav.CUSTOM_TAB);
        nav("shop host", false, "https://shop.aitherium.com/", true, false, "aither", AppTabs.Nav.CUSTOM_TAB);
        nav("lookalike", false, "https://aitherium.com.evil.example/", true, false, "aither", AppTabs.Nav.CUSTOM_TAB);
        nav("plain http", false, "http://app.aitherium.com/chat", true, false, "aither", AppTabs.Nav.CUSTOM_TAB);
        nav("mailto", false, "mailto:x@example.com", true, false, "aither", AppTabs.Nav.EXTERNAL);
        nav("tel", false, "tel:+15551234", true, false, "aither", AppTabs.Nav.EXTERNAL);
        nav("blob", false, "blob:https://app.aitherium.com/1", false, false, "aither", AppTabs.Nav.LOAD);
        // a child's phone: the classroom loads, everything else asks a grown-up -- never a
        // Custom Tab, a route out of the classroom or another app
        String[] childUrls = {O + "/learn/x", O + "/shop", "https://aitherium.com/", "https://example.com/",
            "https://github.com/login/oauth/authorize", "https://idp.aitherium.com/x", "mailto:a@b.c",
            "intent://x#Intent;end", "https://acme.aitherium.com/", "http://[bad"};
        for (String u : childUrls) {
            for (boolean g : new boolean[] {true, false}) {
                AppTabs.Nav n = AppTabs.navigate(true, u, g, false, "learn");
                if (n != AppTabs.Nav.LOAD && n != AppTabs.Nav.ASK_GROWNUP) {
                    System.out.println("FAIL child " + u + " -> " + n); bad++;
                }
            }
        }
        nav("child classroom", true, O + "/learn/x", true, false, "learn", AppTabs.Nav.LOAD);
        nav("child off-site", true, "https://example.com/", true, false, "learn", AppTabs.Nav.ASK_GROWNUP);
        nav("child github", true, "https://github.com/login/oauth/authorize", false, true, "learn", AppTabs.Nav.ASK_GROWNUP);
        // where a routed link lands: the OS, never a native window
        eq("link apex", AppTabs.linkRoute("https://aitherium.com/"), "tab os " + H);
        eq("link family", AppTabs.linkRoute(O + "/learn/parent/"), "tab os " + H + "&app=learn");
        eq("link shop", AppTabs.linkRoute(O + "/shop"), "tab os " + H + "&app=shop");

        // one header: the native bar names the page
        eq("title root", AppTabs.barTitle("Family", true, "Guardian · AitherOS"), "Family");
        eq("title page", AppTabs.barTitle("Family", false, "Household · AitherOS"), "Household");
        eq("title pipe", AppTabs.barTitle("Aither", false, "Shop | Aitherium"), "Shop");
        eq("title url", AppTabs.barTitle("Aither", false, "https://app.aitherium.com/chat"), "Aither");
        eq("title empty window", AppTabs.barTitle("", false, ""), "Aither");
        eq("title long", AppTabs.barTitle("", false, "A very long page title that goes on and on").length() <= 32, true);

        System.out.println(bad == 0 ? "OK" : bad + " FAILED");
        System.exit(bad == 0 ? 0 : 1);
    }
}
