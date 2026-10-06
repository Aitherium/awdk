package com.aitherium.aither;

import java.net.URI;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;

/**
 * What the app shell shows, by who uses this phone: the bottom tabs, the Home grid's apps,
 * where a link opens and what Android back does. Pure Java (no android.*), so
 * test/AppTabsCheck.java runs it on a desktop JVM.
 *
 * Every path is a real route of the AitherOS web app on the app origin (a monorepo test
 * fails on one that is not), and every `?app=` id is an OS app in app-registry.tsx. A child's `?app=` ids resolve through CHILD_APP_HOMES, the copy of
 * aither-app-entries.ts CHILD_SHORTCUT_HOMES (the same test pins the two).
 *
 * A tab with an empty path is native: Home is the grid, Settings opens SettingsActivity.
 */
final class AppTabs {
    static final String ORIGIN = "https://app.aitherium.com";
    static final String HOME = "home";
    static final String SETTINGS = "settings";

    /** id, label, root path ("" = native), icon (Shortcuts tile id; "home"/"gear" =
     *  vector, "mark" = the Aither mark, "aither" = the launcher icon). */
    static final String[][] ADULT_TABS = {
        {HOME, "Home", "", "home"},
        {"learn", "Learn", "/learn/parent/", "learn"},
        {"family", "Family", "/family", "family"},
        {"aither", "Aither", "/chat", "mark"},
        {SETTINGS, "Settings", "", "gear"},
    };
    static final String[][] CHILD_TABS = {
        {"learn", "Learn", "/learn", "learn"},
        {"sprite", "Sprite", "/learn/sprite", "sprite"},
        {"room", "Room", "/hearth#room", "room"},
        {"space", "My Space", "/hearth", "space"},
        {SETTINGS, "Settings", "", "gear"},
    };

    /** id, label, one plain line, path, icon, tab it opens in ("" = its own window). */
    static final String[][] ADULT_APPS = {
        {"learn", "Aither Learn", "Your children's tutor and their week", "/learn/parent/", "learn", "learn"},
        {"family", "Family", "Who's home, the family room, devices", "/family", "family", "family"},
        {"aither", "Talk to Aither", "Ask anything, by text or voice", "/chat", "mark", "aither"},
        {"hearth", "Hearth", "Your home agent: it asks before it acts", "/?app=hearth", "hearth", ""},
        {"sprite", "Sprite", "Hatch it. Teach it.", "/?app=sprite", "sprite", ""},
        {"academy", "Classroom", "Teach a class, or link a child to theirs", "/classroom/", "academy", ""},
        {"spaces", "Spaces", "Friends, agents and rooms", "/spaces/", "spaces", ""},
        {"avatar", "Avatar", "Your avatars, and the one your Sprite wears", "/?app=avatar", "space", ""},
        {"desktop", "AitherOS", "The full desktop with every app", "/?shell=aither-desktop", "aither", ""},
        {"control", "Control", "Your devices, lending and models", "/?app=control", "phone", ""},
        {"account", "My account", "Profile, sign-in and security", "/profile", "family", ""},
        {SETTINGS, "This phone", "Updates, AI on this phone, lending", "", "gear", SETTINGS},
    };
    static final String[][] CHILD_APPS = {
        {"learn", "Learn", "Your quests and your class work", "/learn", "learn", "learn"},
        {"sprite", "My Sprite", "Your learning buddy", "/learn/sprite", "sprite", "sprite"},
        {"quests", "Quests", "Quests with your Sprite", "/learn/sprite#quests", "quests", "sprite"},
        {"room", "Room", "Your family room", "/hearth#room", "room", "room"},
        {"space", "My Space", "Your own space", "/hearth", "space", "space"},
        {SETTINGS, "Settings", "This phone", "", "gear", SETTINGS},
    };

    /** aither-app-entries.ts CHILD_SHORTCUT_HOMES: where a child lands for `?app=<id>`. */
    static final String[][] CHILD_APP_HOMES = {
        {"hearth", "/learn"},
        {"learn", "/learn"},
        {"family", "/hearth"},
        {"room", "/hearth#room"},
        {"spaces", "/hearth"},
        {"myspace", "/hearth"},
        {"sprite", "/learn/sprite"},
        {"quests", "/learn/sprite#quests"},
        {"avatar", "/learn/sprite"},
        {"persona", "/learn/sprite"},
        {"academy", "/learn"},
        {"classroom", "/learn"},
    };

    /** A grown-up's `?app=` ids that have a tab of their own (the rest open as a window). */
    static final String[][] ADULT_APP_TABS = {
        {"learn", "learn"},
        {"family", "family"},
        {"greeter", "aither"},
    };

    private AppTabs() {}

    static String[][] tabs(boolean child) { return child ? CHILD_TABS : ADULT_TABS; }

    static String[][] apps(boolean child) { return child ? CHILD_APPS : ADULT_APPS; }

    /** Where the app opens and where back ends: the grid for a grown-up, Learn for a child. */
    static String homeTab(boolean child) { return child ? "learn" : HOME; }

    static String[] tab(boolean child, String id) {
        for (String[] t : tabs(child)) if (t[0].equals(id)) return t;
        return null;
    }

    static boolean isNative(String[] tab) { return tab != null && tab[2].isEmpty(); }

    /** A tab's root page, or "" for a native tab. */
    static String rootUrl(boolean child, String id) {
        String[] t = tab(child, id);
        return t == null || isNative(t) ? "" : ORIGIN + t[2];
    }

    /** Where a link opens: a tab (url "" = its root / a native screen) or its own window. */
    static final class Route {
        final String tab;
        final String url;
        final boolean window;

        Route(String tab, String url, boolean window) {
            this.tab = tab;
            this.url = url;
            this.window = window;
        }

        @Override
        public String toString() { return window ? "window " + url : "tab " + tab + (url.isEmpty() ? "" : " " + url); }
    }

    /**
     * The route for an aitherium.com link (a shortcut, a notification, an App Link, a QR).
     * A bare origin goes Home; `?app=<id>` goes to that app's tab when it has one; a path
     * under a tab's root opens in that tab (keeping its query and fragment); anything else
     * opens in a window over the tabs. Never the OS desktop by accident.
     */
    static Route route(String url, boolean child) {
        URI u;
        try {
            u = new URI(url);
        } catch (Exception e) {
            return new Route(homeTab(child), "", false);
        }
        String path = u.getRawPath() == null || u.getRawPath().isEmpty() ? "/" : u.getRawPath();
        String query = u.getRawQuery() == null ? "" : u.getRawQuery();
        String frag = u.getRawFragment() == null ? "" : u.getRawFragment();
        String app = "/".equals(path) ? param(query, "app") : "";
        if (!app.isEmpty()) {
            if (child) {
                String home = "/learn";
                for (String[] r : CHILD_APP_HOMES) if (r[0].equals(app)) home = r[1];
                return route(ORIGIN + home, true);
            }
            for (String[] r : ADULT_APP_TABS) if (r[0].equals(app)) return new Route(r[1], "", false);
            return new Route("", url, true);
        }
        if ("/".equals(path) && query.isEmpty() && frag.isEmpty()) return new Route(homeTab(child), "", false);
        String tab = child ? childTabFor(path, frag) : adultTabFor(path);
        if (tab.isEmpty()) return new Route("", url, true);
        return new Route(tab, isRoot(url, tab(child, tab)[2]) && sameFragment(frag, tab(child, tab)[2]) ? "" : url, false);
    }

    private static String adultTabFor(String path) {
        if (under(path, "/learn")) return "learn";
        if (under(path, "/hearth") || under(path, "/family")) return "family";
        if (under(path, "/chat")) return "aither";
        return "";
    }

    private static String childTabFor(String path, String frag) {
        if ("/".equals(path)) return "learn"; // the child edge sends a child's / to /learn
        if (under(path, "/learn/sprite")) return "sprite";
        if (under(path, "/learn")) return "learn";
        if (under(path, "/hearth")) return frag.startsWith("room") ? "room" : "space";
        return "";
    }

    private static boolean under(String path, String prefix) {
        return path.equals(prefix) || path.startsWith(prefix + "/");
    }

    private static boolean sameFragment(String frag, String root) {
        int i = root.indexOf('#');
        return frag.equals(i < 0 ? "" : root.substring(i + 1));
    }

    private static String param(String query, String name) {
        for (String kv : query.split("&")) {
            int i = kv.indexOf('=');
            String k = i < 0 ? kv : kv.substring(0, i);
            if (!k.equals(name)) continue;
            try {
                return URLDecoder.decode(i < 0 ? "" : kv.substring(i + 1), StandardCharsets.UTF_8.name()).trim();
            } catch (Exception e) {
                return "";
            }
        }
        return "";
    }

    /** Is this page the tab's root (same path, trailing slash aside, and same query)? */
    static boolean isRoot(String url, String rootPath) {
        if (url == null || rootPath == null || rootPath.isEmpty()) return false;
        try {
            URI a = new URI(url);
            URI b = new URI(ORIGIN + rootPath);
            return trim(a.getRawPath()).equals(trim(b.getRawPath()))
                    && String.valueOf(a.getRawQuery()).equals(String.valueOf(b.getRawQuery()));
        } catch (Exception e) {
            return false;
        }
    }

    private static String trim(String p) {
        if (p == null || p.isEmpty()) return "/";
        return p.length() > 1 && p.endsWith("/") ? p.substring(0, p.length() - 1) : p;
    }

    /** What Android back does, in order: the window's page, the window, the tab's page
     *  history, the tab's root, Home, then leave the app. */
    enum Back { WINDOW_BACK, WINDOW_CLOSE, WEB_BACK, TAB_ROOT, HOME, EXIT }

    static Back back(boolean windowOpen, boolean windowCanGoBack, boolean webCanGoBack,
                     boolean atTabRoot, boolean onHomeTab) {
        if (windowOpen) return windowCanGoBack ? Back.WINDOW_BACK : Back.WINDOW_CLOSE;
        if (webCanGoBack) return Back.WEB_BACK;
        if (!atTabRoot) return Back.TAB_ROOT;
        if (!onHomeTab) return Back.HOME;
        return Back.EXIT;
    }
}
