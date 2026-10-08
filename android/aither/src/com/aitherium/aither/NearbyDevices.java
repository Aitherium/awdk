package com.aitherium.aither;

import android.app.Activity;
import android.content.Context;
import android.net.nsd.NsdManager;
import android.net.nsd.NsdServiceInfo;
import android.os.Handler;
import android.os.Looper;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import java.net.InetAddress;
import java.util.ArrayDeque;
import java.util.HashSet;
import java.util.Set;

/**
 * "Nearby devices" on this phone: Android's own mDNS (NsdManager) browses
 * _aither-pair._tcp while the Control page asks, for at most 5 minutes, and keeps what it
 * hears in a NearbyBook (untrusted, rate-limited, replay-checked). The page reads it through
 * window.AitherNearby and "Approve" loads the signed-in approval page for ONE listed rid in
 * this WebView, where the device's 6-digit code is typed; Identity decides.
 *
 * One session per app process, so several WebViews never start several discoveries. Not on
 * a child's phone: a child account never approves a device (Identity refuses it as well).
 * Nothing heard is logged; the advertised host and port are never contacted.
 */
final class NearbyDevices {
    private static final int MAX_PENDING_RESOLVES = 16;
    private static NearbyDevices session;

    private final NsdManager nsd;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final NearbyBook book = NearbyBook.standard();
    private final ArrayDeque<NsdServiceInfo> pending = new ArrayDeque<>();
    private final Set<String> queued = new HashSet<>();
    private NsdManager.DiscoveryListener discovery;
    private boolean resolving;
    private long until;
    private final Runnable timeUp = this::stop;

    private NearbyDevices(Context ctx) {
        nsd = (NsdManager) ctx.getApplicationContext().getSystemService(Context.NSD_SERVICE);
    }

    static synchronized NearbyDevices of(Context ctx) {
        if (session == null) session = new NearbyDevices(ctx);
        return session;
    }

    synchronized long start() {
        stop();
        book.clear();
        if (nsd == null) return 0;
        discovery = new NsdManager.DiscoveryListener() {
            @Override public void onDiscoveryStarted(String type) {}
            @Override public void onDiscoveryStopped(String type) {}
            @Override public void onStartDiscoveryFailed(String type, int err) { stop(); }
            @Override public void onStopDiscoveryFailed(String type, int err) {}
            @Override public void onServiceLost(NsdServiceInfo info) {}

            @Override public void onServiceFound(NsdServiceInfo info) { enqueue(info); }
        };
        try {
            nsd.discoverServices(NearbyBook.SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, discovery);
        } catch (RuntimeException e) {
            discovery = null;
            return 0;
        }
        until = System.currentTimeMillis() + NearbyBook.MAX_WINDOW_MS;
        main.postDelayed(timeUp, NearbyBook.MAX_WINDOW_MS);
        return until;
    }

    synchronized void stop() {
        main.removeCallbacks(timeUp);
        if (discovery != null && nsd != null) {
            try { nsd.stopServiceDiscovery(discovery); } catch (RuntimeException e) { /* stopped */ }
        }
        discovery = null;
        pending.clear();
        queued.clear();
        until = 0;
    }

    synchronized boolean listening() { return discovery != null; }

    private synchronized void enqueue(NsdServiceInfo info) {
        String name = info.getServiceName();
        // bounded: a flood of names cannot queue unbounded resolves
        if (discovery == null || name == null || queued.contains(name)
                || pending.size() >= MAX_PENDING_RESOLVES) return;
        queued.add(name);
        pending.add(info);
        next();
    }

    @SuppressWarnings("deprecation") // resolveService: the one call every API 29+ phone has
    private synchronized void next() {
        if (resolving || pending.isEmpty() || discovery == null) return;
        resolving = true;
        NsdServiceInfo info = pending.poll();
        try {
            nsd.resolveService(info, new NsdManager.ResolveListener() {
                @Override public void onResolveFailed(NsdServiceInfo s, int err) { done(); }

                @Override public void onServiceResolved(NsdServiceInfo s) {
                    InetAddress host = s.getHost();
                    book.offer(s.getAttributes(), s.getServiceName(),
                            host == null ? "" : host.getHostAddress());
                    done();
                }
            });
        } catch (RuntimeException e) {
            done();
        }
    }

    private synchronized void done() {
        resolving = false;
        next();
    }

    /** window.AitherNearby: start (5 minutes), the list, approve one listed rid, stop. */
    static final class Bridge {
        private final Activity act;
        private final WebView web;

        Bridge(Activity a, WebView w) {
            act = a;
            web = w;
        }

        /** The phone's child flag OR the account's kind, as every child refusal in the app. */
        private boolean child() {
            Config cfg = new Config(act);
            return DeviceLink.childRefusal(cfg.profileKind(), cfg.childDevice()) != null;
        }

        /** {"listening":bool,"until":ms} -- never on a child's phone. */
        @JavascriptInterface
        public String start() {
            if (child()) return "{\"listening\":false,\"error\":\"child\"}";
            long u = of(act).start();
            return u > 0 ? "{\"listening\":true,\"until\":" + u + "}"
                    : "{\"listening\":false,\"error\":\"mdns-unavailable\"}";
        }

        @JavascriptInterface
        public String list() {
            NearbyDevices s = of(act);
            return "{\"listening\":" + s.listening() + ",\"devices\":" + s.book.json() + "}";
        }

        /** Loads the approval page for a rid that is listed right now; false otherwise. */
        @JavascriptInterface
        public boolean approve(String rid) {
            if (child()) return false;
            NearbyDevices s = of(act);
            if (!s.book.has(rid)) return false;
            String url = NearbyBook.approveUrl(rid);
            if (url == null) return false;
            act.runOnUiThread(() -> web.loadUrl(url));
            return true;
        }

        @JavascriptInterface
        public void stop() { of(act).stop(); }
    }
}
