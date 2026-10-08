package com.aitherium.aither;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Build;
import android.webkit.CookieManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * Household notices on this phone, without Google Play Services: the app reads its own inbox
 * (POST /api/push/inbox, lib/hearth/notices.py) with the page's session cookie. While the
 * phone is "on duty" (DutyService) that read is a long-poll held open by the foreground
 * service, so a Hearth approval card lands within seconds; otherwise the 15-minute check-in
 * reads it once.
 *
 * An approval card is posted on the high-importance "approvals" channel (quiet hours never
 * hold one back) with Approve / Deny. Each button carries only the notice id and the digest
 * of the exact action (ApprovalCard); on Android 12+ the phone must be unlocked before the
 * button runs (setAuthenticationRequired), so a found phone cannot approve. The answer goes
 * through NoticeActionReceiver -> decide(), and the card is replaced with
 * "Approved by you · 1 of 2" (or why it was refused).
 *
 * The inbox also carries CHANGES to a card (a vote here, on another guardian's card or in
 * the Hearth app; approved, denied, ran, expired): the same notification id is re-posted, so
 * the card is replaced in place ("Approved · 2 of 2 · ran 6:41 pm", "Denied by Sam",
 * "Expired") without ringing again.
 *
 * Family notes are not shown from here: FamilyNotices already posts them with their words.
 */
final class Notices {
    static final String EXTRA_ID = "notice_id";
    static final String EXTRA_DIGEST = "digest";
    static final String EXTRA_ALLOW = "allow";
    static final String EXTRA_TITLE = "title";
    static final String ACTION_DECIDE = "com.aitherium.aither.DECIDE";
    private static final int MAX_SEEN = 60;
    static volatile String last = "never";

    private Notices() {}

    /** Read the inbox once (waiting up to waitS seconds). HTTP status, 0 when unreachable. */
    static int poll(Context ctx, int waitS) {
        if (!FamilyNotices.allowed(ctx)) {
            last = "notifications are off for Aither";
            return 403;
        }
        SharedPreferences p = ctx.getSharedPreferences("push", Context.MODE_PRIVATE);
        String cursor = p.getString("cursor", "0");
        try {
            String req = "{\"since\":" + Double.parseDouble(cursor) + ",\"wait_s\":" + Math.max(0, Math.min(waitS, 25)) + "}";
            Object[] got = post(ctx, "/api/push/inbox", req, (waitS + 15) * 1000);
            int code = (Integer) got[0];
            if (code != 200 || got[1] == null) {
                last = code == 401 ? "signed out" : "inbox answered " + code;
                return code;
            }
            JSONObject j = (JSONObject) got[1];
            JSONArray list = j.optJSONArray("notices");
            List<String> seen = new ArrayList<>(Arrays.asList(p.getString("seen", "").split(",")));
            List<String> open = new ArrayList<>(Arrays.asList(p.getString("open", "").split(",")));
            open.remove("");
            for (int i = 0; list != null && i < list.length(); i++) {
                ApprovalCard card = parse(list.optJSONObject(i));
                if (card == null || "family_note".equals(card.kind)) continue;
                // what waits for this account's OK, for the read-only count (openApprovals)
                open.remove(card.noticeId);
                if (card.answerable()) open.add(card.noticeId);
                // a change (a vote anywhere, a result, expiry) re-posts under the same id,
                // which REPLACES the card on this phone
                String mark = card.noticeId + ":" + card.state + ":" + card.have + ":" + card.mine;
                if (seen.contains(mark)) continue;
                seen.add(mark);
                show(ctx, card, null);
            }
            while (seen.size() > MAX_SEEN) seen.remove(0);
            while (open.size() > MAX_SEEN) open.remove(0);
            p.edit().putString("cursor", String.valueOf(j.optDouble("cursor", Double.parseDouble(cursor))))
                    .putString("seen", String.join(",", seen)).putString("open", String.join(",", open))
                    .putLong("checked_at", System.currentTimeMillis()).apply();
            last = "checked " + new java.util.Date();
            return 200;
        } catch (Exception e) {
            last = "inbox failed: " + e.getClass().getSimpleName();
            return 0;
        }
    }

    /** {open approvals for this account, minutes since the inbox was read (-1: never)}, from
     *  the last read; no network. */
    static long[] openApprovals(Context ctx) {
        SharedPreferences p = ctx.getSharedPreferences("push", Context.MODE_PRIVATE);
        long at = p.getLong("checked_at", 0);
        String open = p.getString("open", "");
        int n = open.isEmpty() ? 0 : open.split(",").length;
        return new long[] {n, at == 0 ? -1 : Math.max(0, (System.currentTimeMillis() - at) / 60_000L)};
    }

    static ApprovalCard parse(JSONObject n) {
        if (n == null || !ApprovalCard.validId(n.optString("notice_id"))) return null;
        JSONObject a = n.optJSONObject("approval");
        return new ApprovalCard(n.optString("notice_id"), n.optString("kind"), n.optString("title"),
                n.optString("body"), n.optBoolean("urgent"), n.optBoolean("quiet"),
                a == null ? "" : a.optString("digest"), a == null ? "" : a.optString("state"),
                a == null ? 0 : a.optInt("have"), a == null ? 0 : a.optInt("need", 1),
                a == null ? "" : a.optString("mine"), a == null ? "" : a.optString("denied_by"),
                a == null ? 0 : a.optDouble("ran_at", 0));
    }

    private static void channels(NotificationManager nm) {
        NotificationChannel ap = new NotificationChannel("approvals", "Approvals", NotificationManager.IMPORTANCE_HIGH);
        ap.setDescription("Something in your home is waiting for your OK");
        ap.setLockscreenVisibility(Notification.VISIBILITY_PRIVATE);
        nm.createNotificationChannel(ap);
        nm.createNotificationChannel(new NotificationChannel("household", "Household", NotificationManager.IMPORTANCE_DEFAULT));
        NotificationChannel q = new NotificationChannel("household_quiet", "Household (quiet hours)", NotificationManager.IMPORTANCE_LOW);
        q.setSound(null, null);
        nm.createNotificationChannel(q);
    }

    /** Post or replace one card. {@code line} overrides the status line (after an answer). */
    static void show(Context ctx, ApprovalCard card, String line) {
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        if (nm == null) return;
        channels(nm);
        Intent open = new Intent(Intent.ACTION_VIEW,
                Uri.parse(Shortcuts.ORIGIN + "/hearth/?notice=" + card.noticeId)).setClass(ctx, MainActivity.class);
        PendingIntent pi = PendingIntent.getActivity(ctx, card.notifyId(), open,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        String status = line != null ? line : card.isApproval() ? card.statusLine() : "";
        String text = status.isEmpty() ? card.body : card.body.isEmpty() ? status : card.body + "\n" + status;
        Notification.Builder b = new Notification.Builder(ctx, card.channel())
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentTitle(card.title.isEmpty() ? "Aither" : card.title)
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setContentIntent(pi)
                .setOnlyAlertOnce(line != null || card.settled() || !card.mine.isEmpty())
                .setAutoCancel(line != null || !card.answerable());
        if (card.isApproval()) {
            b.setCategory(Notification.CATEGORY_REMINDER).setVisibility(Notification.VISIBILITY_PRIVATE)
                    .setPublicVersion(new Notification.Builder(ctx, card.channel())
                            .setSmallIcon(android.R.drawable.ic_dialog_info)
                            .setContentTitle("Aither needs your OK")
                            .setContentText("Unlock to see what it is").build());
        }
        if (line == null && card.answerable()) {
            b.addAction(button(ctx, card, true)).addAction(button(ctx, card, false));
        }
        // A paired watch shows the card with its buttons (bridged; never setLocalOnly): one
        // dismissal id per notice, so answering or swiping on either one clears both.
        b.setLocalOnly(false).extend(new Notification.WearableExtender()
                .setDismissalId("notice-" + card.noticeId).setBridgeTag("notices"));
        nm.notify("notice", card.notifyId(), b.build());
    }

    private static Notification.Action button(Context ctx, ApprovalCard card, boolean allow) {
        Intent i = new Intent(ctx, NoticeActionReceiver.class).setAction(ACTION_DECIDE)
                .putExtra(EXTRA_ID, card.noticeId).putExtra(EXTRA_DIGEST, card.digest)
                .putExtra(EXTRA_ALLOW, allow).putExtra(EXTRA_TITLE, card.title);
        PendingIntent pi = PendingIntent.getBroadcast(ctx, card.requestCode(allow), i,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Notification.Action.Builder a = new Notification.Action.Builder(null, allow ? "Approve" : "Deny", pi);
        if (Build.VERSION.SDK_INT >= 31) a.setAuthenticationRequired(true); // unlock first
        return a.build();
    }

    /** POST /api/push/decide. Returns {status, JSONObject or null}. */
    static Object[] decide(Context ctx, String id, String digest, boolean allow) {
        String body = ApprovalCard.decideBody(id, digest, allow);
        if (body == null) return new Object[] {400, null};
        try {
            return post(ctx, "/api/push/decide", body, 60000);
        } catch (Exception e) {
            return new Object[] {0, null};
        }
    }

    private static Object[] post(Context ctx, String path, String json, int readMs) throws Exception {
        String cookie = CookieManager.getInstance().getCookie(HeartbeatJob.API);
        if (cookie == null || cookie.isEmpty()) return new Object[] {401, null};
        HttpURLConnection c = (HttpURLConnection) new URL(HeartbeatJob.API + path).openConnection();
        c.setRequestMethod("POST");
        c.setConnectTimeout(20000);
        c.setReadTimeout(readMs);
        c.setDoOutput(true);
        c.setRequestProperty("Content-Type", "application/json");
        c.setRequestProperty("Origin", "https://aitherium.com");
        c.setRequestProperty("Cookie", cookie);
        try (OutputStream o = c.getOutputStream()) {
            o.write(json.getBytes(StandardCharsets.UTF_8));
        }
        int code = c.getResponseCode();
        StringBuilder sb = new StringBuilder();
        try (InputStream in = code < 400 ? c.getInputStream() : c.getErrorStream()) {
            if (in != null) {
                byte[] b = new byte[4096];
                int n;
                while ((n = in.read(b)) > 0) sb.append(new String(b, 0, n, StandardCharsets.UTF_8));
            }
        }
        JSONObject j = null;
        try { j = new JSONObject(sb.toString()); } catch (Exception e) { /* not JSON */ }
        return new Object[] {code, j};
    }
}
