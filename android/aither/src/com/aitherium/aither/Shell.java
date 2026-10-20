package com.aitherium.aither;

import android.app.Activity;
import android.content.Context;
import android.graphics.Typeface;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebView;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;

/**
 * The app's frame around AitherOS, by who uses this phone.
 *
 * A grown-up's phone IS the AitherOS desktop (0.4.0): ONE full-screen WebView on the OS
 * ({@link AppTabs#OS_HOME}), edge to edge, with no native bar, tabs, Home grid or window.
 * The OS draws its own home screen, dock and windows; Android back asks it first
 * (AppTabs.OS_BACK_JS); every link, notification and shortcut loads into that one WebView
 * (AppTabs.osUrl); the phone's safe areas are handed to the page (AppTabs.insetsJs).
 *
 * A child's phone keeps its native frame, unchanged:
 *   - a bottom bar of ONE tab, the classroom (AppTabs.CHILD_TABS), whose long-press opens
 *     this phone's settings for a grown-up;
 *   - a top bar over the page: back, title, refresh and a gear for a grown-up;
 *   - a full-screen window (back, title, refresh, close) for a page outside the tab.
 *
 * The WebViews themselves come from the Host (MainActivity), which owns their settings,
 * the JS bridges and the link rules, so every WebView behaves the same.
 */
final class Shell {
    interface Host {
        /** A WebView configured like every other one in the app (settings, client, bridge). */
        WebView newWeb();

        /** The native settings screen. */
        void openSettings();

        /** Leave the app (back on the home tab). */
        void exit();

        /** The phone's role changed: edge to edge (the OS) or the native frame (a child). */
        void modeChanged(boolean child);
    }

    private final Activity a;
    private final Host host;
    private boolean child;
    private String current = "";

    private final Map<String, WebView> webs = new HashMap<>();
    private final Set<WebView> clearOnLoad = new HashSet<>();
    private final Map<String, View> navItems = new HashMap<>();

    private final FrameLayout root;
    private final FrameLayout content;
    private final LinearLayout nav;
    private final LinearLayout bar;
    private final View barRule;
    private final View navRule;
    private final TextView title;
    private final ImageView back;
    private final ImageView refresh;
    private final ImageView gear;
    private final View signInBar;
    private TextView signInLine;
    private TextView signInGo;
    private boolean signingIn;
    private boolean signedOut;

    private final LinearLayout windowBox;
    private final FrameLayout windowContent;
    private final TextView windowTitle;
    private WebView window;
    private String windowLabel = "";

    /** A grown-up's one WebView: the OS. */
    private WebView os;
    /** The phone's safe areas in CSS px (top, right, bottom, left), for the OS page. */
    private int[] insets = {0, 0, 0, 0};
    /** A Back is waiting on the OS's answer: a second press does not ask twice. */
    private boolean backPending;

    Shell(Activity a, boolean child, Host host) {
        this.a = a;
        this.host = host;
        this.child = child;
        root = new FrameLayout(a);
        root.setBackgroundColor(Ui.BG);

        LinearLayout main = new LinearLayout(a);
        main.setOrientation(LinearLayout.VERTICAL);
        bar = Ui.bar(a);
        back = Ui.icon(a, R.drawable.ic_nav_back, "Back", v -> up());
        title = Ui.barTitle(a);
        refresh = Ui.icon(a, R.drawable.ic_nav_refresh, "Refresh", v -> {
            WebView w = webs.get(current);
            if (w != null) w.reload();
        });
        gear = Ui.icon(a, R.drawable.ic_nav_gear, "Settings", v -> host.openSettings());
        gear.setVisibility(child ? View.VISIBLE : View.GONE); // a grown-up has the You tab
        bar.addView(back);
        bar.addView(title);
        bar.addView(refresh);
        bar.addView(gear);
        main.addView(bar, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, Ui.dp(a, 56)));
        barRule = Ui.rule(a);
        main.addView(barRule);
        signInBar = signInBar();
        signInBar.setVisibility(View.GONE);
        main.addView(signInBar);
        content = new FrameLayout(a);
        main.addView(content, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        navRule = Ui.rule(a);
        main.addView(navRule);
        nav = new LinearLayout(a);
        nav.setOrientation(LinearLayout.HORIZONTAL);
        nav.setBackgroundColor(Ui.BG);
        main.addView(nav, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, Ui.dp(a, 64)));
        root.addView(main, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT));

        windowBox = new LinearLayout(a);
        windowBox.setOrientation(LinearLayout.VERTICAL);
        windowBox.setBackgroundColor(Ui.BG);
        windowBox.setClickable(true); // nothing under it takes a touch
        LinearLayout wbar = Ui.bar(a);
        windowTitle = Ui.barTitle(a);
        wbar.addView(Ui.icon(a, R.drawable.ic_nav_back, "Back", v -> back()));
        wbar.addView(windowTitle);
        wbar.addView(Ui.icon(a, R.drawable.ic_nav_refresh, "Refresh", v -> {
            if (window != null) window.reload();
        }));
        wbar.addView(Ui.icon(a, R.drawable.ic_nav_close, "Close", v -> closeWindow()));
        windowBox.addView(wbar, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, Ui.dp(a, 56)));
        windowBox.addView(Ui.rule(a));
        windowContent = new FrameLayout(a);
        windowBox.addView(windowContent, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        windowBox.setVisibility(View.GONE);
        root.addView(windowBox, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT));
        buildTabs();
        applyMode();
    }

    View view() { return root; }

    boolean child() { return child; }

    /** The native frame is a child's only: a grown-up's screen is the OS, edge to edge. */
    private void applyMode() {
        int frame = child ? View.VISIBLE : View.GONE;
        bar.setVisibility(frame);
        barRule.setVisibility(frame);
        navRule.setVisibility(frame);
        nav.setVisibility(frame);
        signInBar.setVisibility(child && signedOut ? View.VISIBLE : View.GONE);
        host.modeChanged(child);
    }

    /** Put the OS on screen, loading `url` in it ("" = the OS home when it is new). */
    private void showOs(String url) {
        current = AppTabs.OS;
        if (os == null) {
            os = host.newWeb();
            content.addView(os, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.MATCH_PARENT));
            os.loadUrl(url.isEmpty() ? AppTabs.OS_HOME : url);
        } else if (!url.isEmpty()) {
            os.loadUrl(url);
        }
    }

    /** The phone's safe areas changed (CSS px): the OS page gets them now and on every load. */
    void insets(int top, int right, int bottom, int left) {
        int[] next = {top, right, bottom, left};
        if (java.util.Arrays.equals(next, insets)) return;
        insets = next;
        if (os != null) pushInsets(os);
    }

    private void pushInsets(WebView w) {
        if (!MainActivity.ours(android.net.Uri.parse(String.valueOf(w.getUrl())))) return;
        w.evaluateJavascript(AppTabs.insetsJs(insets[0], insets[1], insets[2], insets[3]),
                v -> android.util.Log.i("AitherInsets", String.valueOf(v)));
    }

    /** Open an aitherium.com link where it belongs (null = the home tab). A grown-up's
     *  link always loads into the one WebView, the OS (AppTabs.osUrl). */
    void open(String url) {
        if (!child) {
            closeWindow();
            showOs(url == null || url.isEmpty() ? "" : AppTabs.route(url, false).url);
            return;
        }
        if (url == null || url.isEmpty()) {
            select(AppTabs.homeTab(child));
            return;
        }
        AppTabs.Route r = AppTabs.route(url, child);
        if (r.window) {
            openWindow(r.url, "");
            return;
        }
        if (AppTabs.SETTINGS.equals(r.tab)) {
            host.openSettings();
            return;
        }
        closeWindow();
        show(r.tab, r.url);
    }

    /** A tapped link in one of the app's pages that belongs somewhere else in the app
     *  (AppTabs.Nav.ROUTE): on a grown-up's phone, the OS URL for it in the one WebView. */
    void openFromLink(String url) {
        AppTabs.Route r = AppTabs.linkRoute(url);
        if (r.window && child) {
            openWindow(r.url, "");
            return;
        }
        open(url);
    }

    /** The tab a WebView belongs to, "" for the window (or one this shell does not hold). */
    String tabOf(WebView v) {
        if (v == window) return "";
        if (v != null && v == os) return AppTabs.OS;
        for (Map.Entry<String, WebView> e : webs.entrySet()) if (e.getValue() == v) return e.getKey();
        return "";
    }

    /** The phone's role changed (a child signed in, or an adult): the OS for a grown-up, the
     *  classroom tab for a child, each from its start. */
    void setChild(boolean c) {
        if (c == child) return;
        child = c;
        closeWindow();
        for (WebView w : webs.values()) {
            content.removeView(w);
            w.destroy();
        }
        webs.clear();
        clearOnLoad.clear();
        if (os != null) {
            content.removeView(os);
            os.destroy();
            os = null;
        }
        current = "";
        gear.setVisibility(child ? View.VISIBLE : View.GONE);
        buildTabs();
        applyMode();
        select(AppTabs.homeTab(child));
    }

    /** The WebView on screen, or the last web tab, or none (for reading the page's storage). */
    WebView anyWeb() {
        if (window != null) return window;
        if (os != null) return os;
        WebView w = webs.get(current);
        if (w != null) return w;
        for (WebView x : webs.values()) return x;
        return null;
    }

    /** The WebView the person is looking at, or none on a native screen. */
    WebView current() {
        if (window != null) return window;
        return os != null ? os : webs.get(current);
    }

    void reloadAll() {
        for (WebView w : webs.values()) w.reload();
        if (window != null) window.reload();
        if (os != null) os.reload();
    }

    // ---- signing in: one place, every tab follows (Session)

    /** The bar under the top bar while this phone has no session: one tap to sign in. */
    private View signInBar() {
        LinearLayout b = new LinearLayout(a);
        b.setOrientation(LinearLayout.HORIZONTAL);
        b.setGravity(Gravity.CENTER_VERTICAL);
        b.setBackgroundColor(Ui.RAISED);
        int p = Ui.dp(a, 16);
        b.setPadding(p, Ui.dp(a, 6), Ui.dp(a, 8), Ui.dp(a, 6));
        signInLine = Ui.text(a, "", 14, Ui.INK);
        b.addView(signInLine, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        signInGo = Ui.action(a, "", v -> signIn());
        signInGo.setPadding(Ui.dp(a, 12), Ui.dp(a, 10), Ui.dp(a, 12), Ui.dp(a, 10));
        b.addView(signInGo);
        signInWords(b);
        return b;
    }

    /** A child's phone asks for a grown-up; nobody's phone offers a child the adult form. */
    private void signInWords(View bar) {
        signInLine.setText(child ? "Ask your grown-up to connect this phone."
                : "You're signed out. Sign in once and every tab follows.");
        signInGo.setText(child ? "Connect" : "Sign in");
        bar.setContentDescription(child ? "Signed out. Ask your grown-up to connect this phone"
                : "Signed out. Sign in");
    }

    /** A child's phone shows the bar; a grown-up's OS shows its own phone sign-in. */
    void signedOut(boolean out) {
        signedOut = out;
        signInWords(signInBar);
        signInBar.setVisibility(out && child ? View.VISIBLE : View.GONE);
    }

    /** The sign-in page in a window over the tabs; it comes back to the page on screen.
     *  On a child's phone that is the kid door (AppTabs.KID_DOOR), never /login. */
    void signIn() {
        WebView w = webs.get(current);
        openWindow(AppTabs.signInUrl(child, w == null ? null : w.getUrl()), child ? "Connect" : "Sign in");
        signingIn = true;
    }

    /** A session arrived (signed in here, or the server answers again after an outage):
     *  close the sign-in window and load every tab again, a tab left on the sign-in page
     *  at its own root. */
    void signedIn() {
        signedOut(false);
        if (signingIn) closeWindow();
        for (Map.Entry<String, WebView> e : webs.entrySet()) {
            WebView w = e.getValue();
            if (AppTabs.isSignIn(w.getUrl())) {
                clearOnLoad.add(w);
                w.loadUrl(AppTabs.rootUrl(child, e.getKey()));
            } else {
                w.reload();
            }
        }
        if (window != null) window.reload();
        if (os != null) {
            if (AppTabs.isSignIn(os.getUrl())) {
                clearOnLoad.add(os);
                os.loadUrl(AppTabs.OS_HOME);
            } else {
                os.reload();
            }
        }
    }

    /** Is any page in the app on the sign-in page (where it lands with no session)? */
    boolean onSignIn() {
        if (window != null && AppTabs.isSignIn(window.getUrl())) return true;
        if (os != null && AppTabs.isSignIn(os.getUrl())) return true;
        for (WebView w : webs.values()) if (AppTabs.isSignIn(w.getUrl())) return true;
        return false;
    }

    /**
     * A page loaded or its history moved (MainActivity's WebViewClient tells us): forget the
     * history behind a tab that was sent to its root, and refresh the bars. A finished load
     * on the OS hands the page the phone's safe areas again (a new document forgets them).
     */
    void pageFinished(WebView v) {
        pageChanged(v);
        if (v == os) pushInsets(v);
    }

    void pageChanged(WebView v) {
        if (clearOnLoad.remove(v)) v.clearHistory();
        if (v == window && windowLabel.isEmpty()) windowTitle.setText(AppTabs.barTitle("", false, v.getTitle()));
        updateBar();
    }

    /** A page's <title> arrived or changed: the bar names it (no history work). */
    void titleChanged(WebView v) {
        if (v == window && windowLabel.isEmpty()) windowTitle.setText(AppTabs.barTitle("", false, v.getTitle()));
        updateBar();
    }

    /**
     * A page's renderer died (out of memory: a 3D avatar on a small phone). The dead WebView
     * cannot be reused, so its tab or the window gets a fresh one at the same page; the other
     * tabs and the app carry on. Returns false only for a WebView this shell does not hold.
     */
    boolean renderGone(WebView dead) {
        String url = String.valueOf(dead.getUrl());
        if (dead == window) {
            windowContent.removeView(dead);
            dead.destroy();
            window = host.newWeb();
            windowContent.addView(window, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.MATCH_PARENT));
            if (url.startsWith("http")) window.loadUrl(url);
            return true;
        }
        if (dead == os) {
            content.removeView(dead);
            clearOnLoad.remove(dead);
            dead.destroy();
            os = null;
            backPending = false;
            showOs(url.startsWith("http") ? url : "");
            return true;
        }
        for (Map.Entry<String, WebView> e : webs.entrySet()) {
            if (e.getValue() != dead) continue;
            String id = e.getKey();
            content.removeView(dead);
            clearOnLoad.remove(dead);
            dead.destroy();
            webs.remove(id);
            // only the tab on screen reloads now; a hidden one comes back at its root when picked
            if (id.equals(current)) show(id, url.startsWith("http") ? url : "");
            return true;
        }
        return false;
    }

    /** Load a page out of sight: the local-model pairing hand-off (#local-pair=) is taken by
     *  the AitherOS page itself, into this app's storage, without opening the desktop. */
    void background(String url) {
        WebView w = host.newWeb();
        root.addView(w, 0, new FrameLayout.LayoutParams(1, 1));
        w.setAlpha(0f);
        w.loadUrl(url);
        root.postDelayed(() -> {
            root.removeView(w);
            w.destroy();
        }, 90_000);
    }

    // ---- back

    /** Android back: the OS answers first on a grown-up's phone; a child's frame walks its
     *  window, page history, tab root, Home, then leaves (AppTabs.back). */
    void back() {
        if (!child) {
            osBack();
            return;
        }
        WebView w = webs.get(current);
        AppTabs.Back b = AppTabs.back(window != null, window != null && window.canGoBack(),
                w != null && w.canGoBack(), atRoot(), current.equals(AppTabs.homeTab(child)));
        switch (b) {
            case WINDOW_BACK: window.goBack(); break;
            case WINDOW_CLOSE: closeWindow(); break;
            case WEB_BACK: w.goBack(); break;
            case TAB_ROOT: toRoot(current); break;
            case HOME: select(AppTabs.homeTab(child)); break;
            default: host.exit();
        }
        updateBar();
    }

    /**
     * Back on the OS: ask window.aitherOs.back() (AppTabs.OS_BACK_JS). `true` = it closed or
     * went back in something. Anything else, or no answer in OS_BACK_TIMEOUT_MS, is decided by
     * AppTabs.osBack: leave from the OS, else the page's own history or the OS home.
     */
    private void osBack() {
        WebView w = os;
        if (w == null) {
            host.exit();
            return;
        }
        if (backPending) return;
        backPending = true;
        final boolean[] answered = {false};
        Runnable late = () -> {
            if (answered[0]) return;
            answered[0] = true;
            backPending = false;
            afterOsBack(w, null);
        };
        root.postDelayed(late, AppTabs.OS_BACK_TIMEOUT_MS);
        w.evaluateJavascript(AppTabs.OS_BACK_JS, v -> {
            if (answered[0]) return;
            answered[0] = true;
            backPending = false;
            root.removeCallbacks(late);
            afterOsBack(w, v);
        });
    }

    private void afterOsBack(WebView w, String result) {
        if (w != os) return; // the WebView was replaced (renderer gone) while asking
        switch (AppTabs.osBack(result, w.getUrl(), w.canGoBack())) {
            case HANDLED: break;
            case WEB_BACK: w.goBack(); break;
            case OS_HOME:
                clearOnLoad.add(w);
                w.loadUrl(AppTabs.OS_HOME);
                break;
            default: host.exit();
        }
    }

    /** The top bar's back arrow: the same as Android back, except it never leaves the app. */
    private void up() {
        WebView w = webs.get(current);
        if (w != null && w.canGoBack()) w.goBack();
        else if (!atRoot()) toRoot(current);
        else if (!current.equals(AppTabs.homeTab(child))) select(AppTabs.homeTab(child));
        updateBar();
    }

    private boolean atRoot() {
        WebView w = webs.get(current);
        if (w == null) return true; // a native tab is its own root
        String[] t = AppTabs.tab(child, current);
        return t == null || AppTabs.isRoot(w.getUrl(), t[2]);
    }

    // ---- tabs

    private void buildTabs() {
        nav.removeAllViews();
        navItems.clear();
        for (String[] t : AppTabs.tabs(child)) {
            View item = navItem(t);
            navItems.put(t[0], item);
            nav.addView(item, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1f));
        }
    }

    private View navItem(String[] t) {
        LinearLayout item = new LinearLayout(a);
        item.setOrientation(LinearLayout.VERTICAL);
        item.setGravity(Gravity.CENTER);
        item.setBackground(Ui.ripple(a, 0));
        item.setContentDescription(t[1]);
        ImageView icon = new ImageView(a);
        icon.setImageResource(iconRes(t[3]));
        icon.setTag("icon");
        item.addView(icon, new LinearLayout.LayoutParams(Ui.dp(a, 26), Ui.dp(a, 26)));
        TextView label = Ui.text(a, t[1], 12, Ui.FAINT);
        label.setTag("label");
        label.setPadding(0, Ui.dp(a, 3), 0, 0);
        // centred under its icon: a vertical LinearLayout's default child is MATCH_PARENT
        // wide, so the text sat flush left of its column ("Home" at the screen edge)
        label.setGravity(Gravity.CENTER_HORIZONTAL);
        label.setSingleLine(true);
        item.addView(label, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT));
        item.setOnClickListener(v -> select(t[0]));
        if (child) {
            // a child's bar is one tab (the classroom): a grown-up long-presses it for Settings
            item.setOnLongClickListener(v -> {
                host.openSettings();
                return true;
            });
        }
        paintNav(item, t[3], false);
        return item;
    }

    /** Every icon but the Aither mark is a white glyph that takes a tint. */
    private static boolean vector(String icon) { return !"mark".equals(icon); }

    /** An app's glyph: res/drawable/ic_app_<id>.xml, generated with the launcher icon by
     *  apply_aither_icon.py (one lucide glyph per app, the same ones the shortcut tiles
     *  wear). R, not a name lookup: a missing glyph fails the build. */
    static int iconRes(String icon) {
        switch (icon) {
            case "mark": return R.drawable.aither_mark; // the Iris cut, glyph alone
            case "home": return R.drawable.ic_app_home;
            case "gear": return R.drawable.ic_app_settings;
            case "learn": return R.drawable.ic_app_learn;
            case "family": return R.drawable.ic_app_family;
            case "hearth": return R.drawable.ic_app_hearth;
            case "sprite": return R.drawable.ic_app_sprite;
            case "academy": return R.drawable.ic_app_academy;
            case "spaces": return R.drawable.ic_app_spaces;
            case "space": return R.drawable.ic_app_space;
            case "avatar": return R.drawable.ic_app_avatar;
            case "quests": return R.drawable.ic_app_quests;
            case "room": return R.drawable.ic_app_room;
            case "phone": return R.drawable.ic_app_control;
            case "account": return R.drawable.ic_app_account;
            case "desktop": return R.drawable.ic_app_desktop;
            case "agents": return R.drawable.ic_app_agents;
            case "packs": return R.drawable.ic_app_packs;
            case "mediaforge": return R.drawable.ic_app_mediaforge;
            case "shop": return R.drawable.ic_app_shop;
            default: return R.drawable.aither_mark;
        }
    }

    private void paintNav(View item, String icon, boolean on) {
        ImageView i = item.findViewWithTag("icon");
        TextView l = item.findViewWithTag("label");
        int color = on ? Ui.ACCENT : Ui.FAINT;
        l.setTextColor(color);
        l.setTypeface(Typeface.create(on ? "sans-serif-medium" : "sans-serif", Typeface.NORMAL));
        if (vector(icon)) i.setImageTintList(android.content.res.ColorStateList.valueOf(color));
        i.setAlpha(on ? 1f : 0.6f);
        item.setSelected(on);
    }

    /** A tap on a tab: Settings opens its screen; the active web tab goes to its root. */
    void select(String id) {
        if (!child) { // a grown-up has no tabs: the one surface is the OS
            closeWindow();
            showOs("");
            return;
        }
        if (AppTabs.SETTINGS.equals(id)) {
            host.openSettings();
            return;
        }
        closeWindow();
        if (id.equals(current) && webs.containsKey(id)) {
            toRoot(id);
            return;
        }
        show(id, "");
    }

    /** Put a child's tab `id` on screen; load `url` in it ("" = its root when it is new). */
    private void show(String id, String url) {
        if (!child) {
            showOs(url);
            return;
        }
        String[] t = AppTabs.tab(child, id);
        if (t == null) t = AppTabs.tab(child, AppTabs.homeTab(child));
        id = t[0];
        current = id;
        for (Map.Entry<String, WebView> e : webs.entrySet()) {
            e.getValue().setVisibility(e.getKey().equals(current) ? View.VISIBLE : View.GONE);
        }
        if (!AppTabs.isNative(t)) {
            WebView w = webs.get(id);
            if (w == null) {
                w = host.newWeb();
                webs.put(id, w);
                content.addView(w, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                        ViewGroup.LayoutParams.MATCH_PARENT));
                w.loadUrl(url.isEmpty() ? AppTabs.ORIGIN + t[2] : url);
            } else if (!url.isEmpty()) {
                w.loadUrl(url);
            }
        }
        for (String[] x : AppTabs.tabs(child)) {
            View item = navItems.get(x[0]);
            if (item != null) paintNav(item, x[3], x[0].equals(current));
        }
        updateBar();
    }

    private void toRoot(String id) {
        WebView w = webs.get(id);
        String rootUrl = AppTabs.rootUrl(child, id);
        if (w == null || rootUrl.isEmpty()) return;
        clearOnLoad.add(w);
        w.loadUrl(rootUrl);
    }

    private void updateBar() {
        String[] t = AppTabs.tab(child, current);
        boolean web = webs.containsKey(current);
        WebView w = webs.get(current);
        // one header: the native bar names the page (its <title>), the tab at its root
        title.setText(t == null || AppTabs.HOME.equals(current) ? "Aither"
                : AppTabs.barTitle(t[1], !web || atRoot(), w == null ? "" : w.getTitle()));
        boolean canUp = web && ((w != null && w.canGoBack()) || !atRoot())
                || (!current.equals(AppTabs.homeTab(child)) && !current.isEmpty());
        back.setVisibility(canUp ? View.VISIBLE : View.INVISIBLE);
        refresh.setVisibility(web ? View.VISIBLE : View.GONE);
    }

    // ---- the window

    void openWindow(String url, String label) {
        windowLabel = label == null ? "" : label;
        windowTitle.setText(windowLabel.isEmpty() ? "Aither" : windowLabel);
        if (window == null) {
            window = host.newWeb();
            windowContent.addView(window, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.MATCH_PARENT));
        }
        windowBox.setVisibility(View.VISIBLE);
        window.loadUrl(url);
    }

    void closeWindow() {
        signingIn = false;
        if (window == null) return;
        windowContent.removeView(window);
        window.destroy();
        window = null;
        windowBox.setVisibility(View.GONE);
        updateBar();
    }
}
