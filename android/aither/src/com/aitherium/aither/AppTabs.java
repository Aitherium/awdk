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
    /** Home's two big buttons (Shell.entryButtons): the chat already listening, and the
     *  live camera with voice (/talk; a child is never shown it without a guardian's switch). */
    static final String TALK_PATH = "/chat?mode=talk";
    static final String SHOW_ME_PATH = "/talk?mode=show";

    /** id, label, root path ("" = native), icon (Shell.iconRes: the app's glyph
     *  res/drawable/ic_app_*, "gear" = settings, "mark" = the Aither mark). */
    static final String[][] ADULT_TABS = {
        {HOME, "Home", "", "home"},
        {"learn", "Learn", "/learn/parent/", "learn"},
        {"family", "Family", "/family", "family"},
        {"aither", "Aither", "/chat", "mark"},
        {SETTINGS, "Settings", "", "gear"},
    };
    /** A child's bar is ONE tab, their classroom (kids-suite S4): every child page opens in
     *  it, so the room is the menu and there is nothing else to wander into. Shell turns a
     *  long-press on it into the phone's lite Settings, for a grown-up. Asserted with the nav
     *  manifest (config/nav/aither-nav.yaml profiles.child.android) by check_child_nav.py. */
    static final String[][] CHILD_TABS = {
        {"learn", "Classroom", "/learn", "learn"},
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
        {"avatar", "Avatar", "Your avatars, and the one your Sprite wears", "/?app=avatar", "avatar", ""},
        {"agents", "My agents", "Create, command and connect your agents", "/?app=agent-life", "agents", ""},
        {"packs", "Packs & skills", "Add abilities to your agents", "/workspace/agents?tab=packs", "packs", ""},
        {"image-studio", "Image Studio", "Make and edit images with your credits", "/?app=image-studio", "mediaforge", ""},
        {"mediaforge", "Media Forge", "Make images, video and voice", "/?app=mediaforge", "mediaforge", ""},
        {"shop", "Shop", "Packs, plans and devices", "/shop", "shop", ""},
        {"desktop", "AitherOS", "The full desktop with every app", "/?shell=aither-desktop", "desktop", ""},
        {"control", "Control", "Your devices, lending and models", "/?app=control", "phone", ""},
        {"account", "My account", "Profile, sign-in and security", "/profile", "account", ""},
        {SETTINGS, "This phone", "Updates, AI on this phone, lending", "", "gear", SETTINGS},
    };
    static final String[][] CHILD_APPS = {
        {"learn", "Classroom", "Your room: lessons, stories, art and games", "/learn", "learn", "learn"},
        // the Nest: the child's own Sprite to talk to (never Aither's general chat)
        {"nest", "Talk with my Sprite", "Your Sprite, in the Nest", "/learn?talk=1", "sprite", "learn"},
        {"sprite", "My Sprite", "Your learning buddy", "/learn/sprite", "sprite", "learn"},
        {"quests", "Quests", "Quests with your Sprite", "/learn/sprite#quests", "quests", "learn"},
        // the agents a grown-up turned on (or "Ask a grown-up"): lib/family/kid_grants.py
        {"helpers", "My helpers", "Helpers your grown-up turned on", "/learn/agents", "agents", "learn"},
        {"room", "Room", "Your family room", "/hearth#room", "room", "learn"},
        {"space", "My Space", "Your own space", "/hearth", "space", "learn"},
    };

    /** Grown-up apps only the platform owner is offered (the web gate refuses everyone
     *  else too: app-gating.ts rbac system:admin). A customer makes images in Image Studio
     *  (paid with account credits), never Media Forge, which frames an owner-only host. */
    static final String[] OWNER_APPS = {"mediaforge"};
    /** Apps the Play build leaves out: Shop checks out through Stripe, which Play forbids
     *  for digital goods (Purchases). */
    static final String[] NOT_IN_STORE = {"shop"};

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

    /** The Home grid for this phone: owner-only apps for the platform owner, no Shop in
     *  the Play build. */
    static String[][] apps(boolean child, boolean owner, boolean store) {
        java.util.List<String[]> out = new java.util.ArrayList<>();
        for (String[] app : apps(child)) {
            if (!owner && has(OWNER_APPS, app[0])) continue;
            if (store && has(NOT_IN_STORE, app[0])) continue;
            out.add(app);
        }
        return out.toArray(new String[0][]);
    }

    private static boolean has(String[] ids, String id) {
        for (String s : ids) if (s.equals(id)) return true;
        return false;
    }

    /**
     * Is this account the platform owner? lib/core/AitherTenant.py roles_grant_platform with
     * the gateway's extras: super_admin/superadmin/platform/platform_admin anywhere; owner or
     * admin only on the platform tenant (every self-service customer is owner of their own).
     * From GET /api/profile's roles and tenant_id.
     */
    static boolean platformOwner(java.util.Collection<String> roles, String tenantId) {
        boolean scoped = false;
        for (String r : roles) {
            String x = r == null ? "" : r.trim().toLowerCase(java.util.Locale.ROOT);
            if (x.equals("super_admin") || x.equals("superadmin") || x.equals("platform")
                    || x.equals("platform_admin")) return true;
            if (x.equals("owner") || x.equals("admin")) scoped = true;
        }
        return scoped && "platform".equals(tenantId == null ? "" : tenantId.trim());
    }

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
        // one tab: the child edge sends a child's / to /learn, and the classroom's own pages
        // (their Sprite, the family room, their space) all open inside it
        if ("/".equals(path) || under(path, "/learn") || under(path, "/hearth")) return "learn";
        return "";
    }

    /** What a tapped link does: stay in the app, open the browser, or -- on a child's phone,
     *  for anything outside aitherium.com -- never open: "Ask a grown-up", and a card to them.
     *  MainActivity starts no ACTION_VIEW for ASK_GROWNUP. */
    enum Link { IN_APP, BROWSER, ASK_GROWNUP }

    static Link link(boolean child, String url) {
        String host = "";
        String scheme = "";
        try {
            URI u = new URI(url == null ? "" : url);
            host = u.getHost() == null ? "" : u.getHost().toLowerCase(java.util.Locale.ROOT);
            scheme = u.getScheme() == null ? "" : u.getScheme().toLowerCase(java.util.Locale.ROOT);
        } catch (Exception e) { /* unparseable: never ours */ }
        boolean ours = "https".equals(scheme)
                && (host.equals("aitherium.com") || host.endsWith(".aitherium.com"));
        // A child's phone stays on the classroom's own hosts: never the shop, a customer's
        // site (<tenant>.aitherium.com) or any other page that happens to be under the domain.
        if (child) return "https".equals(scheme) && CHILD_HOSTS.contains(host) ? Link.IN_APP : Link.ASK_GROWNUP;
        return ours ? Link.IN_APP : Link.BROWSER;
    }

    /** The only hosts a child's phone opens in the app: the classroom (and its apex alias),
     *  its API and the sign-in host. Everything else is "Ask a grown-up". */
    static final java.util.Set<String> CHILD_HOSTS = new java.util.HashSet<>(java.util.Arrays.asList(
            "app.aitherium.com", "aitherium.com", "api.aitherium.com", "idp.aitherium.com"));

    /** What the "Ask a grown-up" card says once POST /me/link-requests answered. "I told your
     *  grown-up" ONLY when the server says a card is stored in their inbox (201 + told:true);
     *  offline, no session, no grown-up yet, the day's ceiling or a card that did not send all
     *  say what to do instead, never that a grown-up knows. status -1 = no answer at all. */
    static String askMessage(int status, String body) {
        boolean told = (status == 200 || status == 201) && body != null
                && body.replaceAll("\s", "").contains("\"told\":true");
        if (told) return "That page is outside Aither. I told your grown-up, so you can look at it together.";
        return "That page is outside Aither. Show it to your grown-up, or a grown-up you trust, so you can look at it together.";
    }

    /** What the card says while the ask is on its way: no claim yet. */
    static final String ASKING = "That page is outside Aither. Asking your grown-up...";

    /** The body of POST /api/tutor/me/link-requests for an off-site link a child tapped:
     *  scheme, host and path only (never the query, fragment or userinfo -- a link can carry
     *  anything), at most 300 characters. "" when it is not a web link (mailto:, intent:...):
     *  the child still sees "Ask a grown-up", nothing is sent. */
    static String linkRequestBody(String url) {
        try {
            URI u = new URI(url == null ? "" : url);
            String scheme = u.getScheme() == null ? "" : u.getScheme().toLowerCase(java.util.Locale.ROOT);
            if ((!scheme.equals("http") && !scheme.equals("https")) || u.getHost() == null) return "";
            String path = u.getRawPath() == null ? "" : u.getRawPath();
            String clean = scheme + "://" + u.getHost().toLowerCase(java.util.Locale.ROOT) + path;
            if (clean.length() > 300) clean = clean.substring(0, 300);
            return "{\"url\":\"" + jsonEscape(clean) + "\"}";
        } catch (Exception e) {
            return "";
        }
    }

    private static String jsonEscape(String s) {
        StringBuilder b = new StringBuilder();
        for (char c : s.toCharArray()) {
            if (c == '"' || c == '\\') b.append('\\').append(c);
            else if (c < 0x20) b.append(String.format("\\u%04x", (int) c));
            else b.append(c);
        }
        return b.toString();
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

    /**
     * The one sign-in page, coming back to `returnUrl` (a page in this app) after it. A page
     * that is itself the sign-in page, or not on the app origin, returns to the default.
     */
    static String signInUrl(String returnUrl) {
        String back = "";
        try {
            URI u = new URI(returnUrl == null ? "" : returnUrl);
            String path = u.getRawPath() == null || u.getRawPath().isEmpty() ? "/" : u.getRawPath();
            if (ORIGIN.equals(u.getScheme() + "://" + u.getRawAuthority()) && !under(path, "/login")) {
                back = path + (u.getRawQuery() == null ? "" : "?" + u.getRawQuery());
            }
        } catch (Exception e) { /* no return page */ }
        if (back.isEmpty() || "/".equals(back)) return ORIGIN + "/login";
        try {
            return ORIGIN + "/login?redirect=" + java.net.URLEncoder.encode(back, StandardCharsets.UTF_8.name());
        } catch (Exception e) {
            return ORIGIN + "/login";
        }
    }

    /** Is this the sign-in page (where a tab lands when its page found no session)? */
    static boolean isSignIn(String url) {
        try {
            URI u = new URI(url == null ? "" : url);
            return u.getRawPath() != null && under(u.getRawPath(), "/login");
        } catch (Exception e) {
            return false;
        }
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
