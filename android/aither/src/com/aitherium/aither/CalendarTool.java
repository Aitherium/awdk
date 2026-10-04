package com.aitherium.aither;

import android.Manifest;
import android.content.ContentUris;
import android.content.Context;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.provider.CalendarContract;

import org.json.JSONArray;
import org.json.JSONObject;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * calendar_read: the owner's events for the next few days, read-only. Offered to an agent
 * only when the owner turned it on in Aither's settings AND Android granted READ_CALENDAR;
 * never on a child's phone.
 */
final class CalendarTool {
    static final String NAME = "calendar_read";
    static final int MAX_EVENTS = 20;

    private CalendarTool() {}

    /** Words that are about a calendar whoever says them. */
    private static final java.util.regex.Pattern CALENDAR_WORDS = java.util.regex.Pattern.compile(
            "\\b(calendar|schedule|scheduled|meetings?|appointments?|events?|agenda|sync)\\b",
            java.util.regex.Pattern.CASE_INSENSITIVE);
    /** Words about time that need "I / my" to be about the owner's own time. */
    private static final java.util.regex.Pattern TIME_WORDS = java.util.regex.Pattern.compile(
            "\\b(plans|busy|free|available|booked|next|today|tonight|tomorrow|this week|next week|"
                    + "weekend|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\\b",
            java.util.regex.Pattern.CASE_INSENSITIVE);
    private static final java.util.regex.Pattern ABOUT_ME = java.util.regex.Pattern.compile(
            "\\b(i|me|my|am i|do i|i'm)\\b", java.util.regex.Pattern.CASE_INSENSITIVE);

    /** Is this plainly a question about the owner's own time (so the calendar is read)? */
    static boolean asksAboutTime(String q) {
        if (q == null) return false;
        return CALENDAR_WORDS.matcher(q).find()
                || (TIME_WORDS.matcher(q).find() && ABOUT_ME.matcher(q).find());
    }

    static boolean allowed(Context c) {
        Config cfg = new Config(c);
        return cfg.toolCalendar() && !"child".equals(cfg.profileKind())
                && c.checkSelfPermission(Manifest.permission.READ_CALENDAR) == PackageManager.PERMISSION_GRANTED;
    }

    static JSONObject spec() throws Exception {
        return new JSONObject().put("type", "function").put("function", new JSONObject()
                .put("name", NAME)
                .put("description", "Read the owner's calendar events (title, start, end, place) "
                        + "for the next few days.")
                .put("parameters", new JSONObject().put("type", "object").put("properties",
                        new JSONObject().put("days", new JSONObject().put("type", "integer")
                                .put("description", "how many days ahead, 1-14 (default 7)")))));
    }

    static String read(Context c, int days) throws Exception {
        if (!allowed(c)) return "{\"error\":\"calendar access is off\"}";
        int d = Math.max(1, Math.min(days <= 0 ? 7 : days, 14));
        long now = System.currentTimeMillis();
        Uri.Builder b = CalendarContract.Instances.CONTENT_URI.buildUpon();
        ContentUris.appendId(b, now);
        ContentUris.appendId(b, now + d * 86_400_000L);
        String[] cols = {CalendarContract.Instances.TITLE, CalendarContract.Instances.BEGIN,
                CalendarContract.Instances.END, CalendarContract.Instances.ALL_DAY,
                CalendarContract.Instances.EVENT_LOCATION};
        JSONArray events = new JSONArray();
        SimpleDateFormat f = new SimpleDateFormat("EEE d MMM HH:mm", Locale.getDefault());
        try (Cursor cur = c.getContentResolver().query(b.build(), cols, null, null,
                CalendarContract.Instances.BEGIN + " ASC")) {
            while (cur != null && cur.moveToNext() && events.length() < MAX_EVENTS) {
                JSONObject e = new JSONObject().put("title", cur.getString(0))
                        .put("start", f.format(new Date(cur.getLong(1))))
                        .put("end", f.format(new Date(cur.getLong(2))))
                        .put("all_day", cur.getInt(3) == 1);
                String where = cur.getString(4);
                if (where != null && !where.isEmpty()) e.put("place", where);
                events.put(e);
            }
        }
        return new JSONObject().put("days", d).put("events", events).toString();
    }

    /** Fixed sample data for measuring the agent: reads nothing from the phone. */
    static String sample() throws Exception {
        return new JSONObject().put("days", 7).put("events", new JSONArray()
                .put(new JSONObject().put("title", "Dentist").put("start", "Tue 6 Oct 09:30")
                        .put("end", "Tue 6 Oct 10:15").put("all_day", false))
                .put(new JSONObject().put("title", "Team sync").put("start", "Wed 7 Oct 14:00")
                        .put("end", "Wed 7 Oct 14:30").put("all_day", false))).toString();
    }
}
