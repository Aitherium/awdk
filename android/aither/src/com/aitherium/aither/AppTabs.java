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
    /** You > My account: the workspace settings profile tab (/profile is retired). */
    static final String ACCOUNT_PATH = "/workspace/settings?tab=profile";

    /** A grown-up's phone IS the AitherOS desktop (0.4.0): ONE full-screen WebView on the
     *  OS, phone-shaped (its own home screen, dock, windows and Back). This is that surface. */
    static final String OS = "os";
    static final String OS_PATH = "/?host=android-os";
    static final String OS_HOME = ORIGIN + OS_PATH;

    /** id, label, root path ("" = native), icon (Shell.iconRes: the app's glyph
     *  res/drawable/ic_app_*, "gear" = settings, "mark" = the Aither mark).
     *
     *  A grown-up's phone draws NO native bar (0.4.0): the one row is the one WebView, the
     *  OS. Its dock, home screen and Settings ("You", which reaches this phone's native
     *  settings through window.AitherApp.openNativeSettings()) are the web's. Kept as a table
     *  so the screen checks keep reading the grown-up's surfaces from one place. Pinned
     *  against the nav manifest (profiles.adult.android: shell os, no tabs). */
    static final String[][] ADULT_TABS = {
        {"os", "Aither", "/?host=android-os", "mark"},
    };
    /** A child's bar is ONE tab, their classroom (kids-suite S4): every child page opens in
     *  it, so the room is the menu and there is nothing else to wander into. Shell turns a
     *  long-press on it into the phone's lite Settings, for a grown-up. Asserted with the nav
     *  manifest (config/nav/aither-nav.yaml profiles.child.android) by check_child_nav.py. */
    static final String[][] CHILD_TABS = {
        {"learn", "Classroom", "/learn", "learn"},
    };

    /** id, label, one plain line, path, icon, tab it opens in ("" = its own window), and
     *  (grown-ups only) the bead-space cluster Home files it under
     *  (docs/plans/2026-10-13-bead-space-navigation.md).
     *
     *  Since 0.4.0 the app draws no grown-up Home grid: the OS home screen lists the apps
     *  from the web's own registry. ADULT_APPS stays the catalogue of what a grown-up's
     *  phone offers (the nav manifest and the screen checks read it); the shell never draws
     *  it. CHILD_APPS is still the child's grid. One row per destination: what a
     *  tab already opens (Learn, Family, Talk to Aither, This phone), what lives inside
     *  another row (Packs & skills is a tab of My agents, My account is in You) and the
     *  owner's second image tool (Media Forge; Image Studio is the one on a phone) are not
     *  repeated here. */
    static final String[][] ADULT_APPS = {
        {"hearth", "Hearth", "Your home agent: it asks before it acts", "/?app=hearth", "hearth", "", "Home"},
        {"household", "Household", "Members, invites and family devices", "/family", "family", "family", "Home"},
        {"sprite", "Sprite", "Hatch it. Teach it.", "/?app=sprite", "sprite", "", "Home"},
        {"academy", "Classroom", "Teach a class, or link a child to theirs", "/classroom/", "academy", "", "Home"},
        {"agents", "Agents", "What is running, what waits on you, your agents", "/?app=agents", "agents", "", "Agents"},
        // ONE images tile per person: the owner's Media Forge, everyone else's Image Studio
        {"image-studio", "Image Studio", "Make and edit images with your credits", "/?app=image-studio", "mediaforge", "", "Make"},
        {"mediaforge", "Media Forge", "Make images, video and audio", "/?app=mediaforge", "mediaforge", "", "Make"},
        {"spaces", "Spaces", "Friends, agents and rooms", "/spaces/", "spaces", "", "Community"},
        {"avatar", "Avatar", "Your avatars, and the one your Sprite wears", "/?app=avatar", "avatar", "", "You"},
        {"control", "Control", "Your devices, lending and models", "/?app=control", "phone", "", "Fleet"},
        {"shop", "Shop", "Packs, plans and devices", "/shop", "shop", "", "Market"},
        {"desktop", "AitherOS", "The full desktop with every app", "/?shell=aither-desktop", "desktop", "", "More"},
    };
    /** The order Home shows the clusters in. */
    static final String[] CLUSTERS = {"Home", "Agents", "Make", "Community", "You", "Fleet", "Market", "More"};
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
     *  else too: app-gating.ts rbac system:admin). Empty since Media Forge left the phone
     *  (2026-10-16): a phone makes images in Image Studio; Media Forge stays on the
     *  desktop (AitherOS), where the owner's operator tools live. */
    static final String[] OWNER_APPS = {"mediaforge"};
    /** What the owner's own tool replaces on the owner's phone (one tile per job). */
    static final String[] NOT_FOR_OWNER = {"image-studio"};
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

    /** A web page that IS an OS app on the phone OS: a link to exactly that page (trailing
     *  slash aside, no query, no fragment) opens the app as an OS window,
     *  /?host=android-os&app=<id>. A deeper page, or one with a query or a fragment (a join
     *  code, #room), loads as itself so nothing it carries is lost. Every id is an OS app in
     *  app-registry.tsx (the monorepo test pins it). */
    static final String[][] ADULT_OS_PAGES = {
        {"/chat", "aeon"},
        {"/learn", "learn"},
        {"/learn/parent", "learn"},
        {"/hearth", "family"},
        {"/family", "family"},
        {"/classroom", "classroom"},
        {"/spaces", "spaces"},
        {"/shop", "shop"},
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
            if (owner && has(NOT_FOR_OWNER, app[0])) continue;
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

    /** Home's search: the apps whose label, line or cluster contain every word of `q`
     *  (case-insensitive); all of them for an empty query. */
    static String[][] search(String[][] apps, String q) {
        String[] words = q == null ? new String[0] : q.trim().toLowerCase(java.util.Locale.ROOT).split("\\s+");
        java.util.List<String[]> out = new java.util.ArrayList<>();
        for (String[] app : apps) {
            String hay = (app[1] + " " + app[2] + " " + (app.length > 6 ? app[6] : "")).toLowerCase(java.util.Locale.ROOT);
            boolean all = true;
            for (String w : words) if (!w.isEmpty() && !hay.contains(w)) all = false;
            if (all) out.add(app);
        }
        return out.toArray(new String[0][]);
    }

    /** Where the app opens and where back ends: the OS for a grown-up, Learn for a child. */
    static String homeTab(boolean child) { return child ? "learn" : OS; }

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
     *
     * A grown-up's phone has ONE surface, the OS: every link loads into it ({@link #osUrl}),
     * never a native window. A child's phone: a bare origin goes to the classroom, `?app=<id>`
     * to that app's child home, a path under the tab's root opens in the tab (keeping its
     * query and fragment); anything else opens in a window over the tab.
     */
    static Route route(String url, boolean child) {
        if (!child) return new Route(OS, osUrl(url), false);
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
            String home = "/learn";
            for (String[] r : CHILD_APP_HOMES) if (r[0].equals(app)) home = r[1];
            return route(ORIGIN + home, true);
        }
        if ("/".equals(path) && query.isEmpty() && frag.isEmpty()) return new Route(homeTab(child), "", false);
        String tab = childTabFor(path, frag);
        if (tab.isEmpty()) return new Route("", url, true);
        return new Route(tab, isRoot(url, tab(child, tab)[2]) && sameFragment(frag, tab(child, tab)[2]) ? "" : url, false);
    }

    /**
     * Where a link loads in a grown-up's one WebView, the OS:
     *   - the app origin's `/` (or the apex's, a static copy of the same app) is the OS, with
     *     `host=android-os` first and every other parameter and the fragment kept
     *     (`/?app=hearth` -> `/?host=android-os&app=hearth`);
     *   - a page that is an OS app (ADULT_OS_PAGES, exactly) opens that app's window;
     *   - any other app page loads as itself (the apex's on the app origin);
     *   - anything unparseable, or not ours, is the OS home.
     */
    static String osUrl(String url) {
        URI u;
        try {
            u = new URI(url == null ? "" : url);
        } catch (Exception e) {
            return OS_HOME;
        }
        String host = u.getHost() == null ? "" : u.getHost().toLowerCase(java.util.Locale.ROOT);
        if (!"https".equalsIgnoreCase(String.valueOf(u.getScheme()))
                || !(host.equals("aitherium.com") || host.endsWith(".aitherium.com"))) {
            return OS_HOME;
        }
        boolean app = host.equals("app.aitherium.com") || host.equals("aitherium.com")
                || host.equals("www.aitherium.com");
        if (!app) return url; // the manual, the journal: ours, not the OS
        String path = u.getRawPath() == null || u.getRawPath().isEmpty() ? "/" : u.getRawPath();
        String query = u.getRawQuery() == null ? "" : u.getRawQuery();
        String frag = u.getRawFragment() == null ? "" : u.getRawFragment();
        if ("/".equals(path)) {
            StringBuilder q = new StringBuilder(OS_PATH);
            for (String kv : query.split("&")) {
                if (kv.isEmpty() || kv.equals("host") || kv.startsWith("host=")) continue;
                q.append('&').append(kv);
            }
            return ORIGIN + q + (frag.isEmpty() ? "" : "#" + frag);
        }
        if (query.isEmpty() && frag.isEmpty()) {
            for (String[] r : ADULT_OS_PAGES) {
                if (trim(path).equals(r[0])) return OS_HOME + "&app=" + r[1];
            }
        }
        return ORIGIN + path + (query.isEmpty() ? "" : "?" + query) + (frag.isEmpty() ? "" : "#" + frag);
    }

    /** Is this page the OS itself (the app origin's `/`), where window.aitherOs answers? */
    static boolean isOsPage(String url) {
        try {
            URI u = new URI(url == null ? "" : url);
            String path = u.getRawPath() == null || u.getRawPath().isEmpty() ? "/" : u.getRawPath();
            return ORIGIN.equals(u.getScheme() + "://" + u.getRawAuthority()) && "/".equals(path);
        } catch (Exception e) {
            return false;
        }
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

    /** Is this a child's phone? The page's flag OR the household's profile kind: either one
     *  is enough, so a signed-out page cannot make a child's phone an adult's. */
    static boolean childPhone(boolean childDevice, String profileKind) {
        return childDevice || "child".equals(profileKind);
    }

    /** The stored child flag after the page reports its own. A page may only turn child mode
     *  ON; turning it off is a guardian's act (the owner removing the phone), never a page's:
     *  a signed-out page reports no child and must not open adult link routing. */
    static boolean afterPageReport(boolean stored, boolean reported) {
        return stored || reported;
    }

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

    /** The hosts that serve the AitherOS web app itself (Veil): the app origin, the apex
     *  and www (the static export of the same pages), the manual and the journal. A page
     *  here is a phone-mode app of this shell (html[data-host="android"]). */
    static final java.util.Set<String> APP_HOSTS = new java.util.HashSet<>(java.util.Arrays.asList(
            "app.aitherium.com", "aitherium.com", "www.aitherium.com", "docs.aitherium.com",
            "blog.aitherium.com"));
    /** Our sign-in and API hosts: a redirect chain passes through them, so they load in
     *  place and are never re-routed. */
    static final java.util.Set<String> AUTH_HOSTS = new java.util.HashSet<>(java.util.Arrays.asList(
            "idp.aitherium.com", "api.aitherium.com"));

    /** What a navigation in one of the app's WebViews does (MainActivity's
     *  shouldOverrideUrlLoading). docs/plans/2026-10-17-mobile-shell.md. */
    enum Nav {
        /** let the WebView load it where it is */
        LOAD,
        /** a tap on a link that belongs to another tab, Home, or its own window:
         *  Shell.openFromLink puts it there */
        ROUTE,
        /** a web page off the app: a Chrome Custom Tab, whose X comes back here */
        CUSTOM_TAB,
        /** mailto:, tel:, intent:... the phone's app for it */
        EXTERNAL,
        /** a child's phone: never leaves the classroom */
        ASK_GROWNUP
    }

    /**
     * The one navigation rule. `gesture` = the person tapped (WebResourceRequest.hasGesture),
     * `redirect` = a server redirect. Only a tap that is not a redirect is ever moved to
     * another tab or window, so a sign-in or OAuth chain runs through uninterrupted.
     * `currentTab` is the tab the WebView belongs to ("" for the window over the tabs).
     */
    static Nav navigate(boolean child, String url, boolean gesture, boolean redirect, String currentTab) {
        Link l = link(child, url);
        if (l == Link.ASK_GROWNUP) return Nav.ASK_GROWNUP;
        URI u;
        try {
            u = new URI(url == null ? "" : url);
        } catch (Exception e) {
            return Nav.EXTERNAL;
        }
        String scheme = u.getScheme() == null ? "" : u.getScheme().toLowerCase(java.util.Locale.ROOT);
        String host = u.getHost() == null ? "" : u.getHost().toLowerCase(java.util.Locale.ROOT);
        if (child) return Nav.LOAD; // link() let only the classroom's own host through
        if (!scheme.equals("http") && !scheme.equals("https")) {
            return scheme.equals("about") || scheme.equals("data") || scheme.equals("blob") ? Nav.LOAD : Nav.EXTERNAL;
        }
        if (!scheme.equals("https")) return Nav.CUSTOM_TAB;
        if (AUTH_HOSTS.contains(host) || signInPage(host, u.getRawPath())) return Nav.LOAD;
        if (!APP_HOSTS.contains(host)) return Nav.CUSTOM_TAB;
        if (!gesture || redirect) return Nav.LOAD;
        // A tap in the one WebView. The manual and the journal are not the OS: a Custom Tab,
        // so the desktop stays where it was. An app page loads where it is, unless it is an
        // OS app's page or the apex's copy, which Shell.openFromLink loads as the OS URL.
        if (host.equals("docs.aitherium.com") || host.equals("blog.aitherium.com")) return Nav.CUSTOM_TAB;
        return osUrl(url).equals(url) ? Nav.LOAD : Nav.ROUTE;
    }

    /** Where a tapped app link goes (Shell.openFromLink): the OS URL for it, in the one WebView. */
    static Route linkRoute(String url) {
        return route(url, false);
    }

    /** The OAuth providers' own sign-in pages (the app's /api/auth/oauth/<p> redirects to
     *  them): they stay in the app, or the session would land in Chrome instead. */
    static boolean signInPage(String host, String path) {
        String p = path == null ? "" : path;
        if (host.equals("github.com")) {
            return p.startsWith("/login") || p.startsWith("/session") || p.startsWith("/sessions/");
        }
        if (host.equals("www.linkedin.com") || host.equals("linkedin.com")) {
            return p.startsWith("/oauth/") || p.startsWith("/uas/") || p.startsWith("/login")
                    || p.startsWith("/checkpoint/");
        }
        return false;
    }

    /** The native bar's title: the tab's name at its root, else the page's own title
     *  ("Shop · AitherOS" -> "Shop"); "Aither" for an empty or URL-shaped title. */
    static String barTitle(String tabLabel, boolean atRoot, String pageTitle) {
        if (atRoot && tabLabel != null && !tabLabel.isEmpty()) return tabLabel;
        String t = pageTitle == null ? "" : pageTitle.trim();
        if (t.isEmpty() || t.startsWith("http") || t.contains("aitherium.com/")) {
            return tabLabel == null || tabLabel.isEmpty() ? "Aither" : tabLabel;
        }
        for (String sep : new String[] {" · ", " | ", " — ", " - "}) {
            int i = t.indexOf(sep);
            if (i > 0) t = t.substring(0, i);
        }
        return t.length() > 32 ? t.substring(0, 31) + "…" : t;
    }

    /** The only host a child's phone NAVIGATES to in the app: the classroom pages
     *  (app.aitherium.com, {@link #ORIGIN}). Everything else is "Ask a grown-up".
     *  Not here, on purpose: the apex (a static marketing export that 307s to the app host;
     *  the app opens its start URL with loadUrl, which this rule never sees), api. (data is
     *  fetched by the page, and fetches never reach shouldOverrideUrlLoading) and idp. (a
     *  child signs in on app.aitherium.com/learn/connect -- a one-time ticket redeemed by
     *  fetch -- or app.aitherium.com/login through the BFF; idp's own pages are only the
     *  OIDC hand-off for other sites and the device-link QR, which LinkActivity takes
     *  natively before any WebView). */
    static final java.util.Set<String> CHILD_HOSTS = new java.util.HashSet<>(java.util.Arrays.asList(
            "app.aitherium.com"));

    /** What the "Ask a grown-up" card says once POST /me/link-requests answered. "I told your
     *  grown-up" ONLY when the server says a card is stored in their inbox (201 + told:true);
     *  offline, no session, no grown-up yet, the day's ceiling or a card that did not send all
     *  say what to do instead, never that a grown-up knows. status -1 = no answer at all. */
    static String askMessage(int status, String body) {
        boolean told = (status == 200 || status == 201) && body != null
                && body.replaceAll("\\s", "").contains("\"told\":true");
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

    /** A child's door in (Veil lib/kid-sign-in.ts): "Ask your grown-up to connect this
     *  phone" and a code the guardian approves from their own phone. Never /login: a child
     *  cannot finish the adult form, and a grown-up signing in there hands the child their
     *  account (2026-10-16). */
    static final String KID_DOOR = "/learn/connect";

    /** The sign-in page for this phone: the kid door on a child's phone, else /login. */
    static String signInUrl(boolean child, String returnUrl) {
        return child ? ORIGIN + KID_DOOR : signInUrl(returnUrl);
    }

    /** Is this the sign-in page (where a tab lands when its page found no session)? */
    static boolean isSignIn(String url) {
        try {
            URI u = new URI(url == null ? "" : url);
            return u.getRawPath() != null && (under(u.getRawPath(), "/login") || under(u.getRawPath(), KID_DOOR));
        } catch (Exception e) {
            return false;
        }
    }

    /** What Android back does, in order: the window's page, the window, the tab's page
     *  history, the tab's root, Home, then leave the app. */
    enum Back { WINDOW_BACK, WINDOW_CLOSE, WEB_BACK, TAB_ROOT, HOME, EXIT }

    /** Android back on a grown-up's phone asks the OS first: it closes the top window, then
     *  Recents or the Apps sheet, then goes home, and answers false at the home screen. */
    static final String OS_BACK_JS =
            "(window.aitherOs&&window.aitherOs.back)?window.aitherOs.back():false";
    /** How long the OS gets to answer Back before the app leaves (a page still loading, or
     *  wedged, answers nothing). */
    static final long OS_BACK_TIMEOUT_MS = 500;

    /** What Android back does on the OS surface, from the page's answer. */
    enum OsBack { HANDLED, WEB_BACK, OS_HOME, EXIT }

    /** Did the OS handle Back? Only a literal `true` (evaluateJavascript's JSON of the
     *  result) is handled; false, null, a string, an error or no answer (null) is not. */
    static boolean osBackHandled(String jsResult) {
        return jsResult != null && "true".equals(jsResult.trim());
    }

    /**
     * Android back on a grown-up's phone. The OS handled it: done. Not handled on the OS
     * itself (its home screen, or no answer in time): leave the app. Not handled on a page
     * that is not the OS (a deep link that loaded as itself, the sign-in page): that page's
     * history, else the OS home -- a link never strands anyone outside the desktop.
     */
    static OsBack osBack(String jsResult, String url, boolean canGoBack) {
        if (osBackHandled(jsResult)) return OsBack.HANDLED;
        if (isOsPage(url)) return OsBack.EXIT;
        return canGoBack ? OsBack.WEB_BACK : OsBack.OS_HOME;
    }

    /** The user agent's app token. `AitherOS` marks the phone-OS shell (0.4.0) for the web's
     *  host detection; ` Play` stays RIGHT after the version, where the web's Play check
     *  (AitherAndroid/<v> Play) looks. A child's phone does not say AitherOS: its pages are
     *  the classroom, never the desktop. */
    static String uaToken(String version, boolean store, boolean osShell) {
        return " AitherAndroid/" + version + (store ? " Play" : "") + (osShell ? " AitherOS" : "");
    }

    /**
     * The script that hands the OS this phone's safe areas (CSS px). WebView may report
     * env(safe-area-inset-*) as 0 under transparent bars; then every side it reports as 0
     * while the phone has an inset is set on the root as the web's own variables (--sat,
     * --sar, --sab, --sal), and --aither-inset-* always carry the phone's numbers. Returns a
     * JSON line with both, for the log.
     */
    static String insetsJs(int top, int right, int bottom, int left) {
        return "(function(n){try{var d=document.documentElement;if(!d)return 'no-doc';"
                + "var p=document.createElement('div');"
                + "p.style.cssText='position:fixed;visibility:hidden;pointer-events:none;"
                + "padding:env(safe-area-inset-top,0px) env(safe-area-inset-right,0px) "
                + "env(safe-area-inset-bottom,0px) env(safe-area-inset-left,0px)';"
                + "(document.body||d).appendChild(p);var c=getComputedStyle(p);"
                + "var e=[c.paddingTop,c.paddingRight,c.paddingBottom,c.paddingLeft].map(parseFloat);"
                + "p.remove();var k=['--sat','--sar','--sab','--sal'],w=['top','right','bottom','left'];"
                + "for(var i=0;i<4;i++){d.style.setProperty('--aither-inset-'+w[i],n[i]+'px');"
                + "if(!(e[i]>0)&&n[i]>0)d.style.setProperty(k[i],n[i]+'px');"
                + "else d.style.removeProperty(k[i]);}"
                + "window.dispatchEvent(new CustomEvent('aither:insets',{detail:{native:n,env:e}}));"
                + "return JSON.stringify({env:e,native:n});}catch(x){return 'error '+x}})(["
                + top + "," + right + "," + bottom + "," + left + "])";
    }

    static Back back(boolean windowOpen, boolean windowCanGoBack, boolean webCanGoBack,
                     boolean atTabRoot, boolean onHomeTab) {
        if (windowOpen) return windowCanGoBack ? Back.WINDOW_BACK : Back.WINDOW_CLOSE;
        if (webCanGoBack) return Back.WEB_BACK;
        if (!atTabRoot) return Back.TAB_ROOT;
        if (!onHomeTab) return Back.HOME;
        return Back.EXIT;
    }
}
