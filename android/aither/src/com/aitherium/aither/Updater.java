package com.aitherium.aither;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.net.ConnectivityManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;

/**
 * Self-update from the app's GitHub releases (Aitherium/awdk, tags aither-android-v*).
 * A newer APK is downloaded into app-private storage and accepted only when its SHA-256
 * matches the release's SHA256SUMS AND it is signed with the release certificate, which
 * is also the certificate this installed app carries. Then it installs without a prompt
 * when Android allows that (UpdatePolicy: a self-update holding
 * UPDATE_PACKAGES_WITHOUT_USER_ACTION, no other app owning updates); a refusal is retried
 * quietly once past Android's throttle (UpdateJob), and only then does one notification
 * open Android's own confirmation. Nothing unverified is ever installed.
 */
final class Updater {
    static final String RELEASES = "https://api.github.com/repos/Aitherium/awdk/releases?per_page=30";
    static final String TAG = "aither-android-v";
    /** SHA-256 of the release signing certificate (awdk/docs/KVHOLDER.md; build.py pins it too). */
    static final String RELEASE_CERT = "a55fdd95f14cfabe194caa78a298769561dc4ac5fe5e8dbb9e73472774e81d56";
    static final long CHECK_EVERY_MS = 6L * 3600 * 1000;
    static volatile String last = "not checked yet";
    static final String PREFS = "update";

    static final class Check {
        boolean ok;
        String latest = "";
        String state = "";
        File apk;
    }

    private final Context ctx;
    private final SharedPreferences p;

    Updater(Context c) {
        ctx = c.getApplicationContext();
        p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    /** What Android knows about this install: who installed it, who owns its updates. */
    static UpdatePolicy.Facts facts(Context ctx) {
        UpdatePolicy.Facts f = new UpdatePolicy.Facts();
        PackageManager pm = ctx.getPackageManager();
        f.me = ctx.getPackageName();
        f.sdk = android.os.Build.VERSION.SDK_INT;
        f.targetSdk = ctx.getApplicationInfo().targetSdkVersion;
        f.updatePermission = ctx.checkSelfPermission("android.permission.UPDATE_PACKAGES_WITHOUT_USER_ACTION")
                == PackageManager.PERMISSION_GRANTED;
        try {
            f.unknownSources = pm.canRequestPackageInstalls();
        } catch (RuntimeException e) {
            f.unknownSources = false;
        }
        try {
            android.content.pm.InstallSourceInfo src = pm.getInstallSourceInfo(f.me);
            f.installer = UpdatePolicy.clean(src.getInstallingPackageName());
            f.initiator = UpdatePolicy.clean(src.getInitiatingPackageName());
            if (f.sdk >= 34) f.updateOwner = UpdatePolicy.clean(src.getUpdateOwnerPackageName());
        } catch (Exception e) {
            f.installer = "unknown (" + e.getClass().getSimpleName() + ")";
        }
        return f;
    }

    /** The update half of collect-diagnostics: enough to tell from afar why a tap was needed. */
    static void diagnostics(Context ctx, JSONObject out) throws org.json.JSONException {
        UpdatePolicy.Facts f = facts(ctx);
        SharedPreferences p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String why = UpdatePolicy.whyNotSilent(f);
        out.put("installer_of_record", f.installer.isEmpty() ? "none" : f.installer)
                .put("initiating_installer", f.initiator.isEmpty() ? "none" : f.initiator)
                .put("update_owner", f.updateOwner.isEmpty() ? "none" : f.updateOwner)
                .put("owns_updates", UpdatePolicy.ownsUpdates(f))
                .put("install_unknown_apps_allowed", f.unknownSources)
                .put("update_without_user_action_granted", f.updatePermission)
                .put("target_sdk", f.targetSdk)
                .put("silent_update_expected", why.isEmpty())
                .put("silent_update_blocker", why)
                .put("last_update_state", p.getString("last_state", last))
                .put("last_update_attempt", new JSONObject()
                        .put("version", p.getString("attempt_version", ""))
                        .put("at", p.getLong("attempt_at", 0))
                        .put("quiet", p.getBoolean("attempt_quiet", false))
                        .put("quiet_tries", p.getInt("attempt_tries", 0))
                        .put("status", p.contains("attempt_status") ? p.getInt("attempt_status", 0) : JSONObject.NULL)
                        .put("status_name", p.contains("attempt_status")
                                ? UpdatePolicy.statusName(p.getInt("attempt_status", 0)) : "none yet")
                        .put("status_message", p.getString("attempt_message", ""))
                        .put("status_at", p.getLong("attempt_status_at", 0)))
                .put("last_installed", new JSONObject()
                        .put("version", p.getString("installed_version", ""))
                        .put("at", p.getLong("installed_at", 0))
                        .put("quiet", p.getBoolean("installed_quiet", false)));
    }

    /** Record a PackageInstaller outcome where diagnostics can read it after a restart. */
    static void recordStatus(Context ctx, String version, int status, String msg, boolean quiet) {
        SharedPreferences.Editor e = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                .putInt("attempt_status", status)
                .putString("attempt_message", UpdatePolicy.clean(msg))
                .putLong("attempt_status_at", System.currentTimeMillis());
        if (status == UpdatePolicy.STATUS_SUCCESS) {
            e.putString("installed_version", UpdatePolicy.clean(version))
                    .putLong("installed_at", System.currentTimeMillis())
                    .putBoolean("installed_quiet", quiet);
        }
        // commit, not apply: a successful install kills this process right after
        e.commit();
    }

    static void remember(Context ctx, String state) {
        last = state + " · " + new java.util.Date();
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString("last_state", last).apply();
    }

    /** Every 6 hours, and only on an unmetered network: the background check. */
    void maybeCheck() {
        if (System.currentTimeMillis() - p.getLong("checked_at", 0) < CHECK_EVERY_MS) return;
        ConnectivityManager cm = ctx.getSystemService(ConnectivityManager.class);
        if (cm == null || cm.isActiveNetworkMetered()) return;
        check(true);
    }

    /** Look for a newer release; when one verifies, offer it (notify=true). Never throws. */
    Check check(boolean notify) {
        Check c = new Check();
        try {
            p.edit().putLong("checked_at", System.currentTimeMillis()).apply();
            if (Flavor.STORE) {
                c.state = "updates come from Google Play";
                return done(c, false);
            }
            String mine = ownCert();
            if (!RELEASE_CERT.equals(mine)) {
                c.state = "this build is not release-signed; it cannot update itself";
                return done(c, false);
            }
            NodeLink.Resp r = get(RELEASES);
            if (r.code != 200) {
                c.state = "GitHub answered " + r.code;
                return done(c, false);
            }
            JSONArray rels = new JSONArray(r.body);
            String best = "";
            JSONObject bestRel = null;
            for (int i = 0; i < rels.length(); i++) {
                JSONObject rel = rels.getJSONObject(i);
                String tag = rel.optString("tag_name", "");
                if (!tag.startsWith(TAG) || rel.optBoolean("draft") || rel.optBoolean("prerelease")) continue;
                String v = tag.substring(TAG.length());
                if (newer(v, best.isEmpty() ? "0" : best)) {
                    best = v;
                    bestRel = rel;
                }
            }
            c.latest = best;
            if (bestRel == null || !newer(best, Config.VERSION)) {
                c.state = "up to date (" + Config.VERSION + ")";
                return done(c, true);
            }
            String apkName = "aither-" + best + ".apk";
            String apkUrl = asset(bestRel, apkName), sumsUrl = asset(bestRel, "SHA256SUMS");
            if (apkUrl.isEmpty() || sumsUrl.isEmpty()) {
                c.state = best + " has no " + (apkUrl.isEmpty() ? apkName : "SHA256SUMS");
                return done(c, false);
            }
            String want = "";
            for (String line : get(sumsUrl).body.split("\n")) {
                String[] f = line.trim().split("\\s+\\*?");
                if (f.length == 2 && f[1].equals(apkName)) want = f[0].toLowerCase(java.util.Locale.ROOT);
            }
            if (want.length() != 64) {
                c.state = "SHA256SUMS does not list " + apkName;
                return done(c, false);
            }
            File dir = new File(ctx.getFilesDir(), "update");
            dir.mkdirs();
            File apk = new File(dir, apkName);
            if (!apk.exists() || !want.equals(sha256(apk))) download(apkUrl, apk);
            String why = verify(apk, want);
            if (!why.isEmpty()) {
                apk.delete();
                c.state = "refused " + best + ": " + why;
                return done(c, false);
            }
            for (File old : dir.listFiles()) if (!old.equals(apk)) old.delete();
            c.apk = apk;
            c.state = best + " is verified and ready to install";
            p.edit().putString("ready", apk.getName()).putString("ready_sha", want).apply();
            if (notify) {
                // Silent when Android allows it (a self-update holding
                // UPDATE_PACKAGES_WITHOUT_USER_ACTION, no other update owner); otherwise
                // UpdateResult retries quietly or hands over Android's own confirmation in
                // one notification. The same verification gate applies either way.
                if (!installQuietly(apk, best)) offer(best, null);
                else c.state = best + " is verified and installing";
            }
            return done(c, true);
        } catch (Exception e) {
            c.state = "update check failed: " + e.getClass().getSimpleName();
            return done(c, false);
        }
    }

    private Check done(Check c, boolean ok) {
        c.ok = ok;
        remember(ctx, c.state);
        return c;
    }

    /** The verified APK waiting to be installed, re-checked now; null when there is none. */
    File ready() {
        String name = p.getString("ready", ""), sha = p.getString("ready_sha", "");
        if (name.isEmpty()) return null;
        File apk = new File(new File(ctx.getFilesDir(), "update"), name);
        return apk.exists() && verify(apk, sha).isEmpty() ? apk : null;
    }

    /** "" when {@code apk} is the expected bytes, this app, newer, and release-signed. */
    String verify(File apk, String wantSha) {
        try {
            if (!wantSha.equals(sha256(apk))) return "SHA-256 does not match SHA256SUMS";
            PackageManager pm = ctx.getPackageManager();
            PackageInfo pi = pm.getPackageArchiveInfo(apk.getPath(), PackageManager.GET_SIGNING_CERTIFICATES);
            if (pi == null || pi.signingInfo == null) return "not a readable APK";
            if (!ctx.getPackageName().equals(pi.packageName)) return "a different app";
            if (pi.getLongVersionCode() <= own().getLongVersionCode()) return "not newer than this app";
            Signature[] s = pi.signingInfo.hasMultipleSigners()
                    ? pi.signingInfo.getApkContentsSigners() : pi.signingInfo.getSigningCertificateHistory();
            if (s == null || s.length != 1) return "not signed by exactly one certificate";
            if (!RELEASE_CERT.equals(certSha(s[0]))) return "not signed with the release certificate";
            return "";
        } catch (Exception e) {
            return "could not be checked (" + e.getClass().getSimpleName() + ")";
        }
    }

    private PackageInfo own() throws PackageManager.NameNotFoundException {
        return ctx.getPackageManager().getPackageInfo(ctx.getPackageName(), PackageManager.GET_SIGNING_CERTIFICATES);
    }

    String ownCert() {
        try {
            Signature[] s = own().signingInfo.getApkContentsSigners();
            return s != null && s.length == 1 ? certSha(s[0]) : "";
        } catch (Exception e) {
            return "";
        }
    }

    /**
     * Hand the verified APK to PackageInstaller asking for no user action. True when the
     * session was committed; the outcome arrives at {@link UpdateResult}. False means the
     * caller should offer the notification instead.
     */
    boolean installQuietly(File apk, String version) {
        // Not gated on "install unknown apps": Android does not consult it for a silent
        // self-update (it gated here before 0.3.32, so browser-installed phones never tried).
        if (!UpdatePolicy.tryQuiet(facts(ctx))) return false;
        try {
            // Two triggers close together (Identity's release autodeploy and a manual
            // "update") must not abandon a session already committed: measured 2026-10-18
            // on the Fold, the second attempt abandoned the first (FAILURE_ABORTED "Session
            // was abandoned") and the throttled second one asked for a tap.
            if (committedInFlight(version)) {
                remember(ctx, version + " is already installing");
                return true;
            }
            abandonMine(ctx);
            int tries = version.equals(p.getString("attempt_version", "")) ? p.getInt("attempt_tries", 0) : 0;
            p.edit().putString("attempt_version", version)
                    .putLong("attempt_at", System.currentTimeMillis())
                    .putBoolean("attempt_quiet", true)
                    .putInt("attempt_tries", tries + 1)
                    .remove("attempt_status").remove("attempt_message").commit();
            int id = writeSession(ctx, apk, true);
            Intent back = new Intent(ctx, UpdateResult.class).putExtra("version", version);
            PendingIntent cb = PendingIntent.getBroadcast(ctx, id, back,
                    PendingIntent.FLAG_MUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
            try (android.content.pm.PackageInstaller.Session s =
                         ctx.getPackageManager().getPackageInstaller().openSession(id)) {
                s.commit(cb.getIntentSender());
            }
            return true;
        } catch (Exception e) {
            remember(ctx, "quiet install failed: " + e.getClass().getSimpleName());
            return false;
        }
    }

    /** A session for {@code version} was committed in the last ten minutes and has not answered. */
    private boolean committedInFlight(String version) {
        if (!version.equals(p.getString("attempt_version", ""))) return false;
        if (p.contains("attempt_status")) return false; // it answered
        if (System.currentTimeMillis() - p.getLong("attempt_at", 0) > UpdatePolicy.IN_FLIGHT_MS) return false;
        for (android.content.pm.PackageInstaller.SessionInfo s
                : ctx.getPackageManager().getPackageInstaller().getMySessions()) {
            if (s.isCommitted()) return true;
        }
        return false;
    }

    /** Drop this app's earlier sessions that were never committed: a committed one is left to finish. */
    static void abandonMine(Context ctx) {
        android.content.pm.PackageInstaller pi = ctx.getPackageManager().getPackageInstaller();
        for (android.content.pm.PackageInstaller.SessionInfo s : pi.getMySessions()) {
            if (s.isCommitted()) continue;
            try {
                pi.abandonSession(s.getSessionId());
            } catch (RuntimeException ignored) {
                // already gone
            }
        }
    }

    /** How many quiet attempts this version has had. */
    int quietTries(String version) {
        return version != null && version.equals(p.getString("attempt_version", "")) ? p.getInt("attempt_tries", 0) : 0;
    }

    /** A PackageInstaller session holding {@code apk}, not yet committed. */
    static int writeSession(Context ctx, File apk, boolean quiet) throws java.io.IOException {
        android.content.pm.PackageInstaller pi = ctx.getPackageManager().getPackageInstaller();
        android.content.pm.PackageInstaller.SessionParams sp = new android.content.pm.PackageInstaller.SessionParams(
                android.content.pm.PackageInstaller.SessionParams.MODE_FULL_INSTALL);
        sp.setAppPackageName(ctx.getPackageName());
        if (quiet && android.os.Build.VERSION.SDK_INT >= 31) {
            sp.setRequireUserAction(android.content.pm.PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED);
        }
        // Become the update owner, so later updates can be silent even after an adb install.
        // Asking needs ENFORCE_UPDATE_OWNERSHIP (a normal permission, declared in the
        // manifest since 0.3.16). Without it createSession throws SecurityException, which
        // broke BOTH the quiet and the tap install from 0.3.13 to 0.3.15 (measured on the
        // kids' phones: "verified and ready to install", no installer session ever made).
        // Ownership is a bonus, so a refusal falls back to a plain session.
        boolean own = android.os.Build.VERSION.SDK_INT >= 34 && ctx.checkSelfPermission(
                "android.permission.ENFORCE_UPDATE_OWNERSHIP") == android.content.pm.PackageManager.PERMISSION_GRANTED;
        if (own) sp.setRequestUpdateOwnership(true);
        int id;
        try {
            id = pi.createSession(sp);
        } catch (SecurityException e) {
            if (!own) throw e;
            sp.setRequestUpdateOwnership(false);
            id = pi.createSession(sp);
        }
        try (android.content.pm.PackageInstaller.Session s = pi.openSession(id);
             InputStream in = new FileInputStream(apk);
             java.io.OutputStream o = s.openWrite("aither.apk", 0, apk.length())) {
            byte[] buf = new byte[1 << 16];
            int n;
            while ((n = in.read(buf)) > 0) o.write(buf, 0, n);
            s.fsync(o);
        }
        return id;
    }

    /**
     * The notification when Android has to ask. {@code confirm} is Android's own confirmation
     * for a session already written (STATUS_PENDING_USER_ACTION's EXTRA_INTENT): the tap goes
     * straight to it instead of building a second session and asking again.
     */
    void offer(String version, Intent confirm) {
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel("update", "App updates",
                NotificationManager.IMPORTANCE_DEFAULT));
        Intent open = new Intent(ctx, UpdateActivity.class);
        if (confirm != null) open.putExtra(UpdateActivity.CONFIRM, confirm);
        PendingIntent pi = PendingIntent.getActivity(ctx, 7, open,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Notification n = new Notification.Builder(ctx, "update")
                .setSmallIcon(android.R.drawable.stat_sys_download_done)
                .setContentTitle("Aither " + version + " is ready")
                .setContentText(UpdatePolicy.askText(facts(ctx)))
                .setContentIntent(pi)
                .setAutoCancel(true)
                .build();
        nm.notify(7, n);
    }

    // ------------------------------------------------------------ helpers

    static boolean newer(String a, String b) {
        String[] x = a.split("\\."), y = b.split("\\.");
        for (int i = 0; i < Math.max(x.length, y.length); i++) {
            int u = i < x.length ? num(x[i]) : 0, v = i < y.length ? num(y[i]) : 0;
            if (u != v) return u > v;
        }
        return false;
    }

    private static int num(String s) {
        try {
            return Integer.parseInt(s);
        } catch (NumberFormatException e) {
            return -1; // a tag that is not a version never wins
        }
    }

    private static String asset(JSONObject rel, String name) {
        JSONArray a = rel.optJSONArray("assets");
        if (a == null) return "";
        for (int i = 0; i < a.length(); i++) {
            JSONObject o = a.optJSONObject(i);
            if (o != null && name.equals(o.optString("name"))) {
                String u = o.optString("browser_download_url", "");
                return u.startsWith("https://github.com/Aitherium/awdk/releases/download/") ? u : "";
            }
        }
        return "";
    }

    private static NodeLink.Resp get(String url) throws java.io.IOException {
        return NodeLink.call("GET", url, null, null, null);
    }

    private static void download(String url, File to) throws java.io.IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(20000);
        c.setReadTimeout(60000);
        c.setRequestProperty("User-Agent", NodeLink.UA);
        if (c.getResponseCode() != 200) throw new java.io.IOException("download " + c.getResponseCode());
        File tmp = new File(to.getPath() + ".part");
        try (InputStream in = c.getInputStream(); FileOutputStream o = new FileOutputStream(tmp)) {
            byte[] b = new byte[1 << 16];
            int n;
            long total = 0;
            while ((n = in.read(b)) > 0) {
                total += n;
                if (total > (200L << 20)) throw new java.io.IOException("too large");
                o.write(b, 0, n);
            }
        }
        if (!tmp.renameTo(to)) throw new java.io.IOException("could not keep the download");
    }

    static String sha256(File f) throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        try (FileInputStream in = new FileInputStream(f)) {
            byte[] b = new byte[1 << 16];
            int n;
            while ((n = in.read(b)) > 0) md.update(b, 0, n);
        }
        return hex(md.digest());
    }

    private static String certSha(Signature s) throws Exception {
        return hex(MessageDigest.getInstance("SHA-256").digest(s.toByteArray()));
    }

    private static String hex(byte[] d) {
        StringBuilder sb = new StringBuilder();
        for (byte b : d) sb.append(String.format("%02x", b & 0xff));
        return sb.toString();
    }
}
