package com.aitherium.aither;

import android.app.Activity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebView;
import android.widget.FrameLayout;

/**
 * The app's frame: AitherOS itself, on EVERY phone (0.5.0).
 *
 * ONE full-screen WebView on the OS ({@link AppTabs#OS_HOME}), edge to edge, with no native
 * bar, tabs, Home grid or window. The OS draws its own home screen, dock and windows;
 * Android back asks it first (AppTabs.OS_BACK_JS); every link, notification and shortcut
 * loads into that one WebView (AppTabs.route); the phone's safe areas are handed to the page
 * (AppTabs.insetsJs).
 *
 * A child's phone is the SAME OS with a child profile on top (owner 2026-11-19, overruling
 * the 10-19 one-tab classroom frame, which is gone): the WebViews say AitherKid
 * (AppTabs.uaToken), so the page offers only the kid-safe apps (Veil lib/kid-os-profile.ts);
 * the child edge (child-lockdown.ts) keeps the session on the child pages; a link off the
 * app origin asks a grown-up (AppTabs.navigate, MainActivity.askGrownUp); and a signed-out
 * child's phone shows the kid door (AppTabs.KID_DOOR), never the grown-up sign-in form.
 *
 * The WebViews come from the Host (MainActivity), which owns their settings, the JS bridges
 * and the link rules, so every WebView behaves the same.
 */
final class Shell {
    interface Host {
        /** A WebView configured like every other one in the app (settings, client, bridge). */
        WebView newWeb();

        /** Leave the app (Back at the OS home). */
        void exit();

        /** Edge to edge: transparent bars, the window drawn under them (the page reads the
         *  safe areas). Called once, from the constructor. */
        void edgeToEdge();
    }

    private final Host host;
    private final FrameLayout root;
    private boolean child;

    /** The one WebView: the OS. */
    private WebView os;
    /** The next finished load forgets the history behind it (sent home, or signed in). */
    private boolean clearOnLoad;
    /** The phone's safe areas in CSS px (top, right, bottom, left), for the OS page. */
    private int[] insets = {0, 0, 0, 0};
    /** A Back is waiting on the OS's answer: a second press does not ask twice. */
    private boolean backPending;

    Shell(Activity a, boolean child, Host host) {
        this.host = host;
        this.child = child;
        root = new FrameLayout(a);
        root.setBackgroundColor(Ui.BG);
        host.edgeToEdge();
    }

    View view() { return root; }

    /** Is this a child's phone (the OS with the child profile)? */
    boolean child() { return child; }

    /** Put the OS on screen, loading `url` in it ("" = the OS home when it is new). */
    private void showOs(String url) {
        if (os == null) {
            os = host.newWeb();
            root.addView(os, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
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

    /** Open an aitherium.com link in the one WebView, the OS (null = the OS home). */
    void open(String url) {
        showOs(url == null || url.isEmpty() ? "" : AppTabs.route(url, child).url);
    }

    /** A tapped link in the OS that belongs to an OS app (AppTabs.Nav.ROUTE, a grown-up's
     *  phone only: a child's navigation never routes): its OS URL, in the one WebView. */
    void openFromLink(String url) {
        open(url);
    }

    /** The surface a WebView belongs to: "os", or "" for one this shell does not hold. */
    String tabOf(WebView v) {
        return v != null && v == os ? AppTabs.OS : "";
    }

    /** The phone's role changed (a child signed in on it): a fresh OS, so every request
     *  carries the new user agent (MainActivity.newWeb reads the role). */
    void setChild(boolean c) {
        if (c == child) return;
        child = c;
        if (os != null) {
            root.removeView(os);
            os.destroy();
            os = null;
        }
        clearOnLoad = false;
        backPending = false;
        showOs("");
    }

    /** The WebView on screen, or none yet (for reading the page's storage). */
    WebView anyWeb() { return os; }

    /** The WebView the person is looking at, or none yet. */
    WebView current() { return os; }

    void reloadAll() {
        if (os != null) os.reload();
    }

    // ---- signing in: the OS signs a grown-up in itself; a child's phone gets the kid door

    /** No session on this phone. A grown-up's OS shows its own phone sign-in. A child's
     *  phone goes to the kid door ("Ask your grown-up to connect this phone"), never the
     *  grown-up form: a child cannot finish it, and a grown-up signing in there hands the
     *  child their account (2026-10-16). */
    void signedOut(boolean out) {
        if (!out || !child) return;
        if (os == null) {
            showOs(AppTabs.signInUrl(true, null));
        } else if (!AppTabs.isSignIn(os.getUrl())) {
            os.loadUrl(AppTabs.signInUrl(true, null));
        }
    }

    /** A session arrived (signed in here, or the server answers again after an outage): a
     *  page left on the sign-in page goes to the OS home, any other page reloads. */
    void signedIn() {
        if (os == null) return;
        if (AppTabs.isSignIn(os.getUrl())) {
            clearOnLoad = true;
            os.loadUrl(AppTabs.OS_HOME);
        } else {
            os.reload();
        }
    }

    /** Is the OS on the sign-in page (where it lands with no session)? */
    boolean onSignIn() {
        return os != null && AppTabs.isSignIn(os.getUrl());
    }

    /**
     * A page loaded or its history moved (MainActivity's WebViewClient tells us): forget the
     * history behind a page that was sent home. A finished load on the OS hands the page the
     * phone's safe areas again (a new document forgets them).
     */
    void pageFinished(WebView v) {
        pageChanged(v);
        if (v == os) pushInsets(v);
    }

    void pageChanged(WebView v) {
        if (v == os && clearOnLoad) {
            clearOnLoad = false;
            v.clearHistory();
        }
    }

    /**
     * A page's renderer died (out of memory: a 3D avatar on a small phone). The dead WebView
     * cannot be reused, so the OS gets a fresh one at the same page. Returns false only for a
     * WebView this shell does not hold.
     */
    boolean renderGone(WebView dead) {
        if (dead != os) return false;
        String url = String.valueOf(dead.getUrl());
        root.removeView(dead);
        dead.destroy();
        os = null;
        clearOnLoad = false;
        backPending = false;
        showOs(url.startsWith("http") ? url : "");
        return true;
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

    /**
     * Android back: ask window.aitherOs.back() (AppTabs.OS_BACK_JS). `true` = it closed or
     * went back in something. Anything else, or no answer in OS_BACK_TIMEOUT_MS, is decided by
     * AppTabs.osBack: leave from the OS, else the page's own history or the OS home.
     */
    void back() {
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
                clearOnLoad = true;
                w.loadUrl(AppTabs.OS_HOME);
                break;
            default: host.exit();
        }
    }

    /** An app's glyph: res/drawable/ic_app_<id>.xml, generated with the launcher icon by
     *  apply_aither_icon.py (one lucide glyph per app, the same ones the shortcut tiles
     *  wear). The OS draws its own icons; this table keeps every generated glyph referenced
     *  (R, not a name lookup: a missing glyph fails the build). */
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
}
