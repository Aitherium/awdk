package com.aitherium.aither;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;

/**
 * "On duty for approvals": while the guardian leaves it on, this foreground service holds
 * ONE outbound long-poll on the household inbox (Notices.poll, 25 s per round), so an
 * approval card reaches this phone within seconds with Aither closed, without Google Play
 * Services and without any inbound path to the phone. Off by default; a guardian turns it
 * on in Aither's settings (or the page asks through window.AitherApp.setApprovalsOnDuty).
 *
 * Errors back off from 5 s to 2 min; a signed-out app waits for the next sign-in instead of
 * hammering. Android's battery optimisation can still pause the network in deep sleep: the
 * settings row "Allow running with the screen off" is what keeps duty working then.
 */
public class DutyService extends Service {
    static final int NOTE_ID = 7;
    static volatile String state = "off";
    private volatile boolean running;
    private Thread loop;

    static void sync(Context c) {
        Intent svc = new Intent(c, DutyService.class);
        if (new Config(c).approvalsOnDuty()) {
            try { c.startForegroundService(svc); } catch (RuntimeException e) { state = "starts when Aither next opens"; }
        } else {
            c.stopService(svc);
        }
    }

    @Override
    public void onCreate() {
        super.onCreate();
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel("duty", "On duty for approvals", NotificationManager.IMPORTANCE_MIN));
        PendingIntent open = PendingIntent.getActivity(this, NOTE_ID,
                new Intent(this, SettingsActivity.class), PendingIntent.FLAG_IMMUTABLE);
        Notification n = new Notification.Builder(this, "duty")
                .setSmallIcon(android.R.drawable.ic_lock_idle_lock)
                .setContentTitle("On duty for approvals")
                .setContentText("Your home's approval requests reach this phone right away")
                .setContentIntent(open)
                .setOngoing(true)
                .build();
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTE_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(NOTE_ID, n);
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (!new Config(this).approvalsOnDuty()) {
            stopSelf();
            return START_NOT_STICKY;
        }
        if (loop == null || !loop.isAlive()) {
            running = true;
            loop = new Thread(this::run, "aither-duty");
            loop.start();
        }
        return START_STICKY;
    }

    private void run() {
        long backoff = 5_000L;
        while (running) {
            int code = Notices.poll(this, 25);
            if (code == 200) {
                state = "on duty · " + Notices.last;
                backoff = 5_000L;
                // a server that answers at once (no long-poll) must not become a busy loop
                try { Thread.sleep(1_000L); } catch (InterruptedException e) { return; }
                continue;
            }
            state = code == 401 ? "waiting for you to sign in"
                    : code == 403 ? "notifications are off for Aither" : "reconnecting (" + code + ")";
            long wait = code == 401 || code == 403 ? 120_000L : backoff;
            backoff = Math.min(backoff * 2, 120_000L);
            try { Thread.sleep(wait); } catch (InterruptedException e) { return; }
        }
    }

    @Override
    public void onDestroy() {
        running = false;
        if (loop != null) loop.interrupt();
        state = "off";
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }
}
