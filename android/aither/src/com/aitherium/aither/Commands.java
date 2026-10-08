package com.aitherium.aither;

import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.os.BatteryManager;
import android.os.Build;
import android.os.PowerManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * The device half of the command channel (Identity's identity_node_commands, adk's
 * node_commands): what the owner can ask this phone to do from their device page.
 *
 * The language is CLOSED and enforced here, whatever the server sent: a command runs only
 * when its HMAC-SHA256 over the canonical fields checks out against this phone's key,
 * it names this phone, it has not expired, its id was never run here, and its verb and
 * arguments are in {@link #VERBS}. There is no shell, path, URL or free text. A result is
 * signed with the same key, so Identity can tell it came from this phone.
 */
final class Commands {
    static final String[] SIGNED = {"id", "tenant_id", "node_id", "verb", "args", "issued_by",
            "issued_at", "expires_at"};
    static final String[] RESULT = {"id", "node_id", "ok", "output"};
    static final int SEEN_MAX = 200;

    /** verb -> argument -> allowed values. Everything else is refused. */
    static final Map<String, Map<String, List<String>>> VERBS = new HashMap<>();

    static {
        Map<String, List<String>> none = new HashMap<>();
        Map<String, List<String>> via = new HashMap<>();
        via.put("via", Arrays.asList("lan", "tunnel", "local"));
        VERBS.put("collect-diagnostics", none);
        VERBS.put("lend-on", via);
        VERBS.put("lend-off", none);
        VERBS.put("refresh-app", none);
        VERBS.put("grants-refresh", none); // GrantsRefresh.java
        VERBS.put("update-model", none);
        VERBS.put("check-update", none);
        VERBS.put("update", none); // the adk name for the same thing: check, then prompt
    }

    private final Context ctx;
    private final NodeLink node;
    private final Config cfg;

    Commands(Context c, NodeLink n) {
        ctx = c;
        node = n;
        cfg = new Config(c);
    }

    /** Why {@code cmd} may not run here, or "" when it may. */
    static String refuse(JSONObject cmd, String keyHex, String nodeId, long nowS, List<String> seen) {
        if (cmd == null) return "not an object";
        if (keyHex == null || keyHex.isEmpty()) return "no command key on this device";
        String expect;
        try {
            expect = Canon.hmac(keyHex, Canon.fields(cmd, SIGNED));
        } catch (Canon.NotCanonical e) {
            return "not canonical: " + e.getMessage();
        }
        if (!Canon.same(expect, cmd.optString("sig", ""))) return "bad signature";
        if (!nodeId.equals(cmd.optString("node_id", ""))) return "addressed to another device";
        Object exp = cmd.opt("expires_at");
        long expires = exp instanceof Integer || exp instanceof Long ? ((Number) exp).longValue() : 0;
        if (expires <= nowS) return "expired";
        if (seen.contains(cmd.optString("id", ""))) return "already run";
        String verb = cmd.optString("verb", "");
        Map<String, List<String>> allowed = VERBS.get(verb);
        if (allowed == null) return "verb not allowed on this device";
        Object a = cmd.opt("args");
        if (a != null && a != JSONObject.NULL && !(a instanceof JSONObject)) return "bad args";
        JSONObject args = a instanceof JSONObject ? (JSONObject) a : new JSONObject();
        for (java.util.Iterator<String> it = args.keys(); it.hasNext(); ) {
            String k = it.next();
            List<String> vals = allowed.get(k);
            if (vals == null || !vals.contains(String.valueOf(args.opt(k)))) {
                return "argument not allowed for " + verb;
            }
        }
        return "";
    }

    static JSONObject signResult(String keyHex, JSONObject result) throws Exception {
        result.put("sig", Canon.hmac(keyHex, Canon.fields(result, RESULT)));
        return result;
    }

    /**
     * Verify, run and sign one delivered command. Null when there is nothing to report:
     * a command Identity could not have signed for this phone gets no answer (a result
     * would need this phone's key, and the server records only signed ones anyway).
     */
    JSONObject handle(JSONObject cmd) {
        if (node.key().isEmpty()) return null; // nothing to check a signature with, or sign with
        SharedPreferences p = node.prefs();
        List<String> seen = seen(p);
        String why = refuse(cmd, node.key(), node.nodeId(), System.currentTimeMillis() / 1000, seen);
        String id = cmd == null ? "" : cmd.optString("id", "");
        if ("bad signature".equals(why) || "addressed to another device".equals(why)
                || id.isEmpty() || why.startsWith("not ") || "already run".equals(why)) {
            return null;
        }
        // remembered BEFORE it runs: a crash mid-verb must not make it run twice
        seen.add(id);
        while (seen.size() > SEEN_MAX) seen.remove(0);
        p.edit().putString("seen", new JSONArray(seen).toString()).commit();
        boolean ok;
        JSONObject out = new JSONObject();
        try {
            if (!why.isEmpty()) {
                ok = false;
                out.put("refused", why);
            } else {
                ok = run(cmd.getString("verb"), out);
            }
        } catch (Exception e) {
            ok = false;
            try { out.put("error", e.getClass().getSimpleName()); } catch (Exception ignored) { /* */ }
        }
        try {
            JSONObject r = new JSONObject().put("id", id).put("node_id", node.nodeId())
                    .put("ok", ok).put("output", out);
            return signResult(node.key(), r);
        } catch (Exception e) {
            return null;
        }
    }

    private static List<String> seen(SharedPreferences p) {
        List<String> l = new ArrayList<>();
        try {
            JSONArray a = new JSONArray(p.getString("seen", "[]"));
            for (int i = 0; i < a.length(); i++) l.add(a.getString(i));
        } catch (Exception e) { /* empty */ }
        return l;
    }

    // ------------------------------------------------------------ the verbs

    private boolean run(String verb, JSONObject out) throws Exception {
        switch (verb) {
            case "collect-diagnostics":
                diagnostics(out);
                return true;
            case "lend-on":
                return lend(true, out);
            case "lend-off":
                return lend(false, out);
            case "refresh-app":
                // the WebView is the activity's: it clears its cache the next time it is shown
                cfg.set("refresh_app", true);
                out.put("state", "AitherOS reloads fresh the next time it is opened");
                return true;
            case "grants-refresh":
                return GrantsRefresh.request(cfg, out);
            case "update-model":
                return model(out);
            case "check-update":
            case "update":
                Updater.Check c = new Updater(ctx).check(true);
                out.put("current", Config.VERSION).put("latest", c.latest).put("state", c.state);
                return c.ok;
            default:
                return false;
        }
    }

    private void diagnostics(JSONObject out) throws Exception {
        Intent bat = ctx.registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        int level = bat == null ? -1 : bat.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
        int plugged = bat == null ? 0 : bat.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0);
        PowerManager pm = ctx.getSystemService(PowerManager.class);
        out.put("app_version", Config.VERSION)
                .put("android_sdk", Build.VERSION.SDK_INT)
                .put("model", Build.MANUFACTURER + " " + Build.MODEL)
                .put("battery_pct", level)
                .put("charging", plugged != 0)
                .put("thermal_status", pm.getCurrentThermalStatus())
                .put("lend_enabled", cfg.enabled())
                .put("lend_paired", cfg.paired())
                .put("lend_state", HolderService.reason)
                .put("local_ai", cfg.llmEnabled())
                .put("local_ai_blocked", cfg.localAiBlocked())
                .put("local_ai_state", LlmService.reason)
                .put("model_ready", node.modelReady())
                .put("household_checkin", HeartbeatJob.last)
                .put("profile_kind", cfg.profileKind())
                .put("update_state", Updater.last);
    }

    private boolean lend(boolean on, JSONObject out) throws Exception {
        if (on && cfg.relay().isEmpty()) {
            out.put("state", "this phone has no workspace relay; pair it for lending first");
            return false;
        }
        cfg.set("enabled", on);
        Intent i = new Intent(ctx, HolderService.class);
        try {
            if (on) ctx.startForegroundService(i);
            else ctx.startService(i.setAction("stop"));
            out.put("state", on ? "lending turned on" : "lending turned off");
        } catch (RuntimeException e) {
            // Android may refuse to start a service from the background: the setting
            // holds, and the service follows it the next time Aither runs.
            out.put("state", (on ? "on" : "off") + " from the next time Aither runs ("
                    + e.getClass().getSimpleName() + ")");
        }
        return true;
    }

    /** Check the model against its pin; a damaged copy is removed and fetched again on use. */
    private boolean model(JSONObject out) throws Exception {
        File m = new File(ctx.getFilesDir(), "models/" + LlmService.MODEL_FILE);
        out.put("pinned_sha256", LlmService.MODEL_SHA256);
        if (!m.exists()) {
            out.put("state", "not downloaded yet; it downloads on first use");
            return true;
        }
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        try (FileInputStream in = new FileInputStream(m)) {
            byte[] b = new byte[1 << 16];
            int n;
            while ((n = in.read(b)) > 0) md.update(b, 0, n);
        }
        StringBuilder hex = new StringBuilder();
        for (byte x : md.digest()) hex.append(String.format("%02x", x & 0xff));
        if (LlmService.MODEL_SHA256.equals(hex.toString())) {
            out.put("state", "the model on this phone matches its pin");
            return true;
        }
        boolean gone = m.delete();
        out.put("state", gone ? "the model did not match its pin; removed, it downloads again on use"
                : "the model did not match its pin and could not be removed");
        return gone;
    }
}
