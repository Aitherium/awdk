package com.aitherium.aither;

import android.app.ActivityManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.ComponentCallbacks2;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;
import java.security.SecureRandom;

/**
 * The owner's small model on this phone, natively: llama.cpp's llama-server (shipped in the
 * APK as libllamaserver.so) with Bonsai 1.7B (Q1_0, 248 MB, fetched once and checked against
 * its pinned SHA-256), behind LocalProxy on 127.0.0.1:8486.
 *
 * The model loads on the first request and unloads after IDLE_MS without one, so the phone
 * pays for it only while AitherOS uses it. It refuses to load on a child's phone, when free
 * memory is under the model plus headroom, or while the phone is hot; it unloads on a
 * low-memory signal or when the phone gets hot.
 */
public class LlmService extends Service implements LocalProxy.Backend {
    static final String MODEL_ID = "bonsai-1.7b";
    static final String MODEL_FILE = "Bonsai-1.7B-Q1_0.gguf";
    static final String MODEL_URL = "https://weights.aitherium.com/" + MODEL_FILE;
    static final String MODEL_SHA256 = "3d7c6c90dd98717a203adb22d5eacd2581850e40aa5327e144b97766cae5f7e3";
    static final long MODEL_BYTES = 248302272L;
    // measured on a Pixel 10 Pro Fold: RSS 720 MB (245 MB of it the mmapped model), 1.4 s load,
    // 37-41 tok/s; the rest is the repacked weights, KV cache and buffers, plus room for the phone
    static final long HEADROOM = 700L << 20;
    static final int INNER_PORT = 18486;
    static final long IDLE_MS = 10 * 60 * 1000;

    static volatile String reason = "off";
    static volatile long loadMs, startedAt;

    private Config cfg;
    private LocalProxy proxy;
    private Process proc;
    private String key;
    private volatile long lastUse;
    private final Object lock = new Object();
    private Thread idleWatch;
    private FamilyShare share;

    @Override
    public void onCreate() {
        super.onCreate();
        cfg = new Config(this);
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel(
                "local-ai", "AI on this phone", NotificationManager.IMPORTANCE_MIN));
        Notification n = note("Ready on this phone");
        if (Build.VERSION.SDK_INT >= 34) startForeground(2, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        else startForeground(2, n);
        proxy = new LocalProxy(cfg, this);
        try {
            proxy.start();
            reason = "ready (loads on first use)";
        } catch (Exception e) {
            reason = "port " + LocalProxy.PORT + " is taken";
            stopSelf();
            return;
        }
        PowerManager pm = getSystemService(PowerManager.class);
        pm.addThermalStatusListener(status -> {
            if (status >= PowerManager.THERMAL_STATUS_SEVERE) unload("unloaded: the phone is hot");
        });
        idleWatch = new Thread(() -> {
            while (true) {
                try { Thread.sleep(30000); } catch (InterruptedException e) { return; }
                if (proc != null && System.currentTimeMillis() - lastUse > IDLE_MS) unload("unloaded: idle");
            }
        }, "aither-llm-idle");
        idleWatch.setDaemon(true);
        idleWatch.start();
        share = new FamilyShare(this, this);
        if (cfg.shareFamily()) share.start();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if ((intent != null && "stop".equals(intent.getAction())) || !cfg.llmEnabled()) {
            stopSelf();
        } else if (share != null) {
            if (cfg.shareFamily()) share.start();
            else share.stop();
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        if (share != null) share.stop();
        unload("off");
        if (proxy != null) proxy.stop();
        if (idleWatch != null) idleWatch.interrupt();
        super.onDestroy();
    }

    @Override
    public void onTrimMemory(int level) {
        super.onTrimMemory(level);
        if (level >= ComponentCallbacks2.TRIM_MEMORY_RUNNING_CRITICAL) unload("unloaded: the phone is low on memory");
    }

    @Override
    public IBinder onBind(Intent i) {
        return null;
    }

    // ------------------------------------------------------------ LocalProxy.Backend

    @Override public int port() { return INNER_PORT; }
    @Override public String key() { return key; }
    @Override public String modelId() { return MODEL_ID; }
    @Override public boolean loaded() { return proc != null; }
    @Override public void touched() { lastUse = System.currentTimeMillis(); }

    @Override
    public String ensure() {
        return load(cfg.localAiBlocked());
    }

    @Override
    public String ensurePool() {
        return load(cfg.poolBlocked());
    }

    private String load(String blocked) {
        synchronized (lock) {
            if (proc != null && proc.isAlive()) return "";
            proc = null;
            if (!blocked.isEmpty()) return reason = blocked;
            PowerManager pm = getSystemService(PowerManager.class);
            if (pm.getCurrentThermalStatus() >= PowerManager.THERMAL_STATUS_SEVERE) {
                return reason = "the phone is hot; try again in a few minutes";
            }
            File bin = new File(getApplicationInfo().nativeLibraryDir, "libllamaserver.so");
            if (!bin.exists()) return reason = "this build has no local model engine";
            File model = new File(getFilesDir(), "models/" + MODEL_FILE);
            if (!model.exists() || model.length() != MODEL_BYTES) {
                reason = "downloading the model (248 MB)";
                String err = download(model);
                if (!err.isEmpty()) return reason = err;
            }
            ActivityManager.MemoryInfo mi = new ActivityManager.MemoryInfo();
            getSystemService(ActivityManager.class).getMemoryInfo(mi);
            if (mi.availMem < MODEL_BYTES + HEADROOM || mi.lowMemory) {
                return reason = "not enough free memory (" + (mi.availMem >> 20) + " MB free)";
            }
            byte[] k = new byte[24];
            new SecureRandom().nextBytes(k);
            key = android.util.Base64.encodeToString(k, android.util.Base64.NO_WRAP | android.util.Base64.URL_SAFE);
            long t0 = System.currentTimeMillis();
            try {
                ProcessBuilder pb = new ProcessBuilder(bin.getAbsolutePath(),
                        "-m", model.getAbsolutePath(), "--host", "127.0.0.1", "--port", String.valueOf(INNER_PORT),
                        "--api-key", key, "--alias", MODEL_ID, "-c", "2048", "-np", "1", "-t", "4",
                        "-ub", "128", "--cache-ram", "0", "--no-webui",
                        "--jinja"); // the model's own chat template: tool calls for the on-device agent
                pb.redirectErrorStream(true);
                pb.redirectOutput(new File(getFilesDir(), "llm.log"));
                proc = pb.start();
            } catch (Exception e) {
                proc = null;
                return reason = "the model engine did not start";
            }
            for (int i = 0; i < 600; i++) { // up to 60 s
                if (!proc.isAlive()) { proc = null; return reason = "the model engine exited (see llm.log)"; }
                if (healthy()) {
                    loadMs = System.currentTimeMillis() - t0;
                    startedAt = System.currentTimeMillis();
                    lastUse = startedAt;
                    reason = "loaded in " + loadMs + " ms";
                    update();
                    return "";
                }
                try { Thread.sleep(100); } catch (InterruptedException e) { break; }
            }
            unload("the model did not come up");
            return reason;
        }
    }

    private boolean healthy() {
        try {
            HttpURLConnection c = (HttpURLConnection) new URL("http://127.0.0.1:" + INNER_PORT + "/health").openConnection();
            c.setConnectTimeout(500);
            c.setReadTimeout(1000);
            return c.getResponseCode() == 200;
        } catch (Exception e) {
            return false;
        }
    }

    private void unload(String why) {
        synchronized (lock) {
            if (proc != null) {
                proc.destroy();
                proc = null;
            }
            reason = why;
        }
        update();
    }

    /** Fetch the model once; keep it only if its SHA-256 is the pinned one. */
    private String download(File model) {
        File part = new File(model.getPath() + ".part");
        model.getParentFile().mkdirs();
        try {
            HttpURLConnection c = (HttpURLConnection) new URL(MODEL_URL).openConnection();
            c.setRequestProperty("User-Agent", "Mozilla/5.0 (Linux; Android) Aither/0.2");
            c.setConnectTimeout(20000);
            c.setReadTimeout(60000);
            if (c.getResponseCode() != 200) return "model download refused (" + c.getResponseCode() + ")";
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            try (InputStream in = c.getInputStream(); OutputStream out = new FileOutputStream(part)) {
                byte[] buf = new byte[1 << 16];
                int n;
                while ((n = in.read(buf)) > 0) {
                    out.write(buf, 0, n);
                    md.update(buf, 0, n);
                }
            }
            StringBuilder hex = new StringBuilder();
            for (byte b : md.digest()) hex.append(String.format("%02x", b & 0xff));
            if (!MODEL_SHA256.equals(hex.toString())) {
                part.delete();
                return "the model download was corrupt; will retry";
            }
            if (!part.renameTo(model)) return "could not store the model";
            return "";
        } catch (Exception e) {
            part.delete();
            return "model download failed: " + e.getClass().getSimpleName();
        }
    }

    // ------------------------------------------------------------ notification

    private Notification note(String text) {
        PendingIntent pi = PendingIntent.getActivity(this, 2, new Intent(this, SettingsActivity.class),
                PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, "local-ai")
                .setSmallIcon(android.R.drawable.ic_menu_manage)
                .setContentTitle("AI on this phone")
                .setContentText(text)
                .setOngoing(true)
                .setContentIntent(pi)
                .build();
    }

    private void update() {
        getSystemService(NotificationManager.class).notify(2, note(proc != null ? "Bonsai 1.7B loaded" : reason));
    }
}
