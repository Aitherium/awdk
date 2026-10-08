package com.aitherium.aither;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.webkit.CookieManager;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

/**
 * Aither on Android: AitherOS itself (the aitherium.com app) inside a native frame (Shell:
 * bottom tabs by role, a Home grid of apps, top bars, Settings one tap away), plus this phone's
 * extras running beside it: the model on this phone (LlmService, 127.0.0.1:8486), lending
 * memory (HolderService) and the household check-in (HeartbeatJob). One app, one icon.
 *
 * Only aitherium.com pages open here; any other link goes to the browser. The first time the
 * local model is available, the page is handed its per-install token through the URL
 * fragment (#local-pair=, never sent to a server), exactly as a separate browser would get it.
 */
public class MainActivity extends Activity implements Shell.Host {
    static final String HOME = "https://app.aitherium.com/";
    /** The page hides its own install cards for this (localapp); one app, one icon. The Play
     *  build adds " Play": the page then offers no purchase (Play's payments policy). */
    static final String UA_TOKEN = " AitherAndroid/" + Config.VERSION + (Flavor.STORE ? " Play" : "");

    /** The native frame: bottom tabs (one WebView each), Home grid, top bars (Shell). */
    private Shell shell;
    private Config cfg;
    private volatile boolean checking;
    private long lastCheck;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        cfg = new Config(this);
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != 0) {
            requestPermissions(new String[] {Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
        shell = new Shell(this, cfg.childDevice(), cfg.platformOwner(), this);
        PageTools.attach(shell);
        setContentView(shell.view());
        Edge.fit(shell.view());
        if (android.os.Build.VERSION.SDK_INT >= 33) {
            // Android 13+ with predictive back (default from target 36) never calls onBackPressed
            getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
                    android.window.OnBackInvokedDispatcher.PRIORITY_DEFAULT, this::back);
        }
        HeartbeatJob.schedule(this);
        Shortcuts.apply(this, cfg.childDevice());
        AitherFunctionService.sync(this, AitherFunctionService.child(cfg));
        if (cfg.llmEnabled()) startForegroundService(new Intent(this, LlmService.class));
        if (cfg.enabled()) startForegroundService(new Intent(this, HolderService.class));
        freshIfAsked();
        String pair = pairUrl();
        if (pair != null) shell.background(pair);
        Uri t = target(getIntent());
        if (linkDevice(t)) t = null; // the approve sheet opens over the app's home
        shell.open(t == null ? null : t.toString());
    }

    /** A device-link QR (app.aitherium.com/auth/device?code=, idp.aitherium.com/link) goes
     *  to the native approve sheet, not the web page. True when it was one. */
    private boolean linkDevice(Uri u) {
        String code = u == null ? null : DeviceLink.codeFrom(u.toString());
        if (code == null) return false;
        startActivity(new Intent(this, LinkActivity.class).putExtra(LinkActivity.EXTRA_CODE, code));
        return true;
    }

    /** Every WebView in the app (each tab, a window, the pairing hand-off) is made here, so
     *  all of them get the same settings, the AitherApp bridge and the same link rules. */
    @Override
    public WebView newWeb() {
        WebView web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setUserAgentString(s.getUserAgentString() + UA_TOKEN);
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true); // api.aitherium.com
        // window.AitherApp: the page reads and asks for the app's notification permission
        // (it has no web Notification API in here). Only aitherium.com pages load.
        web.addJavascriptInterface(new FamilyNotices.Bridge(this), "AitherApp");
        // window.AitherMCP: the page's WebMCP tools answer the phone's agent (PageTools)
        web.addJavascriptInterface(new PageTools.Bridge(), "AitherMCP");
        // window.AitherVoice: push-to-talk with the on-device recognizer (a child's Sprite)
        web.addJavascriptInterface(new PageVoice.Bridge(this, web), "AitherVoice");
        // window.AitherNearby: devices in pairing mode on this Wi-Fi (NearbyDevices, 5 minutes)
        web.addJavascriptInterface(new NearbyDevices.Bridge(this, web), "AitherNearby");
        web.setWebChromeClient(new android.webkit.WebChromeClient() {
            @Override
            public boolean onConsoleMessage(android.webkit.ConsoleMessage m) {
                // Only the voice's own line (awkit learnVoice): the exact text handed to
                // text-to-speech, so "nothing like *blinks* is read aloud" can be checked on a
                // phone. Every other console line stays in the page.
                String msg = m.message();
                if (msg != null && msg.startsWith("[aither-speak] ")) {
                    android.util.Log.i("AitherSpeak", msg.substring(15));
                }
                return false;
            }

            /** The page's own voice stack (getUserMedia: on-device Whisper, then Aither's
             *  recognizer) gets the mic ONLY on an https aitherium.com page, once the app holds
             *  the mic permission (PageVoice.pageMayUseMic). */
            @Override
            public void onPermissionRequest(android.webkit.PermissionRequest req) {
                String origin = req.getOrigin() == null ? "" : req.getOrigin().toString();
                boolean wantsMic = false;
                for (String r : req.getResources()) {
                    if (android.webkit.PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(r)) wantsMic = true;
                }
                if (wantsMic && PageVoice.pageMayUseMic(MainActivity.this, origin)) {
                    req.grant(new String[] {android.webkit.PermissionRequest.RESOURCE_AUDIO_CAPTURE});
                } else {
                    req.deny();
                }
            }
        });
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                Uri u = r.getUrl();
                if (Purchases.refuse(Flavor.STORE, u.getHost())) { // Play payments policy
                    android.widget.Toast.makeText(MainActivity.this, Purchases.REFUSED,
                            android.widget.Toast.LENGTH_LONG).show();
                    return true;
                }
                if (ours(u)) return false;
                try { startActivity(new Intent(Intent.ACTION_VIEW, u)); } catch (RuntimeException e) { /* none */ }
                return true;
            }

            @Override
            public android.webkit.WebResourceResponse shouldInterceptRequest(WebView v, WebResourceRequest r) {
                // a payment page in a frame or a script is refused too, not only a navigation
                if (!Purchases.refuse(Flavor.STORE, r.getUrl().getHost())) return null;
                return new android.webkit.WebResourceResponse("text/plain", "utf-8", 403, "Forbidden",
                        null, new java.io.ByteArrayInputStream(new byte[0]));
            }

            @Override
            public void doUpdateVisitedHistory(WebView v, String url, boolean reload) {
                shell.pageChanged(v); // a single-page app moves history without a page load
                pageMoved(url); // the sign-in page hands over to the page it came from this way
            }

            @Override
            public void onPageStarted(WebView v, String url, android.graphics.Bitmap icon) {
                PageTools.inject(v, url); // document.modelContext, on aitherium.com pages only
            }

            @Override
            public void onPageFinished(WebView v, String url) {
                PageTools.inject(v, url); // again: onPageStarted can land before the new document
                shell.pageChanged(v);
                if (!ours(Uri.parse(url))) return;
                CookieManager.getInstance().flush();
                readHousehold(false);
                pageMoved(url);
            }

            @Override
            public boolean onRenderProcessGone(WebView v, android.webkit.RenderProcessGoneDetail d) {
                // Unhandled, Android kills the whole app with the renderer. A page that ran out
                // of memory (a 3D avatar) gets a fresh WebView in its own tab instead.
                if (!shell.renderGone(v)) {
                    if (v.getParent() instanceof android.view.ViewGroup) {
                        ((android.view.ViewGroup) v.getParent()).removeView(v);
                    }
                    v.destroy(); // the pairing hand-off, loaded out of sight
                }
                return true;
            }
        });
        return web;
    }

    @Override
    public void openSettings() {
        startActivity(new Intent(this, SettingsActivity.class));
    }

    @Override
    public void exit() {
        moveTaskToBack(true); // what Android does for a launcher activity's last back
    }

    /**
     * What the household page left in this WebView's storage: the device it enrolled
     * (family: aither.family.device) and, from a guardian's QR, a single-use pairing code
     * for the workspace (aither.family.device.pair), and whether a child uses this phone
     * (aither.device.child: the launcher shortcuts follow it). Read on every page load and
     * every few seconds while AitherOS is on screen, because the page signs in without
     * reloading.
     */
    private void readHousehold(boolean quiet) {
        WebView web = shell.anyWeb();
        if (web == null) { // the native Home, no page open yet: still check in on open
            checkInOnOpen(false, quiet);
            return;
        }
        web.evaluateJavascript("(function(){try{return JSON.stringify({d:localStorage.getItem('aither.family.device')||'',"
                        + "p:localStorage.getItem('aither.family.device.pair')||'',"
                        + "k:localStorage.getItem('aither.device.child')==='1'})}catch(e){return '{}'}})()",
                val -> {
                    boolean fresh = false;
                    try {
                        // evaluateJavascript hands back a JSON string literal holding our JSON
                        Object lit = new org.json.JSONTokener(val).nextValue();
                        org.json.JSONObject j = new org.json.JSONObject(String.valueOf(lit));
                        String dev = j.optString("d", "");
                        if (dev.startsWith("{") && !dev.equals(cfg.familyDevice())) {
                            cfg.set("family_device", dev);
                            new Thread(() -> HeartbeatJob.beat(MainActivity.this)).start();
                            fresh = true;
                        }
                        fresh |= cfg.acceptPairCode(j.optString("p", ""));
                        boolean child = j.optBoolean("k", false);
                        if (j.has("k") && child != cfg.childDevice()) {
                            cfg.set("child_device", child);
                            Shortcuts.apply(MainActivity.this, child);
                            AitherFunctionService.sync(MainActivity.this, AitherFunctionService.child(cfg));
                            ticks.post(() -> shell.setChild(child)); // the tabs follow the role
                        }
                        if (!cfg.familyDevice().isEmpty()) askBatteryOnce();
                    } catch (Exception e) { /* nothing stored yet */ }
                    checkInOnOpen(fresh, quiet);
                });
    }

    /** On every open (and at once when the page just handed over a device or a pairing
     *  code): link this phone, collect what the owner sent it. */
    private void checkInOnOpen(boolean fresh, boolean quiet) {
        long since = System.currentTimeMillis() - lastCheck;
        if (!checking && (fresh || (!quiet && since > 60_000))) {
            checking = true;
            lastCheck = System.currentTimeMillis();
            new Thread(() -> {
                HeartbeatJob.checkIn(MainActivity.this);
                checking = false;
            }, "aither-open-checkin").start();
        }
    }

    private final android.os.Handler ticks = new android.os.Handler(android.os.Looper.getMainLooper());
    private final Runnable poll = new Runnable() {
        @Override
        public void run() {
            WebView web = shell.anyWeb();
            if (web != null && ours(Uri.parse(String.valueOf(web.getUrl())))) readHousehold(true);
            checkSession(false);
            ticks.postDelayed(this, 5000);
        }
    };

    // ---- the session (Session): one sign-in for every tab

    private volatile boolean probing;
    private long lastProbe;
    private long lastSignedIn;

    /** A page loaded or moved. While signed out, leaving the sign-in page is the moment a
     *  sign-in finished: ask now. (With no session cookie a check costs no request.) */
    private void pageMoved(String url) {
        if (!ours(Uri.parse(String.valueOf(url)))) return;
        checkSession(Session.last != Session.State.IN && !AppTabs.isSignIn(url));
    }

    /** Ask the server whether this phone is signed in: every 2 minutes while it is, every
     *  15 seconds while it is not or did not answer, and at once when `now`. */
    private void checkSession(boolean now) {
        long since = System.currentTimeMillis() - lastProbe;
        long every = Session.last == Session.State.IN ? 120_000 : 15_000;
        if (probing || (!now && since < every)) return;
        probing = true;
        lastProbe = System.currentTimeMillis();
        new Thread(() -> {
            Session.State s = Session.check();
            probing = false;
            ticks.post(() -> applySession(s));
        }, "aither-session").start();
    }

    /**
     * What the frame shows for an answer. Signed out: the Sign in bar (Shell). Signed in
     * after a sign-out or an outage, or with a tab left on the sign-in page: keep the
     * cookie, close the sign-in window, reload every tab, check in. No answer: nothing
     * changes; an outage never signs anyone out.
     */
    private void applySession(Session.State s) {
        if (s == Session.State.OUT) shell.signedOut(true);
        if (s != Session.State.IN) return;
        shell.signedOut(false);
        Boolean owner = Session.owner;
        if (owner != null && owner != cfg.platformOwner()) {
            cfg.set("platform_owner", owner.booleanValue());
            shell.setOwner(owner);
        }
        long sinceReload = System.currentTimeMillis() - lastSignedIn;
        // a page that stays on the sign-in page with a good session is reloaded once, not in a loop
        if (Session.troubled || (shell.onSignIn() && sinceReload > 10 * 60_000)) {
            Session.troubled = false;
            lastSignedIn = System.currentTimeMillis();
            CookieManager.getInstance().flush();
            shell.signedIn();
            checkInOnOpen(true, false);
        }
    }

    /** A link meant for this app: an aitherium.com page, or aither://open?url=<that page>. */
    static Uri target(Intent i) {
        Uri u = i == null ? null : i.getData();
        if (u == null) return null;
        if ("aither".equals(u.getScheme()) && "open".equals(u.getHost())) {
            String inner = u.getQueryParameter("url");
            u = inner == null ? null : Uri.parse(inner);
        }
        return u != null && ours(u) ? u : null;
    }

    /** The local model's pairing hand-off, once (the Shell loads it out of sight), or null. */
    private String pairUrl() {
        if (cfg.llmEnabled() && !cfg.llmPaired() && cfg.localAiBlocked().isEmpty()) {
            String token = cfg.llmToken(); // first: making a token resets "paired"
            cfg.set("llm_paired", true);
            return HOME + "#local-pair=" + token + "&port=" + LocalProxy.PORT;
        }
        return null;
    }

    static boolean ours(Uri u) {
        String h = u.getHost();
        return "https".equals(u.getScheme()) && h != null
                && (h.equals("aitherium.com") || h.endsWith(".aitherium.com"));
    }

    @Override
    protected void onNewIntent(Intent i) {
        super.onNewIntent(i);
        Uri t = target(i);
        if (t != null && !linkDevice(t)) shell.open(t.toString()); // into the right tab
    }

    @Override
    public void onBackPressed() { // Android 12 and older
        back();
    }

    private void back() {
        shell.back(); // window -> page history -> tab root -> Home -> exit (AppTabs.back)
    }

    @Override
    protected void onStart() {
        super.onStart();
        PageTools.shown(true);
    }

    @Override
    protected void onStop() {
        PageTools.shown(false);
        super.onStop();
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        super.onRequestPermissionsResult(code, perms, results);
        if (code == PageVoice.ASK_MIC) {
            PageVoice.of(this).onPermission(results.length > 0 && results[0] == 0);
        }
    }

    @Override
    protected void onDestroy() {
        PageVoice.of(this).destroy();
        super.onDestroy();
    }

    @Override
    protected void onPause() {
        PageVoice.of(this).stop();
        PageVoice.of(this).stopSpeaking(); // a reply never keeps talking after the app is left
        ticks.removeCallbacks(poll);
        CookieManager.getInstance().flush();
        super.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (freshIfAsked()) shell.reloadAll();
        readHousehold(false); // every open checks in, even on the native Home
        // after the pages restored their session into the cookie (a cold start writes it late)
        ticks.postDelayed(() -> checkSession(true), 3000);
        ticks.postDelayed(poll, 5000);
        // Family Shield: Android's one-time VPN prompt, once the guardian turned it on
        ShieldVpnService.askConsent(this, SHIELD_CONSENT);
    }

    static final int SHIELD_CONSENT = 0x5e1d;

    @Override
    protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == SHIELD_CONSENT && result == RESULT_OK) ShieldVpnService.sync(this);
    }

    /**
     * Once, when this phone is in a household: Android's own "let Aither run in the
     * background" dialog. Without it a phone that rarely opens the app drops to the RARE
     * standby bucket and the 15-minute check-in runs about once a day (measured on both
     * kids' phones, 2026-10-04), so the household sees a stale phone and sharing never wakes.
     */
    private void askBatteryOnce() {
        android.os.PowerManager pm = getSystemService(android.os.PowerManager.class);
        if (pm == null || pm.isIgnoringBatteryOptimizations(getPackageName())) return;
        if (cfg.batteryAsked()) return;
        cfg.set("battery_asked", true);
        try {
            startActivity(Flavor.battery(this));
        } catch (RuntimeException e) { /* settings app refused: Settings has the button */ }
    }

    static final int REPORT_ID = 0x4a17;

    /**
     * "Report" on any text selected in AitherOS: the in-app way to flag an AI answer
     * (Google Play's generative-AI policy). Report asks what is wrong and files it.
     */
    @Override
    public void onActionModeStarted(android.view.ActionMode mode) {
        super.onActionModeStarted(mode);
        android.view.Menu m = mode.getMenu();
        if (m.findItem(REPORT_ID) != null) return;
        m.add(android.view.Menu.NONE, REPORT_ID, 200, "Report").setOnMenuItemClickListener(item -> {
            WebView web = shell.current();
            if (web == null) {
                mode.finish();
                Report.open(this, "", "Aither");
                return true;
            }
            web.evaluateJavascript("(function(){try{return String(window.getSelection())}catch(e){return ''}})()",
                    val -> {
                        String sel = "";
                        try { sel = String.valueOf(new org.json.JSONTokener(val).nextValue()); }
                        catch (Exception e) { /* nothing selected */ }
                        mode.finish();
                        Report.open(this, sel, String.valueOf(web.getUrl()));
                    });
            return true;
        });
    }

    /** The owner's refresh-app command: drop the cached AitherOS so it loads fresh. */
    private boolean freshIfAsked() {
        if (!cfg.refreshApp()) return false;
        WebView open = shell.anyWeb();
        WebView web = open != null ? open : new WebView(this);
        web.clearCache(true); // the cache is per app: one WebView clears it for every tab
        if (open == null) web.destroy();
        cfg.set("refresh_app", false);
        return true;
    }
}
