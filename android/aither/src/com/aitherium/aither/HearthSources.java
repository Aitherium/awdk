package com.aitherium.aither;

/**
 * Hearth's real sources, the pure half (no Android, so test/HearthSourcesCheck runs it on a
 * desktop JVM): the calendar push body and the Settings "Connected" line.
 *
 * The phone sends Hearth (POST /api/tutor/me/hearth-sources/calendar, the signed-in adult's
 * own session) its next DAYS days when "Agents may read my calendar" is on: start, end,
 * all-day, title and place of each event. No attendees, no notes, no calendar account
 * names. Never from a child's phone (HearthLink.allowed). The server bounds it again.
 */
final class HearthSources {
    static final int DAYS = 14;
    static final int MAX_EVENTS = 300;
    static final int TITLE_MAX = 200;
    static final int PLACE_MAX = 200;
    static final String PATH = "/api/tutor/me/hearth-sources";

    private HearthSources() {}

    /** One event as read from CalendarContract.Instances. */
    static final class Event {
        final long begin, end;
        final boolean allDay;
        final String title, place;

        Event(long begin, long end, boolean allDay, String title, String place) {
            this.begin = begin;
            this.end = end;
            this.allDay = allDay;
            this.title = title;
            this.place = place;
        }
    }

    /** JSON string literal, control characters escaped; null -> "". */
    static String q(String s, int max) {
        String v = s == null ? "" : s.trim();
        if (v.length() > max) v = v.substring(0, max);
        StringBuilder b = new StringBuilder("\"");
        for (int i = 0; i < v.length(); i++) {
            char c = v.charAt(i);
            if (c == '"' || c == '\\') b.append('\\').append(c);
            else if (c < 0x20) b.append(String.format("\\u%04x", (int) c));
            else b.append(c);
        }
        return b.append('"').toString();
    }

    /** The push body: {"device":..,"tz_offset_min":..,"events":[...]}, at most MAX_EVENTS. */
    static String body(java.util.List<Event> events, String device, int tzOffsetMin) {
        StringBuilder b = new StringBuilder("{\"device\":").append(q(device, 60))
                .append(",\"tz_offset_min\":").append(tzOffsetMin).append(",\"events\":[");
        int n = 0;
        for (Event e : events) {
            if (n >= MAX_EVENTS) break;
            if (n++ > 0) b.append(',');
            b.append("{\"start\":").append(e.begin).append(",\"end\":").append(Math.max(e.end, e.begin))
                    .append(",\"all_day\":").append(e.allDay)
                    .append(",\"title\":").append(q(e.title, TITLE_MAX));
            if (e.place != null && !e.place.trim().isEmpty()) b.append(",\"location\":").append(q(e.place, PLACE_MAX));
            b.append('}');
        }
        return b.append("]}").toString();
    }

    /** The Settings line, e.g. "Calendar: phone ✓ · Email: david@aitherium.com ✓". */
    static String line(String calendarSource, String emailAddress) {
        String cal = calendarSource == null || calendarSource.isEmpty() ? "not connected"
                : ("feed".equals(calendarSource) ? "connected" : calendarSource) + " ✓";
        String mail = emailAddress == null || emailAddress.isEmpty() ? "not connected"
                : emailAddress + " ✓";
        return "Calendar: " + cal + " · Email: " + mail;
    }
}
