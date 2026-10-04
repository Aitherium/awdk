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
 * Aither on Android: AitherOS itself (the aitherium.com app) full screen, plus this phone's
 * extras running beside it: the model on this phone (LlmService, 127.0.0.1:8486), lending
 * memory (HolderService) and the household check-in (HeartbeatJob). One app, one icon.
 *
 * Only aitherium.com pages open here; any other link goes to the browser. The first time the
 * local model is available, the page is handed its per-install token through the URL
 * fragment (#local-pair=, never sent to a server), exactly as a separate browser would get it.
 */
public class MainActivity extends Activity {
    static final String HOME = "https://app.aitherium.com/";
    /** The page hides its own install cards for this (localapp); one app, one icon. The Play
     *  build adds " Play": the page then offers no purchase (Play's payments policy). */
    static final String UA_TOKEN = " AitherAndroid/" + Config.VERSION + (Flavor.STORE ? " Play" : "");

    private WebView web;
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
        web = new WebView(this);
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
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                Uri u = r.getUrl();
                if (ours(u)) return false;
                try { startActivity(new Intent(Intent.ACTION_VIEW, u)); } catch (RuntimeException e) { /* none */ }
                return true;
            }

            @Override
            public void onPageFinished(WebView v, String url) {
                if (!ours(Uri.parse(url))) return;
                CookieManager.getInstance().flush();
                readHousehold(false);
            }
        });
        setContentView(web);
        Edge.fit(web);
        if (android.os.Build.VERSION.SDK_INT >= 33) {
            // Android 13+ with predictive back (default from target 36) never calls onBackPressed
            getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
                    android.window.OnBackInvokedDispatcher.PRIORITY_DEFAULT, this::back);
        }
        HeartbeatJob.schedule(this);
        if (cfg.llmEnabled()) startForegroundService(new Intent(this, LlmService.class));
        if (cfg.enabled()) startForegroundService(new Intent(this, HolderService.class));
        freshIfAsked();
        web.loadUrl(startUrl(getIntent()));
    }

    /**
     * What the household page left in this WebView's storage: the device it enrolled
     * (family: aither.family.device) and, from a guardian's QR, a single-use pairing code
     * for the workspace (aither.family.device.pair). Read on every page load and every few
     * seconds while AitherOS is on screen, because the page signs in without reloading.
     */
    private void readHousehold(boolean quiet) {
        web.evaluateJavascript("(function(){try{return JSON.stringify({d:localStorage.getItem('aither.family.device')||'',"
                        + "p:localStorage.getItem('aither.family.device.pair')||''})}catch(e){return '{}'}})()",
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
                        if (!cfg.familyDevice().isEmpty()) askBatteryOnce();
                    } catch (Exception e) { /* nothing stored yet */ }
                    // on every open (and at once when the page just handed over a device or
                    // a pairing code): link this phone, collect what the owner sent it
                    long since = System.currentTimeMillis() - lastCheck;
                    if (!checking && (fresh || (!quiet && since > 60_000))) {
                        checking = true;
                        lastCheck = System.currentTimeMillis();
                        new Thread(() -> {
                            HeartbeatJob.checkIn(MainActivity.this);
                            checking = false;
                        }, "aither-open-checkin").start();
                    }
                });
    }

    private final android.os.Handler ticks = new android.os.Handler(android.os.Looper.getMainLooper());
    private final Runnable poll = new Runnable() {
        @Override
        public void run() {
            if (ours(Uri.parse(String.valueOf(web.getUrl())))) readHousehold(true);
            ticks.postDelayed(this, 5000);
        }
    };

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

    /** The page to open: a pairing hand-off once, else AitherOS. */
    private String startUrl(Intent i) {
        if (target(i) != null) return target(i).toString();
        if (cfg.llmEnabled() && !cfg.llmPaired() && cfg.localAiBlocked().isEmpty()) {
            String token = cfg.llmToken(); // first: making a token resets "paired"
            cfg.set("llm_paired", true);
            return HOME + "#local-pair=" + token + "&port=" + LocalProxy.PORT;
        }
        return HOME;
    }

    static boolean ours(Uri u) {
        String h = u.getHost();
        return "https".equals(u.getScheme()) && h != null
                && (h.equals("aitherium.com") || h.endsWith(".aitherium.com"));
    }

    @Override
    protected void onNewIntent(Intent i) {
        super.onNewIntent(i);
        if (target(i) != null) web.loadUrl(target(i).toString());
    }

    @Override
    public void onBackPressed() { // Android 12 and older
        back();
    }

    private void back() {
        if (web.canGoBack()) web.goBack();
        else moveTaskToBack(true); // what Android does for a launcher activity's last back
    }

    @Override
    protected void onPause() {
        ticks.removeCallbacks(poll);
        CookieManager.getInstance().flush();
        super.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (freshIfAsked()) web.loadUrl(HOME);
        ticks.postDelayed(poll, 5000);
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

    /** The owner's refresh-app command: drop the cached AitherOS so it loads fresh. */
    private boolean freshIfAsked() {
        if (!cfg.refreshApp()) return false;
        web.clearCache(true);
        cfg.set("refresh_app", false);
        return true;
    }
}
