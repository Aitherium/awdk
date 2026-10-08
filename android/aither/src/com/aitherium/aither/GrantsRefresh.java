package com.aitherium.aither;

import org.json.JSONObject;

/**
 * The signed {@code grants-refresh} verb: a guardian changed what this child may open
 * (Family &gt; Apps &amp; agents), so the next time AitherOS is shown it reloads fresh and the
 * edge re-reads the child's grants. Nothing is decided here -- the server enforces every
 * grant; this only stops a stale page from showing a door that has since closed (or hiding
 * one that opened). Reuses the refresh-app reload (MainActivity.freshIfAsked on resume).
 *
 * Kept in its own file so the 0.3.15 voice work in Commands.java/MainActivity.java is
 * touched by one VERBS line and one case only.
 */
final class GrantsRefresh {
    private GrantsRefresh() {}

    static boolean request(Config cfg, JSONObject out) throws Exception {
        cfg.set("refresh_app", true);
        out.put("state", "AitherOS reloads with the new grants the next time it is opened");
        return true;
    }
}
