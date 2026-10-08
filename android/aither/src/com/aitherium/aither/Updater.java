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
 * when Android allows that (this app is the installer of record or update owner and holds
 * UPDATE_PACKAGES_WITHOUT_USER_ACTION); otherwise a notification offers it and Android's
 * own installer asks to confirm. Nothing unverified is ever installed.
 */
final class Updater {
    static final String RELEASES = "https://api.github.com/repos/Aitherium/awdk/releases?per_page=30";
    static final String TAG = "aither-android-v";
    /** SHA-256 of the release signing certificate (awdk/docs/KVHOLDER.md; build.py pins it too). */
    static final String RELEASE_CERT = "a55fdd95f14cfabe194caa78a298769561dc4ac5fe5e8dbb9e73472774e81d56";
    static final long CHECK_EVERY_MS = 6L * 3600 * 1000;
    static volatile String last = "not checked yet";

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
        p = ctx.getSharedPreferences("update", Context.MODE_PRIVATE);
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
                // Silent when Android allows it (we are the installer of record and hold
                // UPDATE_PACKAGES_WITHOUT_USER_ACTION); otherwise UpdateResult falls back to the
                // one-tap notification. The same verification gate applies either way.
                if (!installQuietly(apk, best)) offer(best);
                else c.state = best + " is verified and installing";
            }
            return done(c, true);
        } catch (Exception e) {
            c.state = "update check failed: " + e.getClass().getSimpleName();
            return done(c, false);
        }
    }

    private static Check done(Check c, boolean ok) {
        c.ok = ok;
        last = c.state + " · " + new java.util.Date();
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
        if (!ctx.getPackageManager().canRequestPackageInstalls()) return false;
        try {
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
            last = "quiet install failed: " + e.getClass().getSimpleName() + " · " + new java.util.Date();
            return false;
        }
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

    void offer(String version) {
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel("update", "App updates",
                NotificationManager.IMPORTANCE_DEFAULT));
        PendingIntent pi = PendingIntent.getActivity(ctx, 7, new Intent(ctx, UpdateActivity.class),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Notification n = new Notification.Builder(ctx, "update")
                .setSmallIcon(android.R.drawable.stat_sys_download_done)
                .setContentTitle("Aither " + version + " is ready")
                .setContentText("Tap to install. It is checked and signed by Aitherium.")
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
