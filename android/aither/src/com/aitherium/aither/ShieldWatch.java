package com.aitherium.aither;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.job.JobInfo;
import android.app.job.JobParameters;
import android.app.job.JobScheduler;
import android.app.job.JobService;
import android.content.BroadcastReceiver;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.net.VpnService;
import android.webkit.CookieManager;

import org.json.JSONObject;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * Family Shield's watch: the guardian knows when this phone's filter is off, and the child
 * is asked to turn it back on. Without device-owner mode the child can switch the VPN off in
 * Settings or let another VPN app take it over; nothing here can stop that, so it is seen and
 * said instead.
 *
 * Three ways in, one check (ShieldState.decide): ShieldVpnService.onRevoke (the moment it
 * happens), a ConnectivityManager callback on VPN networks (another VPN coming up, ours going
 * away; a PendingIntent, so it fires with the app not running), and its own 15-minute job,
 * separate from the household check-in. The check reports "off" (and later "on") to the
 * household (POST /api/tutor/me/device/shield, the device token in the body, like the
 * check-in), restarts the filter itself while Android's consent is held, and otherwise (or
 * when that restart has not worked by the end of the grace) posts a notification whose one
 * tap opens Aither, which starts the filter or shows Android's VPN prompt again.
 *
 * What it cannot see: force-stop, cleared data, uninstall or sign-out end this watch too.
 * The household reads that as silence (Genesis: shield_unknown once check-ins stop).
 */
public class ShieldWatch extends JobService {
    static final int JOB_ID = 4202;
    /** One look at the end of the grace for a filter that did not come back by itself. */
    static final int RECHECK_ID = 4203;
    static final int NUDGE_ID = 9;
    static final String CHANNEL = "family-shield-off";
    private static final Object LOCK = new Object();

    /** Arm the job and the network callback; a no-op when both are already armed. */
    static void arm(Context c) {
        JobScheduler js = c.getSystemService(JobScheduler.class);
        // re-scheduling a periodic job restarts its clock: only when it is not there yet
        if (js != null && js.getPendingJob(JOB_ID) == null) {
            js.schedule(new JobInfo.Builder(JOB_ID, new ComponentName(c, ShieldWatch.class))
                    .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                    .setPeriodic(15 * 60 * 1000L, 5 * 60 * 1000L)
                    .setPersisted(true)
                    .build());
        }
        ConnectivityManager cm = c.getSystemService(ConnectivityManager.class);
        if (cm == null) return;
        try {
            // the same PendingIntent replaces the earlier registration, never adds one
            cm.registerNetworkCallback(new NetworkRequest.Builder()
                    .addTransportType(NetworkCapabilities.TRANSPORT_VPN)
                    .removeCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN)
                    .build(), changed(c));
        } catch (RuntimeException e) {
            ShieldVpnService.last = "watch: no network callback (" + e.getClass().getSimpleName() + ")";
        }
    }

    /** The household no longer asks for a filter: stop watching, forget what was said. */
    static void disarm(Context c) {
        JobScheduler js = c.getSystemService(JobScheduler.class);
        if (js == null || js.getPendingJob(JOB_ID) == null) {
            // never armed (every phone without a filter, on every check-in): nothing to undo
            if (new Config(c).shieldReported().isEmpty()) return;
        } else {
            js.cancel(JOB_ID);
        }
        if (js != null) js.cancel(RECHECK_ID);
        ConnectivityManager cm = c.getSystemService(ConnectivityManager.class);
        if (cm != null) {
            try {
                cm.unregisterNetworkCallback(changed(c));
            } catch (RuntimeException e) {
                // was never registered
            }
        }
        Config cfg = new Config(c);
        if (!cfg.shieldReported().isEmpty()) cfg.set("shield_reported", "");
        if (cfg.shieldOffSince() != 0) cfg.set("shield_off_since", 0L);
        if (cfg.shieldRevoked()) cfg.set("shield_revoked", false);
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm != null) nm.cancel(NUDGE_ID);
    }

    private static PendingIntent changed(Context c) {
        // the system fills in the network extras, so this one has to be mutable
        return PendingIntent.getBroadcast(c, JOB_ID, new Intent(c, Changed.class),
                PendingIntent.FLAG_MUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    /** A VPN network came or went. */
    public static class Changed extends BroadcastReceiver {
        @Override
        public void onReceive(Context c, Intent i) {
            PendingResult done = goAsync();
            Context app = c.getApplicationContext();
            new Thread(() -> {
                try {
                    check(app);
                } finally {
                    done.finish();
                }
            }, "aither-shield-watch").start();
        }
    }

    /** check() off the calling thread (onRevoke and the service run on the main thread). */
    static void checkSoon(Context c) {
        checkSoon(c, 0);
    }

    /** checkSoon after a pause (onRevoke: let our own tunnel go before looking for others). */
    static void checkSoon(Context c, long delayMs) {
        Context app = c.getApplicationContext();
        new Thread(() -> {
            if (delayMs > 0) {
                try {
                    Thread.sleep(delayMs);
                } catch (InterruptedException e) {
                    return;
                }
            }
            check(app);
        }, "aither-shield-watch").start();
    }

    /** One look after ms, so the grace ends on time and not at the next 15-minute run. */
    private static void recheck(Context c, long ms) {
        JobScheduler js = c.getSystemService(JobScheduler.class);
        if (js == null || js.getPendingJob(RECHECK_ID) != null) return;
        js.schedule(new JobInfo.Builder(RECHECK_ID, new ComponentName(c, ShieldWatch.class))
                .setMinimumLatency(ms)
                .setOverrideDeadline(ms + 60_000L)
                .build());
    }

    @Override
    public boolean onStartJob(JobParameters params) {
        new Thread(() -> jobFinished(params, !check(this)), "aither-shield-watch").start();
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters params) {
        return true;
    }

    /** Look, decide, act. True when done (nothing to retry before the next run). */
    static boolean check(Context c) {
        synchronized (LOCK) {
            Config cfg = new Config(c);
            boolean running = ShieldVpnService.isRunning();
            boolean consent = VpnService.prepare(c) == null;
            long now = System.currentTimeMillis();
            long offSince = cfg.shieldOffSince();
            boolean wanted = ShieldVpnService.wanted(c);
            if (wanted && !running && offSince == 0) {
                offSince = now;
                cfg.set("shield_off_since", now);
            } else if (running && offSince != 0) {
                cfg.set("shield_off_since", 0L);
            }
            ShieldState.Verdict v = ShieldState.decide(wanted, running, consent,
                    !running && otherVpn(c), cfg.shieldRevoked(), cfg.shieldReported(), offSince, now);
            if (v.state.isEmpty()) return true;
            if (v.restart) ShieldVpnService.sync(c);
            if (v.recheckIn > 0) recheck(c, v.recheckIn);
            nudge(c, v.nudge);
            if (!v.report) return true;
            boolean sent = report(c, v.state, v.reason, offSince);
            if (sent) cfg.set("shield_reported", v.said());
            return sent;
        }
    }

    /** Any VPN network up on this phone (asked while ours is down, so not ours). */
    private static boolean otherVpn(Context c) {
        ConnectivityManager cm = c.getSystemService(ConnectivityManager.class);
        if (cm == null) return false;
        try {
            for (Network n : cm.getAllNetworks()) {
                NetworkCapabilities nc = cm.getNetworkCapabilities(n);
                if (nc != null && nc.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) return true;
            }
        } catch (RuntimeException e) {
            // no answer is not an answer: the reason then reads no_permission
        }
        return false;
    }

    /** POST the state to the household. True when the household has it. */
    private static boolean report(Context c, String state, String reason, long offSince) {
        String token = ShieldVpnService.token(c);
        if (token.isEmpty()) return true; // not in a household: nobody to tell
        try {
            HttpURLConnection h = (HttpURLConnection) new URL(HeartbeatJob.API
                    + "/api/tutor/me/device/shield").openConnection();
            h.setRequestMethod("POST");
            h.setConnectTimeout(10000);
            h.setReadTimeout(15000);
            h.setDoOutput(true);
            h.setRequestProperty("Content-Type", "application/json");
            h.setRequestProperty("Origin", "https://aitherium.com");
            h.setRequestProperty("User-Agent", "Mozilla/5.0 (Linux; Android) Aither/" + Config.VERSION);
            String cookie = CookieManager.getInstance().getCookie(HeartbeatJob.API);
            if (cookie != null) h.setRequestProperty("Cookie", cookie);
            JSONObject body = new JSONObject().put("token", token).put("state", state)
                    .put("reason", ShieldState.OFF.equals(state) ? reason : "");
            // when it went down, by this phone's clock: the report itself may be late
            if (ShieldState.OFF.equals(state) && offSince > 0) body.put("since", offSince / 1000.0);
            try (OutputStream o = h.getOutputStream()) {
                o.write(body.toString().getBytes(StandardCharsets.UTF_8));
            }
            int code = h.getResponseCode();
            if (code == 200) {
                ShieldVpnService.last = ShieldState.OFF.equals(state)
                        ? "off (" + reason + "): your family was told" : "on";
                return true;
            }
            ShieldVpnService.last = "off (" + reason + "): telling your family failed (" + code + ")";
            return code == 410; // removed from the household: the check-in forgets the device
        } catch (Exception e) {
            ShieldVpnService.last = "off (" + reason + "): telling your family failed ("
                    + e.getClass().getSimpleName() + ")";
            return false;
        }
    }

    /** The child's notification: one tap opens Aither, which shows Android's VPN prompt. */
    private static void nudge(Context c, boolean show) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm == null) return;
        if (!show) {
            nm.cancel(NUDGE_ID);
            return;
        }
        nm.createNotificationChannel(new NotificationChannel(CHANNEL, "Family Shield is off",
                NotificationManager.IMPORTANCE_DEFAULT));
        // MainActivity.onResume asks for consent (ShieldVpnService.askConsent)
        PendingIntent pi = PendingIntent.getActivity(c, NUDGE_ID,
                new Intent(c, MainActivity.class).setFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        nm.notify(NUDGE_ID, new Notification.Builder(c, CHANNEL)
                .setSmallIcon(android.R.drawable.ic_lock_lock)
                .setContentTitle("Family Shield is off")
                .setContentText("Tap to turn it back on. Your family can see when it is off.")
                .setContentIntent(pi)
                .setOnlyAlertOnce(true)
                .setAutoCancel(true)
                .build());
    }
}
