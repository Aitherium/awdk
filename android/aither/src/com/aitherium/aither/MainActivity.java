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
    /** The page hides its own install cards for this (localapp); one app, one icon. */
    static final String UA_TOKEN = " AitherAndroid/0.2.0";

    private WebView web;
    private Config cfg;

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
                // the household device the page enrolled (family): the background check-in uses it
                v.evaluateJavascript("(function(){try{return localStorage.getItem('aither.family.device')||''}catch(e){return ''}})()",
                        val -> {
                            String dev = val == null ? "" : val.replaceAll("^\"|\"$", "").replace("\\\"", "\"");
                            if (dev.startsWith("{") && !dev.equals(cfg.familyDevice())) {
                                cfg.set("family_device", dev);
                                new Thread(() -> HeartbeatJob.beat(MainActivity.this)).start();
                            }
                        });
            }
        });
        setContentView(web);
        HeartbeatJob.schedule(this);
        if (cfg.llmEnabled()) startForegroundService(new Intent(this, LlmService.class));
        if (cfg.enabled()) startForegroundService(new Intent(this, HolderService.class));
        web.loadUrl(startUrl(getIntent()));
    }

    /** The page to open: a pairing hand-off once, else AitherOS. */
    private String startUrl(Intent i) {
        if (i != null && i.getData() != null && ours(i.getData())) return i.getData().toString();
        if (cfg.llmEnabled() && !cfg.llmPaired() && cfg.localAiBlocked().isEmpty()) {
            cfg.set("llm_paired", true);
            return HOME + "#local-pair=" + cfg.llmToken() + "&port=" + LocalProxy.PORT;
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
        if (i.getData() != null && ours(i.getData())) web.loadUrl(i.getData().toString());
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onPause() {
        CookieManager.getInstance().flush();
        super.onPause();
    }
}
