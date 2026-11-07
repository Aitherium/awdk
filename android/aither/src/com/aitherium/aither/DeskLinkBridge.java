package com.aitherium.aither;

import android.app.Activity;
import android.os.Handler;
import android.os.Looper;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * window.AitherDeskLink: the page drives the owner's PC from this phone (DeskLink).
 *
 * request(id, method, path, body) answers later through
 * window.__aitherDeskLinkResult(id, status, body, transport, ms); status 0 means it never
 * reached the desk (no transport answered) or was refused here, and body is the reason.
 * One session, three transports, tried in DeskLink.TRANSPORTS order with the last one that
 * worked first: the USB cable (adb reverse), the tailnet (the desk's mesh address, learned
 * from an approved hello over the cable), and tunnel.aitherium.com (Cloudflare Access: the
 * owner signs in there once in this app; its cookie rides along). The calls and the signed
 * hello are the same on all three. Signed calls get a fresh hello from desk-link.html, an
 * off-screen WebView on the holder's origin, so the private key never leaves WebCrypto.
 * Never on a child's phone.
 */
final class DeskLinkBridge {
    private static final int MAX_ANSWER = 32 << 20;   // /link/do file.get answers in base64
    private static final Handler MAIN = new Handler(Looper.getMainLooper());
    private static final long PREFER_MS = 60_000;

    // One signer page per process, shared by every activity that registers the bridge. It is
    // created on first use and never destroyed (only replaced if its renderer dies): one idle
    // off-screen WebView for the life of the process, so a signature never waits on a page load.
    private static WebView signer;
    private static volatile CountDownLatch signerReady = new CountDownLatch(1);
    private static final Map<String, BlockingQueue<String[]>> SIGNING = new ConcurrentHashMap<>();
    private static volatile String deskId;            // from the last /link/info or approved hello
    private static volatile String lastGood = "";     // the transport that answered last
    private static volatile long lastGoodAt;

    private final Activity act;
    private final WebView web;

    DeskLinkBridge(Activity a, WebView w) {
        act = a;
        web = w;
    }

    private boolean child() {
        Config cfg = new Config(act);
        return DeviceLink.childRefusal(cfg.profileKind(), cfg.childDevice()) != null;
    }

    /** desk_id + mesh_addr, kept from the last approved hello (cleared on Forget). */
    private android.content.SharedPreferences prefs() {
        return act.getApplicationContext().getSharedPreferences("desklink", android.content.Context.MODE_PRIVATE);
    }

    @JavascriptInterface
    public boolean available() {
        return !child();
    }

    @JavascriptInterface
    public void request(String id, String method, String path, String bodyJson) {
        if (id == null) return;
        if (child()) { deliver(id, 0, "refused: not on a child's phone", "", 0); return; }
        if (!DeskLink.pathAllowed(method, path)) { deliver(id, 0, "refused: " + method + " " + path, "", 0); return; }
        new Thread(() -> {
            try {
                // the bridge rides the app's WebView: only our own pages may drive the PC
                if (!ourPage()) { deliver(id, 0, "refused: not an Aither page", "", 0); return; }
                String hello = null;
                if (DeskLink.signed(path)) {
                    if (deskId == null) deskId = prefs().getString("desk_id", null);
                    if (deskId == null) {
                        String[] info = callAny("GET", "/link/info", null, null);
                        if (!"200".equals(info[0])) { deliver(id, Integer.parseInt(info[0]), info[1], info[2], Long.parseLong(info[3])); return; }
                        remember(info[1]);
                    }
                    String desk = deskId;
                    if (!DeskLink.validDeskId(desk)) { deliver(id, 0, "the desk did not say who it is", "", 0); return; }
                    String node = new NodeLink(act).nodeId();
                    if (node.isEmpty()) { deliver(id, 0, "this phone is not linked yet (open Aither once and link this phone)", "", 0); return; }
                    String[] h = sign(DeskLink.audience(desk), node);
                    if (h[1] == null) { deliver(id, 0, h[0], "", 0); return; }
                    hello = DeskLink.helloHeader(h[1]);
                }
                String[] r = callAny(method, path, "POST".equals(method) ? bodyJson : null, hello);
                if ("200".equals(r[0])) {
                    if ("/link/info".equals(path)) remember(r[1]);
                    else if ("/link/hello".equals(path)) keepApproved(r[1]);
                    else if ("/link/revoke".equals(path)) { prefs().edit().clear().apply(); deskId = null; }
                }
                deliver(id, Integer.parseInt(r[0]), r[1], r[2], Long.parseLong(r[3]));
            } catch (IOException e) {
                deliver(id, 0, "PC offline: no cable, mesh or tunnel answered (" + e.getMessage() + ")", "", 0);
            } catch (Exception e) {
                deliver(id, 0, "desk link failed: " + e, "", 0);
            }
        }, "desk-link").start();
    }

    /** The page asking is one of ours (McpPolicy.ourPage). WebView.getUrl is main-thread only. */
    private boolean ourPage() throws InterruptedException {
        final String[] url = {null};
        CountDownLatch done = new CountDownLatch(1);
        MAIN.post(() -> { url[0] = web.getUrl(); done.countDown(); });
        return done.await(2, TimeUnit.SECONDS) && McpPolicy.ourPage(url[0]);
    }

    private static void remember(String infoJson) {
        try {
            String d = new JSONObject(infoJson).optString(DeskLink.DESK_ID_FIELD, "");
            deskId = DeskLink.validDeskId(d) ? d : null;
        } catch (Exception e) {
            deskId = null;
        }
    }

    /** An approved hello names the desk and, when it has one, its tailnet address. */
    private void keepApproved(String helloJson) {
        try {
            JSONObject o = new JSONObject(helloJson);
            if (!"approved".equals(o.optString("state"))) return;
            android.content.SharedPreferences.Editor e = prefs().edit();
            String d = o.optString(DeskLink.DESK_ID_FIELD, "");
            if (DeskLink.validDeskId(d)) { e.putString("desk_id", d); deskId = d; }
            JSONObject m = o.optJSONObject("mesh");
            String addr = m == null ? "" : m.optString("addr", "");
            if (DeskLink.validMeshAddr(addr)) e.putString("mesh_addr", addr);
            e.apply();
        } catch (Exception ignored) {
            // an answer without the fields changes nothing
        }
    }

    /** {status, body, transport, ms}: the first transport that ANSWERS (any HTTP status). */
    private String[] callAny(String method, String path, String body, String hello) throws IOException {
        String mesh = prefs().getString("mesh_addr", "");
        java.util.List<String> order = new java.util.ArrayList<>(java.util.Arrays.asList(DeskLink.TRANSPORTS));
        if (!lastGood.isEmpty() && System.currentTimeMillis() - lastGoodAt < PREFER_MS && order.remove(lastGood)) {
            order.add(0, lastGood);
        }
        IOException last = null;
        for (String t : order) {
            String url = DeskLink.url(t, mesh, path);
            if (url == null) continue;
            long t0 = android.os.SystemClock.elapsedRealtime();
            try {
                String[] r = call(t, url, method, body, hello);
                lastGood = t;
                lastGoodAt = System.currentTimeMillis();
                return new String[] {r[0], r[1], t, String.valueOf(android.os.SystemClock.elapsedRealtime() - t0)};
            } catch (IOException e) {
                last = e;
            }
        }
        throw last != null ? last : new IOException("link this phone over the USB cable first");
    }

    /** {status, body}; an IOException when nothing answers on that transport. */
    private static String[] call(String transport, String url, String method, String body, String hello) throws IOException {
        String cookie = null;
        if ("remote".equals(transport)) {
            cookie = android.webkit.CookieManager.getInstance().getCookie("https://tunnel.aitherium.com");
            if (cookie == null || cookie.isEmpty()) throw new IOException("not signed in to tunnel.aitherium.com");
        }
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        try {
            c.setRequestMethod(method);
            c.setInstanceFollowRedirects(false);  // an Access sign-in redirect is an answer, not the desk
            c.setConnectTimeout("cable".equals(transport) ? 1500 : 3000);
            c.setReadTimeout(30000);
            c.setRequestProperty("Content-Type", "application/json");
            if (cookie != null) c.setRequestProperty("Cookie", cookie);
            if (hello != null) c.setRequestProperty(DeskLink.HEADER, hello);
            if (body != null) {
                c.setDoOutput(true);
                try (OutputStream o = c.getOutputStream()) {
                    o.write((body.isEmpty() ? "{}" : body).getBytes(StandardCharsets.UTF_8));
                }
            }
            int st = c.getResponseCode();
            if ("remote".equals(transport) && st >= 300 && st < 400) {
                return new String[] {"401", "{\"error\":\"sign in to tunnel.aitherium.com in Aither once\"}"};
            }
            InputStream in = st < 400 ? c.getInputStream() : c.getErrorStream();
            String text = in == null ? "" : read(in);
            if ("remote".equals(transport) && st >= 500 && !text.trim().startsWith("{")) {
                // Cloudflare's own error page: the fleet serving the tunnel lives on the PC
                return new String[] {"503", "{\"error\":\"PC offline\",\"offline\":true}"};
            }
            return new String[] {String.valueOf(st), text};
        } finally {
            c.disconnect();
        }
    }

    private static String read(InputStream in) throws IOException {
        try (InputStream i = in) {
            ByteArrayOutputStream b = new ByteArrayOutputStream();
            byte[] buf = new byte[65536];
            int n;
            while ((n = i.read(buf)) > 0) {
                if (b.size() + n > MAX_ANSWER) throw new IOException("answer over 32 MB");
                b.write(buf, 0, n);
            }
            return b.toString("UTF-8");
        }
    }

    private void deliver(String id, int status, String body, String transport, long ms) {
        String js = "window.__aitherDeskLinkResult&&window.__aitherDeskLinkResult(" + JSONObject.quote(id)
                + "," + status + "," + JSONObject.quote(body == null ? "" : body) + ","
                + JSONObject.quote(transport == null ? "" : transport) + "," + ms + ")";
        MAIN.post(() -> web.evaluateJavascript(js, null));
    }

    // ------------------------------------------------------------ the signer page

    /** {error, null} or {null, helloJson}. Blocks the calling (background) thread up to 15 s. */
    private String[] sign(String relay, String node) throws InterruptedException {
        MAIN.post(this::startSigner);
        if (!signerReady.await(10, TimeUnit.SECONDS)) return new String[] {"the signer page did not start", null};
        String rid = Long.toHexString(System.nanoTime()) + Integer.toHexString(System.identityHashCode(relay));
        BlockingQueue<String[]> q = new ArrayBlockingQueue<>(1);
        SIGNING.put(rid, q);
        try {
            String js = "deskHello(" + JSONObject.quote(rid) + "," + JSONObject.quote(relay)
                    + "," + JSONObject.quote(node) + ")";
            MAIN.post(() -> { if (signer != null) signer.evaluateJavascript(js, null); });
            String[] r = q.poll(5, TimeUnit.SECONDS);
            return r != null ? r : new String[] {"the signer page did not answer", null};
        } finally {
            SIGNING.remove(rid);
        }
    }

    /** Main thread. Not attached to any window: it is never shown. */
    private void startSigner() {
        if (signer != null) return;
        WebView w = new WebView(act.getApplicationContext());
        WebSettings s = w.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        w.addJavascriptInterface(new Signer(), "AitherDeskSigner");
        w.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView v, WebResourceRequest r) {
                String name = DeskLink.assetName(r.getUrl().toString());
                if (name == null) return null;
                try {
                    return new WebResourceResponse(name.endsWith(".html") ? "text/html" : "text/javascript",
                            "utf-8", v.getContext().getAssets().open(name));
                } catch (IOException e) {
                    return null;
                }
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                return true;  // it goes nowhere
            }

            @Override
            public boolean onRenderProcessGone(WebView v, android.webkit.RenderProcessGoneDetail d) {
                if (v == signer) {
                    signerReady = new CountDownLatch(1);  // before null: a waiter must not see the old one
                    signer = null;
                    v.destroy();
                }  // the next request starts another
                return true;
            }
        });
        signer = w;
        w.loadUrl(DeskLink.ORIGIN + "desk-link.html");
    }

    /** What desk-link.html may call: three callbacks, nothing that reads or changes state. */
    static final class Signer {
        @JavascriptInterface
        public void ready() {
            signerReady.countDown();
        }

        @JavascriptInterface
        public void onHello(String rid, String json) {
            BlockingQueue<String[]> q = rid == null ? null : SIGNING.get(rid);
            if (q != null) q.offer(new String[] {null, json});
        }

        @JavascriptInterface
        public void onError(String rid, String why) {
            BlockingQueue<String[]> q = rid == null ? null : SIGNING.get(rid);
            if (q != null) q.offer(new String[] {why == null ? "signing failed" : why, null});
        }
    }
}
