package com.aitherium.aither;

import android.app.ActivityManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.Build;
import android.os.IBinder;

import org.json.JSONObject;

import java.io.File;
import java.io.FileDescriptor;
import java.io.InputStream;
import java.io.PrintWriter;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * Lends this phone's RAM to the pooled model ({@code mesh-lend-on}): ggml's RPC server, run
 * from nativeLibraryDir as a child of this foreground service, bound to the phone's Tailscale
 * address. A foreground service because Android kills a background app's processes; its own
 * service (not HolderService) because KV-holder lending lives and dies with the workspace
 * relay, and this needs none. The rules it runs by are {@link MeshLend}'s.
 *
 * Off after a reboot or when Android stops the service: the owner sends mesh-lend-on again.
 * LOCK guards the fields and is held only to read or publish them, never across a probe or a
 * wait (onStartCommand/onDestroy take it on the main thread). Every start bumps {@code gen};
 * a launch, watcher or waiting command from an older run never touches a newer one.
 */
public class MeshLendService extends Service {
    static final int NOTE_ID = 11;
    static final String CHANNEL = "mesh-lend";
    static final String EXTRA_GB = "gb";
    static final String EXTRA_GEN = "gen";
    static final long START_WAIT_MS = 45_000;
    static final long SETTLE_MS = 2_000;

    // what diagnostics and the command result read; written only under LOCK
    static volatile String state = "off";
    static volatile boolean lending;
    static volatile String ip = "";
    static volatile long lendMb;
    static volatile long pid = -1;
    static volatile String build = "";
    static volatile long startedAt;

    private static final Object LOCK = new Object();
    private static Process proc;
    private static long gen;
    private static CountDownLatch done = new CountDownLatch(0);

    /**
     * Start lending {@code gb} GB and wait for the verdict. Called from the command thread
     * (the heartbeat's), never the main thread. The answer is filled into {@code out}.
     */
    static boolean start(Context ctx, int gb, JSONObject out) throws Exception {
        CountDownLatch latch = new CountDownLatch(1);
        long mine;
        synchronized (LOCK) {
            if (proc != null && proc.isAlive()) {
                report(out.put("state", "already lending; send mesh-lend-off first to change it"));
                return true;
            }
            mine = ++gen;
            done.countDown(); // a caller still waiting on an older run is released, not reused
            done = latch;
            state = "starting";
        }
        try {
            ctx.startForegroundService(new Intent(ctx, MeshLendService.class)
                    .putExtra(EXTRA_GB, gb).putExtra(EXTRA_GEN, mine));
        } catch (RuntimeException e) {
            // No foreground service = Android would kill the server: do not start it at all.
            synchronized (LOCK) {
                if (gen == mine) state = "off";
            }
            out.put("state", "Android refused to start the lending service ("
                    + e.getClass().getSimpleName() + "); open Aither once and send it again");
            return false;
        }
        if (!latch.await(START_WAIT_MS, TimeUnit.MILLISECONDS)) {
            // never report "not lending" while a server may still come up: retire the run
            synchronized (LOCK) {
                if (gen == mine) gen++;
            }
            kill("off: did not come up within " + START_WAIT_MS / 1000 + " s");
            out.put("state", "did not come up within " + START_WAIT_MS / 1000 + " s; stopped");
            return false;
        }
        synchronized (LOCK) {
            report(out.put("state", state));
            return lending && gen == mine;
        }
    }

    /** Stop lending now: the server is killed here, not when Android gets round to the service. */
    static void stop(Context ctx, JSONObject out) throws Exception {
        synchronized (LOCK) {
            gen++; // a launch still in flight will not adopt its server
            done.countDown();
        }
        boolean was = kill("off");
        try {
            ctx.stopService(new Intent(ctx, MeshLendService.class));
        } catch (RuntimeException e) { /* not running */ }
        out.put("state", was ? "lending stopped" : "was not lending");
    }

    /** The state, as collect-diagnostics and every mesh-lend result report it. */
    static JSONObject report(JSONObject o) throws Exception {
        o.put("lending", lending).put("release", MeshLend.RELEASE);
        if (lending) {
            o.put("bound", ip + ":" + MeshLend.PORT).put("ip", ip).put("port", MeshLend.PORT)
                    .put("lend_mb", lendMb).put("pid", pid).put("build", build)
                    .put("started_at", startedAt / 1000)
                    // the RPC server takes no memory cap and reports total memory as free:
                    // the pool must place at most lend_mb here; the phone does not register
                    .put("announced", false);
        }
        return o;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        getSystemService(NotificationManager.class).createNotificationChannel(new NotificationChannel(
                CHANNEL, "Lending memory to your AI pool", NotificationManager.IMPORTANCE_MIN));
        Notification n = note("Starting");
        if (Build.VERSION.SDK_INT >= 34) startForeground(NOTE_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        else startForeground(NOTE_ID, n);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        int gb = intent == null ? 0 : intent.getIntExtra(EXTRA_GB, 0);
        long mine = intent == null ? -1 : intent.getLongExtra(EXTRA_GEN, -1);
        if (gb <= 0) { // a restart Android made on its own, or no request: nothing to run
            boolean idle;
            synchronized (LOCK) {
                idle = proc == null || !proc.isAlive();
            }
            if (idle) stopSelf();
            return START_NOT_STICKY;
        }
        new Thread(() -> launch(gb, mine), "aither-mesh-lend").start();
        return START_NOT_STICKY;
    }

    @Override
    public void onDestroy() {
        synchronized (LOCK) {
            gen++;
            done.countDown();
        }
        kill("off");
        super.onDestroy();
    }

    /** {@code adb shell dumpsys activity service com.aitherium.aither/.MeshLendService}. */
    @Override
    protected void dump(FileDescriptor fd, PrintWriter out, String[] args) {
        out.println("mesh lend: " + state + (lending ? " " + ip + ":" + MeshLend.PORT
                + " " + lendMb + " MB pid " + pid + " " + build : ""));
    }

    @Override
    public IBinder onBind(Intent i) {
        return null;
    }

    // ------------------------------------------------------------ the server

    private void launch(int gb, long mine) {
        String why = launchRun(gb, mine);
        boolean up;
        synchronized (LOCK) {
            if (gen != mine) return; // a newer start, a stop or a timeout owns the state now
            if (!why.isEmpty()) finish(why);
            up = lending;
            done.countDown();
        }
        if (up) note(state);
        else stopSelf();
    }

    /** "" when the server is up and published; otherwise why not. */
    private String launchRun(int gb, long mine) {
        if (new Config(this).childDevice()) return "not on a child's phone";
        String host = MeshLend.pickAddress(addresses(this));
        if (host.isEmpty()) {
            return "not on the mesh: install Tailscale, sign in to the owner's tailnet with Use Tailscale DNS on, then send it again";
        }
        ActivityManager.MemoryInfo mi = new ActivityManager.MemoryInfo();
        getSystemService(ActivityManager.class).getMemoryInfo(mi);
        String refused = MeshLend.lendRefusal(gb, mi.availMem >> 20);
        if (!refused.isEmpty()) return refused;
        String dir = getApplicationInfo().nativeLibraryDir;
        String last = "";
        for (String[] b : MeshLend.BUILDS) {
            File exe = new File(dir, b[0]);
            String bad = MeshLend.verify(exe, b);
            if (!bad.isEmpty()) {
                last = bad;
                continue;
            }
            if (!MeshLend.usage(probe(exe))) {
                last = b[1] + " did not run on this CPU";
                continue;
            }
            List<String> argv = MeshLend.argv(exe.getAbsolutePath(), host,
                    MeshLend.threads(Runtime.getRuntime().availableProcessors()));
            Process p;
            synchronized (LOCK) {
                if (gen != mine) return "superseded";
                try {
                    p = new ProcessBuilder(argv).redirectErrorStream(true)
                            .redirectOutput(new File(getFilesDir(), "mesh-lend.log")).start();
                } catch (Exception e) {
                    last = b[1] + " did not start (" + e.getClass().getSimpleName() + ")";
                    continue;
                }
                proc = p; // owned from here: a stop or onDestroy kills it
            }
            try { Thread.sleep(SETTLE_MS); } catch (InterruptedException e) { /* check now */ }
            synchronized (LOCK) {
                if (proc != p) return "superseded";
                if (!p.isAlive()) {
                    proc = null;
                    last = b[1] + " exited " + p.exitValue() + " at start (see mesh-lend.log)";
                    continue;
                }
                ip = host;
                lendMb = gb * 1024L;
                pid = pidOf(p, exe.getAbsolutePath());
                build = b[1];
                startedAt = System.currentTimeMillis();
                lending = true;
                state = "lending " + gb + " GB at " + host + ":" + MeshLend.PORT;
            }
            watch(p);
            return "";
        }
        return "could not lend: " + (last.isEmpty() ? "no server build" : last);
    }

    /** When the server dies on its own, say so and let the service go. */
    private void watch(Process p) {
        Thread t = new Thread(() -> {
            int code;
            try { code = p.waitFor(); } catch (InterruptedException e) { return; }
            synchronized (LOCK) {
                if (proc != p) return; // stopped on purpose, or a newer run
                proc = null;
                finish("stopped: the server exited " + code + " (see mesh-lend.log)");
            }
            stopSelf();
        }, "aither-mesh-lend-watch");
        t.setDaemon(true);
        t.start();
    }

    /** {@code exe -h}: its usage text, or "" when it would not run. Never longer than 20 s. */
    private static String probe(File exe) {
        try {
            Process p = new ProcessBuilder(exe.getAbsolutePath(), "-h").redirectErrorStream(true).start();
            // the usage text is ~1 KB, well under a pipe buffer: wait first, then read
            if (!p.waitFor(20, TimeUnit.SECONDS)) {
                p.destroyForcibly();
                return "";
            }
            byte[] buf = new byte[8192];
            int n = 0;
            try (InputStream in = p.getInputStream()) {
                int r;
                while (n < buf.length && (r = in.read(buf, n, buf.length - n)) > 0) n += r;
            }
            return new String(buf, 0, n, StandardCharsets.UTF_8);
        } catch (Exception e) {
            return "";
        }
    }

    /** Stop the server: SIGKILL, waited for up to 2 s so the port is free for a new start. */
    private static boolean kill(String why) {
        Process p;
        synchronized (LOCK) {
            p = proc;
            proc = null;
            if (p != null || lending) finish(why);
        }
        if (p == null) return false;
        boolean was = p.isAlive();
        p.destroyForcibly();
        try { p.waitFor(2, TimeUnit.SECONDS); } catch (InterruptedException e) { /* gone soon */ }
        return was;
    }

    /** Not lending: clear the fields. Holds LOCK. */
    private static void finish(String why) {
        lending = false;
        ip = "";
        lendMb = 0;
        pid = -1;
        build = "";
        state = why;
    }

    /** The server's pid: Process.pid() where the runtime has it, else from /proc (own uid). */
    private static long pidOf(Process p, String exe) {
        try {
            return ((Number) Process.class.getMethod("pid").invoke(p)).longValue();
        } catch (Exception e) { /* older runtime */ }
        File[] all = new File("/proc").listFiles();
        if (all == null) return -1;
        for (File d : all) {
            if (!d.getName().chars().allMatch(c -> c >= '0' && c <= '9')) continue;
            try {
                byte[] c = Files.readAllBytes(new File(d, "cmdline").toPath());
                int end = 0;
                while (end < c.length && c[end] != 0) end++;
                if (exe.equals(new String(c, 0, end, StandardCharsets.UTF_8))) {
                    return Long.parseLong(d.getName());
                }
            } catch (Exception e) { /* gone or not ours */ }
        }
        return -1;
    }

    /**
     * {interface, IPv4} of every VPN network that is Tailscale's (MeshLend.tailscale: its DNS
     * is 100.100.100.100 or a *.ts.net domain). Another VPN app handing out a 100.64/10
     * address, or a carrier's CGNAT address, is not the mesh.
     */
    static List<String[]> addresses(Context ctx) {
        List<String[]> out = new ArrayList<>();
        try {
            ConnectivityManager cm = ctx.getSystemService(ConnectivityManager.class);
            for (Network n : cm.getAllNetworks()) {
                NetworkCapabilities caps = cm.getNetworkCapabilities(n);
                if (caps == null || !caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) continue;
                LinkProperties lp = cm.getLinkProperties(n);
                if (lp == null) continue;
                List<String> dns = new ArrayList<>();
                for (InetAddress d : lp.getDnsServers()) dns.add(d.getHostAddress());
                if (!MeshLend.tailscale(dns, lp.getDomains())) continue;
                for (LinkAddress la : lp.getLinkAddresses()) {
                    InetAddress a = la.getAddress();
                    if (a instanceof Inet4Address) {
                        out.add(new String[] {String.valueOf(lp.getInterfaceName()), a.getHostAddress()});
                    }
                }
            }
        } catch (Exception e) { /* none: not on the mesh */ }
        return out;
    }

    private Notification note(String text) {
        PendingIntent pi = PendingIntent.getActivity(this, NOTE_ID, new Intent(this, SettingsActivity.class),
                PendingIntent.FLAG_IMMUTABLE);
        Notification n = new Notification.Builder(this, CHANNEL)
                .setSmallIcon(android.R.drawable.ic_menu_share)
                .setContentTitle("Lending memory to your AI pool")
                .setContentText(text)
                .setOngoing(true)
                .setContentIntent(pi)
                .build();
        getSystemService(NotificationManager.class).notify(NOTE_ID, n);
        return n;
    }
}
