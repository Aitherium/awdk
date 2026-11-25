package com.aitherium.aither;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.widget.TextView;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * The fairy as a SYSTEM overlay: it floats over every app, drags anywhere, and a tap opens
 * Aither. Owner, 2026-11-17: "how do i make the fiary float like an overlay on the phone
 * screen outside of aither app".
 *
 * Android is the only one of the owner's platforms that can do this (SYSTEM_ALERT_WINDOW in
 * the system window layer); iOS forbids cross-app overlays, where a Live Activity is the
 * equivalent. It lives HERE, inside the shipped app, because this app already carries the
 * over-the-air update lane (Updater, Aitherium/awdk tags aither-android-v*) -- a separate
 * "fairy" APK would be a second app with no update path.
 *
 * Started by the person (ACTION_SHOW below, from a settings row or the assistant), never by
 * itself. FLAG_NOT_FOCUSABLE so it cannot take the keyboard from whatever the person is in.
 */
public class FairyOverlayService extends Service {
    public static final String ACTION_SHOW = "com.aitherium.aither.FAIRY_SHOW";
    public static final String ACTION_HIDE = "com.aitherium.aither.FAIRY_HIDE";
    private static final String CHANNEL = "fairy-overlay";
    private static final int NOTICE_ID = 41;

    private WindowManager wm;
    private View fairy;
    private TextView fairyText;

    /**
     * The fairy's live state, from the SAME source the web fairy reads: the owner's open
     * decision cards (GET /api/decisions?status=open) with this app's WebView session, plus
     * whether Aither is speaking. It is refreshed on a timer AND on every network reconnect —
     * the #18476 contract, because a reconnect can come back "connected" without replaying
     * the relay's #decisions channel. Android has no WebSocket client here (the app ships no
     * third-party deps and java.net.http is not on Android), so the overlay follows the web
     * fairy's own fallback path rather than a second, divergent push.
     */
    private volatile FairyOverlayState state = FairyOverlayState.empty();
    private Handler ui;
    private ScheduledExecutorService feed;
    private ConnectivityManager.NetworkCallback net;
    private boolean speakingOn;

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? null : intent.getAction();
        if (ACTION_HIDE.equals(action)) {
            stopSelf();
            return START_NOT_STICKY;
        }
        startForeground(NOTICE_ID, notification());
        if (fairy == null) {
            attach();
            startFeed();
        }
        return START_STICKY;
    }

    private Notification notification() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(
                    CHANNEL, "Floating fairy", NotificationManager.IMPORTANCE_LOW);
            ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).createNotificationChannel(ch);
        }
        Intent open = new Intent(this, MainActivity.class);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT
                | (Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0);
        return new Notification.Builder(this, CHANNEL)
                .setContentTitle("Aither is floating")
                .setContentText("Tap the fairy to open Aither. Long-press the notification to stop it.")
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentIntent(PendingIntent.getActivity(this, 0, open, flags))
                .build();
    }

    private void attach() {
        wm = (WindowManager) getSystemService(WINDOW_SERVICE);
        TextView v = new TextView(this);
        v.setTextSize(38);
        v.setShadowLayer(8f, 0f, 2f, Color.BLACK);
        v.setPadding(16, 16, 16, 16);
        fairyText = v;
        render();

        final WindowManager.LayoutParams lp = new WindowManager.LayoutParams(
                WindowManager.LayoutParams.WRAP_CONTENT, WindowManager.LayoutParams.WRAP_CONTENT,
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE, PixelFormat.TRANSLUCENT);
        lp.gravity = Gravity.TOP | Gravity.START;
        lp.x = 40;
        lp.y = 240;

        final int[] start = new int[2];
        final float[] touch = new float[2];
        v.setOnTouchListener((view, e) -> {
            switch (e.getActionMasked()) {
                case MotionEvent.ACTION_DOWN:
                    start[0] = lp.x;
                    start[1] = lp.y;
                    touch[0] = e.getRawX();
                    touch[1] = e.getRawY();
                    return true;
                case MotionEvent.ACTION_MOVE:
                    lp.x = start[0] + (int) (e.getRawX() - touch[0]);
                    lp.y = start[1] + (int) (e.getRawY() - touch[1]);
                    wm.updateViewLayout(view, lp);
                    return true;
                case MotionEvent.ACTION_UP:
                    float dx = Math.abs(e.getRawX() - touch[0]);
                    float dy = Math.abs(e.getRawY() - touch[1]);
                    if (dx < 12 && dy < 12) {
                        Intent open = new Intent(this, MainActivity.class);
                        open.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                        startActivity(open);
                    }
                    return true;
                default:
                    return false;
            }
        });
        fairy = v;
        wm.addView(fairy, lp);
    }

    // ── the state feed: same snapshot as the web fairy, reconciled on reconnect ──────────

    private void startFeed() {
        ui = new Handler(Looper.getMainLooper());
        feed = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "fairy-feed");
            t.setDaemon(true);
            return t;
        });
        // A timer, like the web fairy's fallback poll (there is no relay stream on Android).
        feed.scheduleWithFixedDelay(this::refresh, 0, 60, TimeUnit.SECONDS);
        // Return-to-network reconciles at once, whatever the timer says: a reconnect can
        // report "connected" without replaying #decisions (#18476's contract).
        try {
            ConnectivityManager cm = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
            if (cm != null) {
                net = new ConnectivityManager.NetworkCallback() {
                    @Override public void onAvailable(Network n) { feed.execute(FairyOverlayService.this::refresh); }
                    @Override public void onCapabilitiesChanged(Network n, NetworkCapabilities c) {
                        if (c.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)) {
                            feed.execute(FairyOverlayService.this::refresh);
                        }
                    }
                };
                cm.registerNetworkCallback(
                        new NetworkRequest.Builder()
                                .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET).build(), net);
            }
        } catch (Exception e) {
            android.util.Log.w("FairyOverlay", "no network watch: " + e);
        }
        // A fast, cheap ticker for the speaking pulse only (no network here).
        ui.postDelayed(this::speakTick, 500);
    }

    private void speakTick() {
        boolean on = AitherSay.speaking;
        if (on != state.speaking) {
            state = state.withSpeaking(on);
            render();
        }
        if (on) {
            speakingOn = !speakingOn;
            paint();
        }
        if (feed != null && !feed.isShutdown()) ui.postDelayed(this::speakTick, 500);
    }

    /** Read /api/decisions?status=open with this app's WebView session (the web fairy's source). */
    private void refresh() {
        try {
            String cookies = CookieManager.getInstance().getCookie(AppTabs.ORIGIN);
            boolean signedIn = cookies != null && cookies.contains(Session.COOKIE + "=");
            if (!signedIn) {
                state = FairyOverlayState.empty().withSpeaking(AitherSay.speaking);
                render();
                return;
            }
            HttpURLConnection c = (HttpURLConnection) new URL(
                    AppTabs.ORIGIN + "/api/decisions?status=open").openConnection();
            c.setConnectTimeout(8000);
            c.setReadTimeout(15000);
            c.setRequestProperty("Accept", "application/json");
            c.setRequestProperty("Cookie", cookies);
            if (c.getResponseCode() != 200) return; // offline/unauthorized: keep what we had
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            try (InputStream in = c.getInputStream()) {
                byte[] b = new byte[8192];
                int n;
                while ((n = in.read(b)) > 0) buf.write(b, 0, n);
            }
            state = FairyOverlayState.parse(buf.toString(StandardCharsets.UTF_8.name()), AitherSay.speaking);
            render();
        } catch (Exception e) {
            // offline: keep the last reading until the next poll or reconnect
        }
    }

    private void render() {
        if (ui == null || Looper.myLooper() == Looper.getMainLooper()) paint();
        else ui.post(this::paint);
    }

    private void paint() {
        TextView v = fairyText;
        if (v == null) return;
        FairyOverlayState s = state;
        v.setText(s.label());
        v.setContentDescription(s.description());
        // "Needs you" warms the fairy; speaking pulses it (the web fairy's flash).
        v.setTextColor(s.attention ? 0xFFFFC24D : Color.WHITE);
        v.setAlpha(s.speaking ? (speakingOn ? 1f : 0.5f) : 1f);
    }

    @Override
    public void onDestroy() {
        if (feed != null) feed.shutdownNow();
        if (net != null) {
            try {
                ((ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE)).unregisterNetworkCallback(net);
            } catch (Exception ignored) { /* already gone */ }
        }
        if (wm != null && fairy != null) {
            try {
                wm.removeView(fairy);
            } catch (Exception ignored) {
                // the window goes with the task when the system tears it down first
            }
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
