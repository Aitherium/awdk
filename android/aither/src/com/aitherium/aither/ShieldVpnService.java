package com.aitherium.aither;

import android.app.Activity;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.VpnService;
import android.os.Build;
import android.os.ParcelFileDescriptor;
import android.util.Base64;
import android.webkit.CookieManager;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.URL;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * Family Shield on a child's phone: every DNS lookup on the phone goes through the
 * household's filter, set by the guardian in the Family tab.
 *
 * A VpnService that routes ONE address through its tun: a fake DNS server, which Android
 * hands every app as its resolver. Normal traffic is not tunnelled. Each lookup is sent to
 * the household resolver (RFC 8484 GET, .../me/device/dns-query) with this phone's household
 * device token and the page's own session cookie, and the answer is written back: the real
 * address, a safe-search host, or a block. This app excludes itself from the tunnel so its
 * own lookups (and the resolver itself) never loop.
 *
 * Runs while the household's mode for this device is not "off" (from the check-in and the
 * policy endpoint). When the filter cannot answer (no network, signed out) the lookup
 * fails: a filter that fails open is no filter. The Aither app itself keeps working, so the
 * child can open it and sign in again.
 *
 * Android asks once ("Aither wants to set up a VPN connection"); MainActivity shows that
 * prompt when the guardian turns the filter on. Making it stick (Always-on VPN) is a
 * Settings step on the phone; see the Family Shield doc. When it does not stick (switched
 * off, or another VPN took over), ShieldWatch tells the household and asks the child.
 */
public class ShieldVpnService extends VpnService implements Runnable {
    static final String ACTION_STOP = "com.aitherium.aither.SHIELD_STOP";
    static final String TUN_ADDR = "10.111.222.1";
    static final String DNS_ADDR = "10.111.222.2";
    static final String CHANNEL = "family-shield";
    static final int NOTE_ID = 7;
    static final int CONSENT_NOTE_ID = 8;
    static final int CACHE_MAX = 512;
    static final long CACHE_MS = 60_000L;
    static final long POLICY_EVERY_S = 300;
    static volatile String last = "off";
    private static volatile ShieldVpnService running;

    private ParcelFileDescriptor tun;
    private FileOutputStream out;
    private Thread reader;
    private ExecutorService pool;
    private ScheduledExecutorService ticker;
    private final Map<String, Object[]> cache = new LinkedHashMap<String, Object[]>(64, 0.75f, true) {
        @Override
        protected boolean removeEldestEntry(Map.Entry<String, Object[]> e) {
            return size() > CACHE_MAX;
        }
    };

    // ---- when to run --------------------------------------------------------

    /** Should the filter be up? The household says a mode other than off for this phone. */
    static boolean wanted(Context c) {
        String mode = new Config(c).shieldMode();
        return !mode.isEmpty() && !"off".equals(mode) && !new Config(c).familyDevice().isEmpty();
    }

    static boolean isRunning() {
        return running != null;
    }

    /** Start or stop the filter to match the household. Safe from any thread or context. */
    static void sync(Context c) {
        ShieldVpnService s = running;
        if (!wanted(c)) {
            ShieldWatch.disarm(c);
            // Always-on VPN keeps the tunnel up as a plain resolver (mode off answers
            // everything): tearing it down would be the system's job, not ours.
            if (s != null && !s.isAlwaysOn()) s.shutdown();
            last = "off";
            return;
        }
        ShieldWatch.arm(c);
        if (s != null) return;
        if (VpnService.prepare(c) != null) {
            last = "waiting for permission on this phone";
            askLater(c);
            return;
        }
        try {
            c.startForegroundService(new Intent(c, ShieldVpnService.class));
        } catch (RuntimeException e) {
            last = "could not start: open Aither";
        }
    }

    /** From MainActivity: Android's one-time VPN prompt, when the filter is wanted. */
    static void askConsent(Activity a, int requestCode) {
        if (!wanted(a)) return;
        Intent consent = VpnService.prepare(a);
        if (consent == null) {
            sync(a);
            return;
        }
        try {
            a.startActivityForResult(consent, requestCode);
        } catch (RuntimeException e) {
            last = "this phone refused the VPN prompt";
        }
    }

    /** A notification that opens Aither, which then shows Android's prompt. */
    private static void askLater(Context c) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm == null) return;
        nm.createNotificationChannel(new NotificationChannel(CHANNEL, "Family Shield",
                NotificationManager.IMPORTANCE_LOW));
        PendingIntent pi = PendingIntent.getActivity(c, CONSENT_NOTE_ID,
                new Intent(c, MainActivity.class).setFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        nm.notify(CONSENT_NOTE_ID, new Notification.Builder(c, CHANNEL)
                .setSmallIcon(android.R.drawable.ic_lock_lock)
                .setContentTitle("Family Shield is on for this phone")
                .setContentText("Tap to finish setting it up")
                .setContentIntent(pi)
                .setAutoCancel(true)
                .build());
    }

    // ---- lifecycle ----------------------------------------------------------

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            shutdown();
            return START_NOT_STICKY;
        }
        Notification n = note("Filtering this phone's internet for your family");
        if (Build.VERSION.SDK_INT >= 34) startForeground(NOTE_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        else startForeground(NOTE_ID, n);
        if (tun == null && !establish()) {
            shutdown();
            return START_NOT_STICKY;
        }
        return START_STICKY;
    }

    private boolean establish() {
        try {
            Builder b = new Builder()
                    .setSession("Aither Family Shield")
                    .addAddress(TUN_ADDR, 32)
                    .addRoute(DNS_ADDR, 32) // only the fake resolver: other traffic is untouched
                    .addDnsServer(DNS_ADDR)
                    .setBlocking(true)
                    .setMtu(1500);
            b.addDisallowedApplication(getPackageName()); // the app and the resolver never loop
            b.setConfigureIntent(PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class),
                    PendingIntent.FLAG_IMMUTABLE));
            tun = b.establish();
        } catch (Exception e) {
            last = "could not start: " + e.getClass().getSimpleName();
            return false;
        }
        if (tun == null) { // permission was withdrawn
            last = "waiting for permission on this phone";
            return false;
        }
        out = new FileOutputStream(tun.getFileDescriptor());
        pool = Executors.newFixedThreadPool(4);
        ticker = Executors.newSingleThreadScheduledExecutor();
        ticker.scheduleWithFixedDelay(this::refreshPolicy, 5, POLICY_EVERY_S, TimeUnit.SECONDS);
        reader = new Thread(this, "aither-shield");
        reader.start();
        running = this;
        last = "on";
        new Config(this).set("shield_revoked", false);
        ShieldWatch.checkSoon(this); // an earlier "off" is cleared at the household
        return true;
    }

    /** The user (or another VPN app) took the VPN away: say so, and stop. */
    @Override
    public void onRevoke() {
        last = "turned off on this phone";
        new Config(this).set("shield_revoked", true);
        shutdown();
        // tell the household, ask the child; after a breath, so the VPN still up is another's
        if (wanted(this)) ShieldWatch.checkSoon(this, 2000);
    }

    @Override
    public void onDestroy() {
        shutdown();
        super.onDestroy();
    }

    synchronized void shutdown() {
        if (running == this) running = null;
        if (ticker != null) ticker.shutdownNow();
        if (pool != null) pool.shutdownNow();
        try {
            if (tun != null) tun.close(); // ends the reader's blocking read
        } catch (IOException e) {
            last = "closed with " + e.getClass().getSimpleName();
        }
        tun = null;
        stopForeground(STOP_FOREGROUND_REMOVE);
        stopSelf();
    }

    // ---- packets ------------------------------------------------------------

    @Override
    public void run() {
        byte[] dnsIp;
        try {
            dnsIp = InetAddress.getByName(DNS_ADDR).getAddress();
        } catch (IOException e) {
            return;
        }
        byte[] buf = new byte[32767];
        ParcelFileDescriptor fd = tun;
        if (fd == null) return;
        try (FileInputStream in = new FileInputStream(fd.getFileDescriptor())) {
            while (!Thread.currentThread().isInterrupted()) {
                int n = in.read(buf);
                if (n < 0) break;
                ShieldPackets.Query q = ShieldPackets.parse(buf, n, dnsIp);
                if (q == null) continue;
                ExecutorService p = pool;
                if (p == null || p.isShutdown()) break;
                p.execute(() -> answer(q));
            }
        } catch (IOException | java.util.concurrent.RejectedExecutionException e) {
            // the tun closed: shutdown() or onRevoke() is running
        }
    }

    private void answer(ShieldPackets.Query q) {
        byte[] dns = resolve(q.dns);
        byte[] pkt = ShieldPackets.reply(q, dns);
        synchronized (this) {
            try {
                if (out != null && tun != null) out.write(pkt);
            } catch (IOException e) {
                last = "write failed: " + e.getClass().getSimpleName();
            }
        }
    }

    /** The household resolver's answer, from a 60-second cache when it has one. */
    private byte[] resolve(byte[] query) {
        String key = ShieldPackets.key(query);
        long now = System.currentTimeMillis();
        synchronized (cache) {
            Object[] hit = cache.get(key);
            if (hit != null && (long) hit[1] > now) return ShieldPackets.withId((byte[]) hit[0], query);
        }
        String token = token(this);
        if (token.isEmpty()) return ShieldPackets.servfail(query);
        try {
            String q = Base64.encodeToString(query, Base64.URL_SAFE | Base64.NO_PADDING | Base64.NO_WRAP);
            HttpURLConnection c = open("/api/tutor/me/device/dns-query?dns=" + q, token);
            c.setRequestProperty("Accept", "application/dns-message");
            int code = c.getResponseCode();
            if (code != 200) {
                last = "filter answered " + code;
                return ShieldPackets.servfail(query);
            }
            byte[] ans = read(c.getInputStream(), 8192);
            if (ans.length < 12) return ShieldPackets.servfail(query);
            synchronized (cache) {
                cache.put(key, new Object[] {ans, now + CACHE_MS});
            }
            return ShieldPackets.withId(ans, query);
        } catch (IOException e) {
            last = "filter unreachable: " + e.getClass().getSimpleName();
            return ShieldPackets.servfail(query);
        }
    }

    /** Every few minutes: the household's policy (ETag'd); stop when it says off. */
    private void refreshPolicy() {
        Config cfg = new Config(this);
        String token = token(this);
        if (token.isEmpty()) return;
        try {
            HttpURLConnection c = open("/api/tutor/me/device/internet", token);
            String etag = cfg.shieldEtag();
            if (!etag.isEmpty()) c.setRequestProperty("If-None-Match", etag);
            int code = c.getResponseCode();
            if (code == 304) return;
            if (code == 410) { // removed from the household
                cfg.set("shield_mode", "off");
                sync(this);
                return;
            }
            if (code != 200) return;
            JSONObject j = new JSONObject(new String(read(c.getInputStream(), 65536),
                    java.nio.charset.StandardCharsets.UTF_8));
            JSONObject pol = j.optJSONObject("policy");
            cfg.set("shield_mode", pol == null ? "off" : pol.optString("mode", "off"));
            cfg.set("shield_etag", j.optString("etag", ""));
            synchronized (cache) {
                cache.clear(); // a new policy applies to the next lookup, not in a minute
            }
            sync(this);
        } catch (Exception e) {
            last = "policy check failed: " + e.getClass().getSimpleName();
        }
    }

    static String token(Context c) {
        String dev = new Config(c).familyDevice();
        if (dev.isEmpty()) return "";
        try {
            return new JSONObject(dev).optString("token", "");
        } catch (Exception e) {
            return "";
        }
    }

    private static HttpURLConnection open(String path, String token) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(HeartbeatJob.API + path).openConnection();
        c.setConnectTimeout(5000);
        c.setReadTimeout(5000);
        c.setUseCaches(false);
        c.setRequestProperty("X-Aither-Device-Token", token);
        c.setRequestProperty("User-Agent", "Mozilla/5.0 (Linux; Android) Aither/" + Config.VERSION);
        String cookie = CookieManager.getInstance().getCookie(HeartbeatJob.API);
        if (cookie != null) c.setRequestProperty("Cookie", cookie);
        return c;
    }

    private static byte[] read(InputStream in, int max) throws IOException {
        try (InputStream s = in) {
            ByteArrayOutputStream bo = new ByteArrayOutputStream();
            byte[] b = new byte[2048];
            int n;
            while ((n = s.read(b)) > 0) {
                bo.write(b, 0, n);
                if (bo.size() > max) throw new IOException("answer too large");
            }
            return bo.toByteArray();
        }
    }

    private Notification note(String text) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel(CHANNEL, "Family Shield",
                NotificationManager.IMPORTANCE_LOW));
        PendingIntent pi = PendingIntent.getActivity(this, NOTE_ID, new Intent(this, MainActivity.class),
                PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CHANNEL)
                .setSmallIcon(android.R.drawable.ic_lock_lock)
                .setContentTitle("Family Shield")
                .setContentText(text)
                .setContentIntent(pi)
                .setOngoing(true)
                .build();
    }
}
