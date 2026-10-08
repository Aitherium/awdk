package com.aitherium.aither;

/**
 * One household notice as the phone shows it, and the ONLY payload an Approve/Deny button
 * may send. Pure Java (no Android classes) so test/ApprovalCardCheck.java runs on a desktop
 * JVM.
 *
 * Binding: the buttons' PendingIntents carry the notice id and the digest of the exact
 * action the card shows (lib/hearth/notices.py). The answer POSTs only those two and the
 * choice to /api/push/decide with this app's own session; the server refuses a card that
 * changed, expired, was answered, or is not addressed to the signed-in account, and the
 * Hearth re-checks the digest before it runs anything. A malformed id or digest never
 * leaves the phone (decideBody returns null).
 */
final class ApprovalCard {
    final String noticeId;
    final String kind;
    final String title;
    final String body;
    final boolean urgent;
    final boolean quiet;
    // approval only ("" / 0 otherwise)
    final String digest;
    final String state;
    final int have;
    final int need;
    final String mine;
    /** who said no (a settled "denied"), and when it ran (epoch seconds, 0 = not run) */
    final String deniedBy;
    final double ranAt;

    ApprovalCard(String noticeId, String kind, String title, String body, boolean urgent,
                 boolean quiet, String digest, String state, int have, int need, String mine) {
        this(noticeId, kind, title, body, urgent, quiet, digest, state, have, need, mine, "", 0);
    }

    ApprovalCard(String noticeId, String kind, String title, String body, boolean urgent,
                 boolean quiet, String digest, String state, int have, int need, String mine,
                 String deniedBy, double ranAt) {
        this.noticeId = noticeId == null ? "" : noticeId;
        this.kind = kind == null ? "" : kind;
        this.title = clip(title, 80);
        this.body = clip(body, 240);
        this.urgent = urgent;
        this.quiet = quiet;
        this.digest = digest == null ? "" : digest.toLowerCase(java.util.Locale.ROOT);
        this.state = state == null ? "" : state;
        this.have = have;
        this.need = need;
        this.mine = mine == null ? "" : mine;
        this.deniedBy = clip(deniedBy, 40);
        this.ranAt = ranAt;
    }

    /** The notice's own link (a same-origin path, e.g. /decide?id=...), "" when it has none. */
    String link = "";

    static boolean validId(String id) { return id != null && id.matches("[0-9a-f]{16}"); }

    /** Where tapping the notification opens: the notice's own same-origin link, so a
     *  decision card opens straight on /decide; anything else (or a Hearth link) on the
     *  Hearth page for this notice. Never another origin. */
    static String openPath(String link, String noticeId) {
        if (link != null && link.length() <= 200 && link.startsWith("/") && !link.startsWith("//")
                && !link.contains("\\") && !link.startsWith("/hearth") && link.chars().allMatch(c -> c > 0x20 && c < 0x7f)) {
            return link;
        }
        return "/hearth/?notice=" + noticeId;
    }

    static boolean validDigest(String d) { return d != null && d.matches("[0-9a-f]{64}"); }

    boolean isApproval() { return "approval".equals(kind) && validDigest(digest); }

    /** Approve/Deny buttons: an open approval this account has not answered yet. */
    boolean answerable() { return isApproval() && "pending".equals(state) && mine.isEmpty(); }

    /** approvals: always the high-importance channel (quiet hours never hold one back). */
    String channel() {
        if (isApproval()) return "approvals";
        return quiet ? "household_quiet" : "household";
    }

    /** A stable notification id per notice, so an answer replaces its own card. */
    int notifyId() { return 0x4e000000 | (noticeId.hashCode() & 0x00ffffff); }

    /** The request code of one button: distinct per notice AND choice, or Android merges them. */
    int requestCode(boolean allow) { return (noticeId.hashCode() * 31) + (allow ? 1 : 2); }

    /** True when a button's extras still describe THIS card (stale intent -> refuse). */
    boolean matches(String id, String dig) {
        return noticeId.equals(id) && digest.equals(dig == null ? "" : dig.toLowerCase(java.util.Locale.ROOT));
    }

    /** The decide request body, or null when the id or digest is not well formed. */
    static String decideBody(String id, String dig, boolean allow) {
        if (!validId(id) || !validDigest(dig)) return null;
        return "{\"notice_id\":\"" + id + "\",\"digest\":\"" + dig.toLowerCase(java.util.Locale.ROOT)
                + "\",\"allow\":" + allow + "}";
    }

    /** The card's last line, the SAME grammar as lib/hearth/notices.status_line and sw.js:
     *  "Approved by you · 1 of 2", "Approved · 2 of 2 · ran 6:41 pm", "Denied by Sam",
     *  "Expired". {@code ranAt} is already written in this phone's clock ("" = not run). */
    static String statusLine(String state, int have, int need, String mine, String deniedBy, String ranAt) {
        String count = have + " of " + need;
        if ("denied".equals(state)) {
            if ("no".equals(mine)) return "Denied by you";
            return deniedBy == null || deniedBy.isEmpty() ? "Denied" : "Denied by " + deniedBy;
        }
        if ("expired".equals(state)) return "Expired";
        if ("approved".equals(state)) return "Approved · " + count;
        if ("done".equals(state)) {
            return ranAt == null || ranAt.isEmpty() ? "Approved · " + count : "Approved · " + count + " · ran " + ranAt;
        }
        if ("failed".equals(state)) return "Approved · " + count + " · did not run";
        if ("yes".equals(mine)) return "Approved by you · " + count;
        if ("no".equals(mine)) return "Denied by you · " + count;
        return "Waiting · " + count;
    }

    static String statusLine(String state, int have, int need, String mine) {
        return statusLine(state, have, need, mine, "", "");
    }

    /** "6:41 pm" in {@code zone}. */
    static String clockText(double epochSeconds, java.time.ZoneId zone) {
        java.time.ZonedDateTime t = java.time.Instant.ofEpochMilli((long) (epochSeconds * 1000)).atZone(zone);
        int h = t.getHour();
        return (h % 12 == 0 ? 12 : h % 12) + ":" + (t.getMinute() < 10 ? "0" : "") + t.getMinute()
                + (h < 12 ? " am" : " pm");
    }

    String statusLine() {
        return statusLine(state, have, need, mine, deniedBy,
                ranAt > 0 ? clockText(ranAt, java.time.ZoneId.systemDefault()) : "");
    }

    /** A card a person no longer acts on: a replacement should not ring again. */
    boolean settled() { return !"pending".equals(state); }

    /** What a refused answer says on the card (the server's detail code -> words). */
    static String refusal(int code, String detail) {
        if (code == 401) return "Sign in to Aither to answer. Tap to open it.";
        if ("card_changed".equals(detail)) return "This card changed. Tap to see it in Aither.";
        if ("expired".equals(detail)) return "This card expired.";
        if (detail != null && detail.startsWith("already_")) return "Already answered.";
        if (code == 0) return "Not sent: no connection. Tap to answer in Aither.";
        return "Not answered. Tap to open Aither.";
    }

    static String clip(String s, int n) {
        if (s == null) return "";
        String t = s.replaceAll("\\s+", " ").trim();
        return t.length() > n ? t.substring(0, n - 1) + "…" : t;
    }
}
