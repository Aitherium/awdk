package com.aitherium.aither;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.ServiceInfo;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.wifi.WifiManager;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * The durable holder: a foreground service that hosts the holder engine (holder.js, the same
 * code the browser page runs) in a WebView it owns, and dials the workspace relay outbound.
 *
 * It lends only while the owner's policy holds (by default: charging and on Wi-Fi). While
 * lending it keeps a partial wake lock and a Wi-Fi lock so the link survives the screen going
 * off. The notification always says what it is doing.
 */
public class HolderService extends Service {
    static final String ORIGIN = "https://appassets.androidplatform.net/";
    static volatile String lastStatus = "{}";
    static volatile String reason = "starting";

    private Config cfg;
    private Handler main;
    private WebView web;
    private PowerManager.WakeLock wake;
    private WifiManager.WifiLock wifi;
    private boolean charging, onWifi, pageReady, lending, pairing;
    private long lastNote;

    private final BroadcastReceiver battery = new BroadcastReceiver() {
        @Override
        public void onReceive(Context c, Intent i) {
            charging = i.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0;
            evaluate();
        }
    };

    private final ConnectivityManager.NetworkCallback net = new ConnectivityManager.NetworkCallback() {
        @Override
        public void onCapabilitiesChanged(Network n, NetworkCapabilities caps) {
            onWifi = caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
                    || caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET);
            main.post(HolderService.this::evaluate);
        }

        @Override
        public void onLost(Network n) {
            onWifi = false;
            main.post(HolderService.this::evaluate);
        }
    };

    @Override
    public void onCreate() {
        super.onCreate();
        cfg = new Config(this);
        main = new Handler(Looper.getMainLooper());
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel(
                "kvholder", "Lending memory", NotificationManager.IMPORTANCE_LOW));
        Notification n = note("Starting");
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(1, n);
        }
        PowerManager pm = getSystemService(PowerManager.class);
        wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "aither:kvholder");
        wake.setReferenceCounted(false);
        WifiManager wm = getApplicationContext().getSystemService(WifiManager.class);
        wifi = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "aither:kvholder");
        wifi.setReferenceCounted(false);
        registerReceiver(battery, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        getSystemService(ConnectivityManager.class).registerDefaultNetworkCallback(net);
        startPage();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && "stop".equals(intent.getAction())) {
            cfg.set("enabled", false);
        }
        main.post(() -> {
            // a pairing link that arrived while the engine page was already up
            if (pageReady && !cfg.paired() && !cfg.pendingCode().isEmpty()) pair(cfg.pubkey());
            evaluate();
        });
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        setLending(false);
        try { unregisterReceiver(battery); } catch (RuntimeException e) { /* never registered */ }
        try {
            getSystemService(ConnectivityManager.class).unregisterNetworkCallback(net);
        } catch (RuntimeException e) { /* never registered */ }
        if (web != null) web.destroy();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent i) {
        return null;
    }

    // ------------------------------------------------------------ the engine page

    private void startPage() {
        web = new WebView(getApplicationContext());
        // keep the renderer at the service's priority with the screen off (it is never shown)
        web.setRendererPriorityPolicy(WebView.RENDERER_PRIORITY_IMPORTANT, false);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        web.addJavascriptInterface(new Bridge(), "AitherKV");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView v, WebResourceRequest r) {
                return asset(r.getUrl().toString());
            }
        });
        web.resumeTimers();
        web.loadUrl(ORIGIN + "holder-app.html");
    }

    /** Serve the bundled page from a secure origin (WebGPU and WebCrypto need one). */
    private WebResourceResponse asset(String url) {
        if (!url.startsWith(ORIGIN)) return null;
        String name = url.substring(ORIGIN.length());
        int q = name.indexOf('?');
        if (q >= 0) name = name.substring(0, q);
        if (!name.matches("[a-z0-9-]+\\.(html|js)")) return null;
        try {
            InputStream in = getAssets().open(name);
            String type = name.endsWith(".html") ? "text/html" : "text/javascript";
            return new WebResourceResponse(type, "utf-8", in);
        } catch (Exception e) {
            return null;
        }
    }

    private void js(String code) {
        if (web != null) web.evaluateJavascript(code, null);
    }

    // ------------------------------------------------------------ policy

    void evaluate() {
        String why;
        if (!cfg.enabled()) why = "off (turned off on this phone)";
        else if (cfg.relay().isEmpty()) why = "not paired (open the owner's pairing link)";
        else if (pairing) why = "pairing with the workspace";
        else if (!cfg.paired()) why = reason.startsWith("pairing") ? reason : "not paired";
        else if (!pageReady) why = "starting the engine";
        else if (cfg.onlyCharging() && !charging) why = "waiting: not charging";
        else if (cfg.onlyWifi() && !onWifi) why = "waiting: not on Wi-Fi";
        else why = "";
        if (why.isEmpty() != lending) setLending(why.isEmpty());
        if (!why.isEmpty()) reason = why;
        update(true);
        if (!cfg.enabled()) stopSelf();
    }

    private void setLending(boolean on) {
        lending = on;
        if (on) {
            wake.acquire();
            wifi.acquire();
            reason = "lending";
            js("kvStart()");
        } else {
            js("kvStop()");
            if (wake.isHeld()) wake.release();
            if (wifi.isHeld()) wifi.release();
        }
    }

    // ------------------------------------------------------------ notification

    private Notification note(String text) {
        Intent open = new Intent(this, SettingsActivity.class);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open, PendingIntent.FLAG_IMMUTABLE);
        Intent stop = new Intent(this, HolderService.class).setAction("stop");
        PendingIntent ps = PendingIntent.getService(this, 1, stop, PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, "kvholder")
                .setSmallIcon(android.R.drawable.ic_menu_share)
                .setContentTitle("Aither KV holder")
                .setContentText(text)
                .setOngoing(true)
                .setContentIntent(pi)
                .addAction(new Notification.Action.Builder(null, "Stop lending", ps).build())
                .build();
    }

    private void update(boolean force) {
        long now = System.currentTimeMillis();
        if (!force && now - lastNote < 5000) return;
        lastNote = now;
        String text = reason;
        if (lending) {
            try {
                JSONObject st = new JSONObject(lastStatus);
                text = st.optString("state", "lending") + " · " + st.optString("held", "0")
                        + " keys · " + st.optString("engine", "");
            } catch (Exception e) { /* keep reason */ }
        }
        getSystemService(NotificationManager.class).notify(1, note(text));
    }

    // ------------------------------------------------------------ pairing (Ed25519 key is in the page)

    private void pair(String pub) {
        final String code = cfg.pendingCode();
        if (code.isEmpty() || pairing) return;
        pairing = true;
        new Thread(() -> {
            String result;
            try {
                JSONObject body = new JSONObject()
                        .put("code", code)
                        .put("node_id", cfg.deviceId())
                        .put("hostname", cfg.deviceId())
                        .put("platform", "android")
                        .put("node_class", "phone")
                        .put("seal_pubkey", pub)
                        .put("cpu_count", Runtime.getRuntime().availableProcessors())
                        .put("capabilities", new org.json.JSONArray().put("kvholder"));
                URL u = new URL(cfg.identity() + "/v1/nodes/pairing/confirm");
                HttpURLConnection c = (HttpURLConnection) u.openConnection();
                c.setRequestMethod("POST");
                c.setConnectTimeout(20000);
                c.setReadTimeout(60000);
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", "application/json");
                c.setRequestProperty("User-Agent", "Mozilla/5.0 (Linux; Android) AitherKVHolder/0.1");
                try (OutputStream o = c.getOutputStream()) {
                    o.write(body.toString().getBytes(StandardCharsets.UTF_8));
                }
                int code2 = c.getResponseCode();
                if (code2 == 200) {
                    cfg.set("paired", true);
                    cfg.set("code", "");
                    result = "paired";
                } else {
                    InputStream es = c.getErrorStream();
                    result = "pairing refused (" + code2 + "): " + (es == null ? "" : read(es, 200));
                    if (code2 == 400) cfg.set("code", "");  // used or expired: ask for a new link
                }
            } catch (Exception e) {
                result = "pairing failed: " + e.getClass().getSimpleName();
            }
            final String r = result;
            main.post(() -> {
                pairing = false;
                reason = r;
                evaluate();
            });
        }).start();
    }

    private static String read(InputStream in, int max) throws java.io.IOException {
        ByteArrayOutputStream b = new ByteArrayOutputStream();
        byte[] buf = new byte[512];
        int n;
        while (b.size() < max && (n = in.read(buf)) > 0) b.write(buf, 0, n);
        String s = b.toString("UTF-8");
        return s.length() > max ? s.substring(0, max) : s;
    }

    /** What the page may call. It gets the relay and device id, never the pairing code. */
    final class Bridge {
        @JavascriptInterface
        public String getConfig() {
            return cfg.forPage();
        }

        @JavascriptInterface
        public void onKey(String pubHex) {
            if (pubHex == null || !pubHex.matches("[0-9a-f]{64}")) return;
            main.post(() -> {
                if (!pubHex.equals(cfg.pubkey())) cfg.set("pubkey", pubHex);
                pageReady = true;
                if (!cfg.paired() && !cfg.pendingCode().isEmpty()) pair(pubHex);
                evaluate();
            });
        }

        @JavascriptInterface
        public void onStatus(String json) {
            lastStatus = json == null ? "{}" : json;
            main.post(() -> {
                if (lending && lastStatus.contains("\"state\":\"attached\"")) reason = "lending";
                update(false);
            });
        }

        @JavascriptInterface
        public void onRefused(String why) {
            main.post(() -> {
                reason = "refused by the relay: " + why;
                update(true);
            });
        }
    }
}
