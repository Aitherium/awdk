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
 * The app's own frame around AitherOS, so the phone gets an app and not a website:
 *
 *   - a native bottom bar of tabs by who uses this phone (AppTabs.tabs); each web tab is
 *     its own WebView, kept alive with its own back stack; tapping the active tab takes it
 *     back to its root;
 *   - a native Home grid of the apps this account can use (AppTabs.apps);
 *   - a native top bar over web content: back, title, refresh and a gear for Settings;
 *   - a full-screen window (back, title, refresh, close) for an app that has no tab.
 *
 * The WebViews themselves come from the Host (MainActivity), which owns their settings,
 * the JS bridge and the link rules, so every tab behaves exactly as the one WebView did.
 */
final class Shell {
    interface Host {
        /** A WebView configured like every other one in the app (settings, client, bridge). */
        WebView newWeb();

        /** The native settings screen. */
        void openSettings();

        /** Leave the app (back on the home tab). */
        void exit();
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
    private final TextView title;
    private final ImageView back;
    private final ImageView refresh;
    private View home;

    private final LinearLayout windowBox;
    private final FrameLayout windowContent;
    private final TextView windowTitle;
    private WebView window;
    private String windowLabel = "";

    Shell(Activity a, boolean child, Host host) {
        this.a = a;
        this.host = host;
        this.child = child;
        root = new FrameLayout(a);
        root.setBackgroundColor(Ui.BG);

        LinearLayout main = new LinearLayout(a);
        main.setOrientation(LinearLayout.VERTICAL);
        LinearLayout bar = Ui.bar(a);
        back = Ui.icon(a, R.drawable.ic_nav_back, "Back", v -> up());
        title = Ui.barTitle(a);
        refresh = Ui.icon(a, R.drawable.ic_nav_refresh, "Refresh", v -> {
            WebView w = webs.get(current);
            if (w != null) w.reload();
        });
        ImageView gear = Ui.icon(a, R.drawable.ic_nav_gear, "Settings", v -> host.openSettings());
        bar.addView(back);
        bar.addView(title);
        bar.addView(refresh);
        bar.addView(gear);
        main.addView(bar, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, Ui.dp(a, 56)));
        main.addView(Ui.rule(a));
        content = new FrameLayout(a);
        main.addView(content, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        main.addView(Ui.rule(a));
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
    }

    View view() { return root; }

    boolean child() { return child; }

    /** Open an aitherium.com link where it belongs (null = the home tab). */
    void open(String url) {
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

    /** The phone's role changed (a child signed in, or an adult): new tabs, back to home. */
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
        if (home != null) content.removeView(home);
        home = null;
        current = "";
        buildTabs();
        select(AppTabs.homeTab(child));
    }

    /** The WebView on screen, or the last web tab, or none (for reading the page's storage). */
    WebView anyWeb() {
        if (window != null) return window;
        WebView w = webs.get(current);
        if (w != null) return w;
        for (WebView x : webs.values()) return x;
        return null;
    }

    /** The WebView the person is looking at, or none on a native screen. */
    WebView current() {
        return window != null ? window : webs.get(current);
    }

    void reloadAll() {
        for (WebView w : webs.values()) w.reload();
        if (window != null) window.reload();
    }

    /**
     * A page loaded or its history moved (MainActivity's WebViewClient tells us): forget the
     * history behind a tab that was sent to its root, and refresh the bars.
     */
    void pageChanged(WebView v) {
        if (clearOnLoad.remove(v)) v.clearHistory();
        if (v == window && windowLabel.isEmpty()) windowTitle.setText(cleanTitle(v.getTitle()));
        updateBar();
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

    /** Android back. Returns false when the app should leave (the caller moves to back). */
    void back() {
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
        item.addView(label);
        item.setOnClickListener(v -> select(t[0]));
        paintNav(item, t[3], false);
        return item;
    }

    private static boolean vector(String icon) { return "home".equals(icon) || "gear".equals(icon); }

    static int iconRes(String icon) {
        if ("home".equals(icon)) return R.drawable.ic_nav_home;
        if ("gear".equals(icon)) return R.drawable.ic_nav_gear;
        if ("mark".equals(icon)) return R.drawable.aither_mark; // the Iris cut, glyph alone
        return Shortcuts.iconRes(icon);
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

    /** Put tab `id` on screen; load `url` in it ("" = its root when it is new). */
    private void show(String id, String url) {
        String[] t = AppTabs.tab(child, id);
        if (t == null) t = AppTabs.tab(child, AppTabs.homeTab(child));
        id = t[0];
        current = id;
        for (Map.Entry<String, WebView> e : webs.entrySet()) {
            e.getValue().setVisibility(e.getKey().equals(current) ? View.VISIBLE : View.GONE);
        }
        if (home != null) home.setVisibility(AppTabs.HOME.equals(id) ? View.VISIBLE : View.GONE);
        if (AppTabs.HOME.equals(id)) {
            if (home == null) {
                home = homeGrid();
                content.addView(home, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                        ViewGroup.LayoutParams.MATCH_PARENT));
            }
        } else if (!AppTabs.isNative(t)) {
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
        title.setText(t == null || AppTabs.HOME.equals(current) ? "Aither" : t[1]);
        boolean web = webs.containsKey(current);
        WebView w = webs.get(current);
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
        if (window == null) return;
        windowContent.removeView(window);
        window.destroy();
        window = null;
        windowBox.setVisibility(View.GONE);
        updateBar();
    }

    private static String cleanTitle(String t) {
        if (t == null || t.isEmpty() || t.startsWith("http")) return "Aither";
        int i = t.indexOf(" · ");
        if (i < 0) i = t.indexOf(" | ");
        return i > 0 ? t.substring(0, i) : t;
    }

    // ---- Home

    /** The grid of apps: each opens in its tab, in a window, or the settings screen. */
    private View homeGrid() {
        Context c = a;
        ScrollView scroll = new ScrollView(c);
        scroll.setFillViewport(true);
        LinearLayout col = new LinearLayout(c);
        col.setOrientation(LinearLayout.VERTICAL);
        int pad = Ui.dp(c, 16);
        col.setPadding(pad, pad, pad, pad);
        LinearLayout head = new LinearLayout(c);
        head.setOrientation(LinearLayout.HORIZONTAL);
        head.setGravity(Gravity.CENTER_VERTICAL);
        ImageView mark = new ImageView(c);
        mark.setImageResource(R.drawable.aither_mark);
        mark.setContentDescription("Aither");
        head.addView(mark, new LinearLayout.LayoutParams(Ui.dp(c, 48), Ui.dp(c, 48)));
        TextView h = Ui.title(c, "Your apps");
        h.setPadding(Ui.dp(c, 12), 0, 0, 0);
        head.addView(h);
        col.addView(head);
        TextView sub = Ui.note(c, "Tap one to open it. The bar at the bottom takes you back.");
        sub.setPadding(0, Ui.dp(c, 8), 0, Ui.dp(c, 16));
        col.addView(sub);
        String[][] apps = AppTabs.apps(child);
        int perRow = c.getResources().getConfiguration().screenWidthDp >= 600 ? 4 : 3;
        LinearLayout row = null;
        for (int i = 0; i < apps.length; i++) {
            if (i % perRow == 0) {
                row = new LinearLayout(c);
                row.setOrientation(LinearLayout.HORIZONTAL);
                col.addView(row);
            }
            row.addView(tile(apps[i]), tileParams(c));
        }
        if (row != null) {
            for (int k = apps.length % perRow; k != 0 && k < perRow; k++) {
                row.addView(new View(c), tileParams(c)); // keep the last row's tiles the same width
            }
        }
        scroll.addView(col);
        return scroll;
    }

    private static LinearLayout.LayoutParams tileParams(Context c) {
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        int m = Ui.dp(c, 5);
        p.setMargins(m, m, m, m);
        return p;
    }

    private View tile(String[] app) {
        Context c = a;
        LinearLayout t = new LinearLayout(c);
        t.setOrientation(LinearLayout.VERTICAL);
        t.setGravity(Gravity.CENTER_HORIZONTAL);
        t.setBackground(new android.graphics.drawable.RippleDrawable(
                android.content.res.ColorStateList.valueOf(Ui.PRESS), Ui.round(c, Ui.CARD, Ui.RADIUS, Ui.LINE), null));
        int p = Ui.dp(c, 10);
        t.setPadding(p, Ui.dp(c, 14), p, Ui.dp(c, 12));
        t.setMinimumHeight(Ui.dp(c, 132));
        t.setContentDescription(app[1] + ". " + app[2]);
        ImageView icon = new ImageView(c);
        icon.setImageResource(iconRes(app[4]));
        if (vector(app[4])) icon.setImageTintList(android.content.res.ColorStateList.valueOf(Ui.ACCENT));
        t.addView(icon, new LinearLayout.LayoutParams(Ui.dp(c, 52), Ui.dp(c, 52)));
        TextView name = Ui.text(c, app[1], 14, Ui.INK);
        name.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        name.setGravity(Gravity.CENTER);
        name.setPadding(0, Ui.dp(c, 8), 0, Ui.dp(c, 2));
        t.addView(name);
        TextView line = Ui.text(c, app[2], 12, Ui.DIM);
        line.setGravity(Gravity.CENTER);
        line.setMaxLines(2);
        line.setEllipsize(android.text.TextUtils.TruncateAt.END);
        t.addView(line);
        t.setOnClickListener(v -> openApp(app));
        return t;
    }

    private void openApp(String[] app) {
        String tab = app[5];
        if (AppTabs.SETTINGS.equals(tab)) {
            host.openSettings();
        } else if (!tab.isEmpty()) {
            String root = AppTabs.rootUrl(child, tab);
            String url = AppTabs.ORIGIN + app[3];
            show(tab, url.equals(root) ? "" : url);
        } else {
            openWindow(AppTabs.ORIGIN + app[3], app[1]);
        }
    }
}
