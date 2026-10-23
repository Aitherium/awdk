package com.aitherium.aither;

import android.app.ActivityManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.ServiceInfo;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.BatteryManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

import org.json.JSONObject;

import java.io.File;
import java.io.FileDescriptor;
import java.io.PrintWriter;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.List;
import java.util.concurrent.TimeUnit;

/**
 * "Help my fleet": this phone runs its own Bonsai 1.7B as an OpenAI-compatible llama-server on
 * its Tailscale address, behind its worker key, with {@code -np <parallel jobs>} slots, and the
 * fleet's MicroScheduler sends it whole small jobs (lib/compute/family_pool.worker_plan). The
 * rules are {@link Worker}'s; this is the foreground service that applies them.
 *
 * A loop checks the conditions every {@link #TICK_MS}: when they hold and no server runs, it
 * starts one; when they fail (unplugged, off Wi-Fi, hot, off the mesh, turned off) it stops
 * the server at once and says why, and starts it again when they hold. While on it also checks
 * in with Identity every {@link #BEAT_MS}, so the fleet learns within a minute that the worker
 * came or went (the 15-minute job alone is far too slow for that), and so a worker-on/off
 * command from the owner arrives within a minute.
 *
 * {@code adb shell dumpsys activity service com.aitherium.aither/.WorkerService}.
 */
public class WorkerService extends Service {
    static final int NOTE_ID = 12;
    static final String CHANNEL = "fleet-worker";
    static final long TICK_MS = 10_000;
    static final long BEAT_MS = 60_000;
    static final long START_WAIT_MS = 60_000;
    /** Per slot: a 2048-token KV cache of the 1.7B plus buffers, measured ~230 MB; kept generous. */
    static final long SLOT_MB = 320;
    static final long BASE_HEADROOM_MB = 700;

    static volatile String state = "off";
    static volatile boolean serving;
    static volatile String url = "";
    static volatile int slots;
    static volatile String keyPrint = "";
    static volatile long startedAt;

    private static final Object LOCK = new Object();
    private Process proc;
    private String runningKey = "";
    private int runningSlots;
    private Thread loop;
    private volatile boolean stop;
    private Config cfg;
    private int healthFails;
    private long lastBeat;

    /** Apply the setting: start the service when it is on, stop it when it is off. */
    static void sync(Context c) {
        Config cfg = new Config(c);
        Intent i = new Intent(c, WorkerService.class);
        try {
            if (cfg.workerOn()) c.startForegroundService(i);
            else c.stopService(i);
        } catch (RuntimeException e) {
            state = "Android refused to start the worker now; it starts when Aither next runs";
        }
    }

    /** What collect-diagnostics, the heartbeat and every worker-on result report. */
    static JSONObject report(JSONObject o) throws Exception {
        o.put("state", state).put("serving", serving);
        if (serving) {
            o.put("url", url).put("model", Worker.PHONE_MODEL).put("slots", slots)
                    .put("key_fingerprint", keyPrint).put("started_at", startedAt / 1000);
        }
        return o;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        cfg = new Config(this);
        getSystemService(NotificationManager.class).createNotificationChannel(new NotificationChannel(
                CHANNEL, "Helping your fleet", NotificationManager.IMPORTANCE_MIN));
        Notification n = note("Starting");
        if (Build.VERSION.SDK_INT >= 34) startForeground(NOTE_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        else startForeground(NOTE_ID, n);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if ((intent != null && "stop".equals(intent.getAction())) || !cfg.workerOn()) {
            stopSelf();
            return START_NOT_STICKY;
        }
        if (loop == null) {
            stop = false;
            loop = new Thread(this::run, "aither-fleet-worker");
            loop.setDaemon(true);
            loop.start();
        } else {
            loop.interrupt(); // a setting changed: re-check now
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        stop = true;
        if (loop != null) loop.interrupt();
        kill("off");
        new Thread(this::beat, "aither-fleet-worker-off").start(); // tell the fleet now
        super.onDestroy();
    }

    @Override
    protected void dump(FileDescriptor fd, PrintWriter out, String[] args) {
        out.println("fleet worker: " + state + (serving ? " " + url + " slots=" + slots
                + " key=" + keyPrint : "") + " on=" + cfg.workerOn() + " parallel="
                + cfg.workerParallel() + " guard=" + cfg.workerGuard());
    }

    @Override
    public IBinder onBind(Intent i) {
        return null;
    }

    // ------------------------------------------------------------ the loop

    private void run() {
        while (!stop) {
            boolean changed;
            try {
                changed = tick();
            } catch (Exception e) {
                state = "error: " + e.getClass().getSimpleName() + "; retrying";
                changed = false;
            }
            long now = System.currentTimeMillis();
            if (changed || now - lastBeat > BEAT_MS) beat();
            note(state);
            try { Thread.sleep(TICK_MS); } catch (InterruptedException e) { /* re-check now */ }
        }
    }

    /** One check. True when the server started or stopped (the fleet should hear now). */
    private boolean tick() {
        NodeLink node = new NodeLink(this);
        String ip = MeshLend.pickAddress(MeshLendService.addresses(this));
        String key = Worker.apiKey(node.key(), node.nodeId());
        PowerManager pm = getSystemService(PowerManager.class);
        boolean hot = pm.getCurrentThermalStatus() >= PowerManager.THERMAL_STATUS_SEVERE;
        String why = Worker.paused(cfg.workerOn(), cfg.childDevice(), ip, key, hot,
                cfg.workerGuard(), charging(), wifi());
        int np = Worker.parallel(cfg.workerParallel());
        boolean alive = proc != null && proc.isAlive();
        if (!why.isEmpty()) {
            boolean was = kill(why);
            return was;
        }
        if (alive && (!key.equals(runningKey) || np != runningSlots || !url.contains("//" + ip + ":"))) {
            kill("restarting with the new settings");
            alive = false;
        }
        if (alive) {
            if (healthy(ip)) {
                healthFails = 0;
            } else if (++healthFails >= 3) {
                kill("restarting: the server stopped answering");
                return true;
            }
            return false;
        }
        String err = start(ip, key, np);
        if (!err.isEmpty()) {
            state = err;
            return false;
        }
        return true;
    }

    /** "" when the server is up on ip:PORT; otherwise why not. */
    private String start(String ip, String key, int np) {
        File bin = new File(getApplicationInfo().nativeLibraryDir, "libllamaserver.so");
        if (!bin.exists()) return "this build has no local model engine";
        File model = new File(getFilesDir(), "models/" + LlmService.MODEL_FILE);
        if (!model.exists() || model.length() != LlmService.MODEL_BYTES) {
            state = "downloading the model (248 MB)";
            String err = LlmService.download(model);
            if (!err.isEmpty()) return err;
        }
        ActivityManager.MemoryInfo mi = new ActivityManager.MemoryInfo();
        getSystemService(ActivityManager.class).getMemoryInfo(mi);
        long needMb = (LlmService.MODEL_BYTES >> 20) + BASE_HEADROOM_MB + SLOT_MB * np;
        if (mi.lowMemory || (mi.availMem >> 20) < needMb) {
            return "waiting: not enough free memory (" + (mi.availMem >> 20) + " MB free, "
                    + needMb + " MB needed for " + np + " jobs)";
        }
        List<String> argv = Worker.argv(bin.getAbsolutePath(), model.getAbsolutePath(), ip, key, np,
                Math.min(4, Runtime.getRuntime().availableProcessors()));
        Process p;
        try {
            p = new ProcessBuilder(argv).redirectErrorStream(true)
                    .redirectOutput(new File(getFilesDir(), "worker.log")).start();
        } catch (Exception e) {
            return "the model engine did not start (" + e.getClass().getSimpleName() + ")";
        }
        synchronized (LOCK) {
            proc = p;
        }
        long until = System.currentTimeMillis() + START_WAIT_MS;
        while (System.currentTimeMillis() < until) {
            if (!p.isAlive()) {
                synchronized (LOCK) {
                    proc = null;
                }
                return "the model engine exited " + p.exitValue() + " (see worker.log)";
            }
            if (healthy(ip)) {
                synchronized (LOCK) {
                    runningKey = key;
                    runningSlots = np;
                    url = "http://" + ip + ":" + Worker.PORT;
                    slots = np;
                    keyPrint = Worker.fingerprint(key);
                    startedAt = System.currentTimeMillis();
                    serving = true;
                    healthFails = 0;
                    state = "serving " + np + (np == 1 ? " job" : " jobs") + " at " + url;
                }
                return "";
            }
            try { Thread.sleep(250); } catch (InterruptedException e) { /* keep waiting */ }
        }
        kill("the model did not come up within " + START_WAIT_MS / 1000 + " s");
        return state;
    }

    /** Stop the server now. True when one was running. */
    private boolean kill(String why) {
        Process p;
        synchronized (LOCK) {
            p = proc;
            proc = null;
            serving = false;
            url = "";
            slots = 0;
            keyPrint = "";
            runningKey = "";
            state = why;
        }
        if (p == null) return false;
        boolean was = p.isAlive();
        p.destroyForcibly();
        try { p.waitFor(2, TimeUnit.SECONDS); } catch (InterruptedException e) { /* gone soon */ }
        return was;
    }

    private static boolean healthy(String ip) {
        try {
            HttpURLConnection c = (HttpURLConnection) new URL("http://" + ip + ":" + Worker.PORT
                    + "/health").openConnection();
            c.setConnectTimeout(1000);
            c.setReadTimeout(2000);
            return c.getResponseCode() == 200;
        } catch (Exception e) {
            return false;
        }
    }

    /** Check in with Identity now (advertises or withdraws the worker; collects commands). */
    private void beat() {
        lastBeat = System.currentTimeMillis();
        try {
            new NodeLink(this).checkIn();
        } catch (RuntimeException e) { /* the next tick tries again */ }
    }

    // ------------------------------------------------------------ conditions

    private boolean charging() {
        Intent b = registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        return b != null && b.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0;
    }

    /** On Wi-Fi or Ethernet: any non-VPN network with that transport and internet. */
    private boolean wifi() {
        try {
            ConnectivityManager cm = getSystemService(ConnectivityManager.class);
            for (Network n : cm.getAllNetworks()) {
                NetworkCapabilities caps = cm.getNetworkCapabilities(n);
                if (caps == null || caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) continue;
                if ((caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
                        || caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET))
                        && caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)) {
                    return true;
                }
            }
        } catch (Exception e) { /* unknown = not on Wi-Fi */ }
        return false;
    }

    private Notification note(String text) {
        PendingIntent pi = PendingIntent.getActivity(this, NOTE_ID, new Intent(this, SettingsActivity.class),
                PendingIntent.FLAG_IMMUTABLE);
        Notification n = new Notification.Builder(this, CHANNEL)
                .setSmallIcon(android.R.drawable.ic_menu_share)
                .setContentTitle("Helping your fleet")
                .setContentText(text)
                .setOngoing(true)
                .setContentIntent(pi)
                .build();
        getSystemService(NotificationManager.class).notify(NOTE_ID, n);
        return n;
    }
}
