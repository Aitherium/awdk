package com.aitherium.aither;

/** What Settings › "Check for an update" says when the check is done (owner, 2026-11-19: the button said nothing
 *  unless an update was ready, so "up to date" and a failed check both looked like a dead button). Pure: no Android. */
final class UpdateAnswer {
    private UpdateAnswer() {}

    static final String COULD_NOT = "Could not check right now. Try again in a minute.";

    /** ready = an update was downloaded and verified; latest = its version; state = Updater.Check.state. */
    static String of(boolean ready, String latest, String state) {
        if (ready) return "Version " + (latest == null || latest.isEmpty() ? "" : latest + " ") + "is ready to install.";
        String s = state == null ? "" : state.trim();
        if (s.isEmpty()) return COULD_NOT;
        String first = s.substring(0, 1).toUpperCase(java.util.Locale.ROOT) + s.substring(1);
        return first.endsWith(".") ? first : first + ".";
    }
}
