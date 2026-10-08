package com.aitherium.aither;

import android.Manifest;
import android.app.Activity;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * Household messages as real Android notifications. Inside the app the page has no web
 * Notification API, so the app does it: at each check-in it reads the signed-in learner's
 * own notes from their grown-up (GET /api/tutor/me/messages, the same call the Learn page
 * makes, with the same session cookie) and posts one notification per new unread note.
 * Tapping it opens Learn. A grown-up's account has no learner and gets a 404: nothing.
 *
 * The page asks about the permission through window.AitherApp (Bridge below), so the
 * "connect this device" page shows the app's real state instead of "not offered".
 */
final class FamilyNotices {
    static final String CHANNEL = "family";
    private static final int MAX_SEEN = 50;

    private FamilyNotices() {}

    static boolean allowed(Context c) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        return nm != null && nm.areNotificationsEnabled()
                && c.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == 0;
    }

    /** New unread notes since the last check -> notifications. Never throws. */
    static void poll(Context ctx) {
        if (!allowed(ctx)) return;
        try {
            String cookie = CookieManager.getInstance().getCookie(HeartbeatJob.API);
            if (cookie == null || cookie.isEmpty()) return;
            HttpURLConnection c = (HttpURLConnection) new URL(HeartbeatJob.API + "/api/tutor/me/messages?limit=10").openConnection();
            c.setConnectTimeout(20000);
            c.setReadTimeout(30000);
            c.setRequestProperty("Origin", "https://aitherium.com");
            c.setRequestProperty("Cookie", cookie);
            if (c.getResponseCode() != 200) return; // 404: not a learner; 401: signed out
            StringBuilder sb = new StringBuilder();
            try (InputStream in = c.getInputStream()) {
                byte[] b = new byte[4096];
                int n;
                while ((n = in.read(b)) > 0) sb.append(new String(b, 0, n, StandardCharsets.UTF_8));
            }
            JSONArray msgs = new JSONObject(sb.toString()).optJSONArray("messages");
            if (msgs == null) return;
            SharedPreferences p = ctx.getSharedPreferences("notices", Context.MODE_PRIVATE);
            List<String> seen = new ArrayList<>(Arrays.asList(p.getString("seen", "").split(",")));
            boolean first = !p.contains("seen"); // first run: remember, do not flood
            for (int i = 0; i < msgs.length(); i++) {
                JSONObject m = msgs.optJSONObject(i);
                if (m == null || !"grown_up".equals(m.optString("from")) || m.optBoolean("read", false)) continue;
                String id = m.optString("msg_id", "");
                if (id.isEmpty() || seen.contains(id)) continue;
                seen.add(id);
                if (!first) show(ctx, id, m.optString("text", ""));
            }
            while (seen.size() > MAX_SEEN) seen.remove(0);
            p.edit().putString("seen", String.join(",", seen)).apply();
        } catch (Exception e) { /* next check-in tries again */ }
    }

    private static void show(Context ctx, String id, String text) {
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        if (nm == null) return;
        nm.createNotificationChannel(new NotificationChannel(CHANNEL, "Family messages", NotificationManager.IMPORTANCE_DEFAULT));
        Intent open = new Intent(Intent.ACTION_VIEW, Uri.parse(Shortcuts.ORIGIN + "/learn")).setClass(ctx, MainActivity.class);
        PendingIntent pi = PendingIntent.getActivity(ctx, id.hashCode(), open,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        String body = text.length() > 200 ? text.substring(0, 200) + "…" : text;
        nm.notify("family", id.hashCode(), new android.app.Notification.Builder(ctx, CHANNEL)
                .setSmallIcon(android.R.drawable.ic_dialog_email)
                .setContentTitle("A note from your grown-up")
                .setContentText(body)
                .setStyle(new android.app.Notification.BigTextStyle().bigText(body))
                .setContentIntent(pi)
                .setAutoCancel(true)
                .build());
    }

    /** window.AitherApp: what the page may ask the app. Read-only state plus one request. */
    static final class Bridge {
        private final Activity act;

        Bridge(Activity a) { act = a; }

        /** "granted", or "denied" when the app may not post notifications. */
        @JavascriptInterface
        public String notifications() {
            return allowed(act) ? "granted" : "denied";
        }

        /** Android's own permission dialog; the system settings page if it will not ask again. */
        @JavascriptInterface
        public void requestNotifications() {
            act.runOnUiThread(() -> {
                if (act.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != 0
                        && act.shouldShowRequestPermissionRationale(Manifest.permission.POST_NOTIFICATIONS)) {
                    act.requestPermissions(new String[] {Manifest.permission.POST_NOTIFICATIONS}, 2);
                } else if (!allowed(act)) {
                    try {
                        act.startActivity(new Intent(android.provider.Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                                .putExtra(android.provider.Settings.EXTRA_APP_PACKAGE, act.getPackageName()));
                    } catch (RuntimeException e) {
                        act.requestPermissions(new String[] {Manifest.permission.POST_NOTIFICATIONS}, 2);
                    }
                }
            });
        }

        /** True while this phone is on duty for approvals (DutyService). */
        @JavascriptInterface
        public boolean approvalsOnDuty() {
            return new Config(act).approvalsOnDuty();
        }

        /** On duty: approval cards reach this phone in seconds (a quiet ongoing notification
         *  shows it). Not offered on a child's phone. Returns the resulting state. */
        @JavascriptInterface
        public boolean setApprovalsOnDuty(boolean on) {
            Config cfg = new Config(act);
            if (on && "child".equals(cfg.profileKind())) return false;
            cfg.set("approvals_duty", on);
            act.runOnUiThread(() -> DutyService.sync(act));
            return on;
        }

        /** Android's share sheet for a family invite (the WebView has no navigator.share).
         *  Only an aitherium.com / app.aitherium.com https link is shared, as "<text> <url>"
         *  built here (InviteShare); false when the link is refused. */
        @JavascriptInterface
        public boolean share(String title, String text, String url) {
            String msg = InviteShare.message(text, url);
            if (msg == null) return false;
            String subject = InviteShare.clip(title, InviteShare.MAX_TITLE);
            act.runOnUiThread(() -> {
                Intent send = new Intent(Intent.ACTION_SEND).setType("text/plain")
                        .putExtra(Intent.EXTRA_TEXT, msg);
                if (!subject.isEmpty()) send.putExtra(Intent.EXTRA_SUBJECT, subject);
                try {
                    act.startActivity(Intent.createChooser(send, subject.isEmpty() ? "Share invite" : subject));
                } catch (RuntimeException e) { /* no app can share text */ }
            });
            return true;
        }
    }
}
