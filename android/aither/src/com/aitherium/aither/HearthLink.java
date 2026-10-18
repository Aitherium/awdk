package com.aitherium.aither;

import android.Manifest;
import android.content.ContentUris;
import android.content.Context;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.provider.CalendarContract;
import android.webkit.CookieManager;

import org.json.JSONObject;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.TimeZone;

/**
 * The phone's side of Hearth's real sources: push this phone's calendar to the signed-in
 * adult's Hearth, forget it when the switch goes off, and read the "Connected" line.
 *
 * Same session and origin as the household check-in (HeartbeatJob): this app's WebView
 * cookie, POSTed as JSON to the Veil tutor proxy, which turns it into the caller's bearer.
 * Nothing in a request names a user; the server derives who from the session and refuses
 * a child account. Blocking: call off the main thread.
 */
final class HearthLink {
    static volatile String last = "never";

    private HearthLink() {}

    /** Pushing is allowed: the calendar switch is on, Android granted READ_CALENDAR, and
     *  this is not a child's phone. */
    static boolean allowed(Context c) {
        Config cfg = new Config(c);
        return cfg.toolCalendar() && !cfg.childDevice() && !"child".equals(cfg.profileKind())
                && c.checkSelfPermission(Manifest.permission.READ_CALENDAR) == PackageManager.PERMISSION_GRANTED;
    }

    /** The next HearthSources.DAYS days of this phone's calendar. */
    static List<HearthSources.Event> read(Context c) {
        List<HearthSources.Event> out = new ArrayList<>();
        long now = System.currentTimeMillis();
        Uri.Builder b = CalendarContract.Instances.CONTENT_URI.buildUpon();
        ContentUris.appendId(b, now - 86_400_000L);
        ContentUris.appendId(b, now + HearthSources.DAYS * 86_400_000L);
        String[] cols = {CalendarContract.Instances.TITLE, CalendarContract.Instances.BEGIN,
                CalendarContract.Instances.END, CalendarContract.Instances.ALL_DAY,
                CalendarContract.Instances.EVENT_LOCATION};
        try (Cursor cur = c.getContentResolver().query(b.build(), cols, null, null,
                CalendarContract.Instances.BEGIN + " ASC")) {
            while (cur != null && cur.moveToNext() && out.size() < HearthSources.MAX_EVENTS) {
                out.add(new HearthSources.Event(cur.getLong(1), cur.getLong(2), cur.getInt(3) == 1,
                        cur.getString(0), cur.getString(4)));
            }
        }
        return out;
    }

    /** Push now when allowed. True when done (sent, or nothing to send). */
    static boolean push(Context c) {
        if (!allowed(c)) return true;
        try {
            int tz = TimeZone.getDefault().getOffset(System.currentTimeMillis()) / 60_000;
            String body = HearthSources.body(read(c), android.os.Build.MODEL, tz);
            int code = send(c, "POST", "/calendar", body);
            last = code == 200 ? "calendar sent " + new java.util.Date() : "calendar answered " + code;
            return code == 200 || code == 401 || code == 403;
        } catch (Exception e) {
            last = "calendar failed: " + e.getClass().getSimpleName();
            return false;
        }
    }

    /** The switch went off: Hearth deletes what this phone sent. */
    static void forget(Context c) {
        try {
            send(c, "DELETE", "/calendar", null);
        } catch (Exception e) {
            last = "forget failed: " + e.getClass().getSimpleName();
        }
    }

    /** The Connected line from Hearth, or null when it could not be read. */
    static String connectedLine(Context c) {
        try {
            HttpURLConnection h = open(c, "GET", "");
            if (h.getResponseCode() != 200) return null;
            JSONObject j;
            try (InputStream in = h.getInputStream()) {
                j = new JSONObject(new String(in.readAllBytes(), StandardCharsets.UTF_8));
            }
            JSONObject cal = j.optJSONObject("calendar");
            JSONObject mail = j.optJSONObject("inbox");
            return HearthSources.line(cal == null ? "" : cal.optString("source", ""),
                    mail == null ? "" : mail.optString("address", ""));
        } catch (Exception e) {
            return null;
        }
    }

    private static HttpURLConnection open(Context c, String method, String sub) throws Exception {
        HttpURLConnection h = (HttpURLConnection) new URL(HeartbeatJob.API + HearthSources.PATH + sub).openConnection();
        h.setRequestMethod(method);
        h.setConnectTimeout(20000);
        h.setReadTimeout(30000);
        h.setRequestProperty("Accept", "application/json");
        h.setRequestProperty("Content-Type", "application/json");
        h.setRequestProperty("Origin", "https://aitherium.com");
        h.setRequestProperty("User-Agent", "Mozilla/5.0 (Linux; Android) Aither/0.2");
        String cookie = CookieManager.getInstance().getCookie(HeartbeatJob.API);
        if (cookie != null) h.setRequestProperty("Cookie", cookie);
        return h;
    }

    private static int send(Context c, String method, String sub, String body) throws Exception {
        HttpURLConnection h = open(c, method, sub);
        if (body != null) {
            h.setDoOutput(true);
            try (OutputStream o = h.getOutputStream()) {
                o.write(body.getBytes(StandardCharsets.UTF_8));
            }
        }
        int code = h.getResponseCode();
        h.disconnect();
        return code;
    }
}
