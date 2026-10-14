package com.aitherium.aither;

import android.app.Activity;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.FeatureInfo;
import android.content.pm.PackageInfo;
import android.content.pm.PackageInstaller;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.net.ConnectivityManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.List;

/**
 * Over-the-air updates for the watch, with no Play Store and no phone in the loop: the watch
 * reads the same GitHub release the phone app updates from (WearUpdate has the rules), over
 * its own Wi-Fi, every 6 hours while the app or the Tile is used, on an unmetered network.
 *
 * The watch app updates ITSELF: a PackageInstaller session for its own package asking for
 * no user action (UPDATE_PACKAGES_WITHOUT_USER_ACTION). Android lets an app update itself
 * that way whoever installed it first (an adb sideload included), so a watch that got the
 * app once by adb stays current from then on. The watch face is another package, so its
 * update needs one confirmation on the watch: the result arrives as "pending user action"
 * and the confirmation opens the next time Aither is opened (and as a notification when the
 * watch allows notifications).
 *
 * Nothing unverified is installed: the APK's SHA-256 must match the release's SHA256SUMS,
 * it must be the expected package, newer than what is installed, declare the watch feature
 * (the phone APK shares this package name), and be signed by the release certificate --
 * the one this installed app carries.
 *
 * Why not the phone pushing the APK to the watch: the phone/watch channel for apps is the
 * Wearable Data Layer, a Google Play services library this build does not carry by rule.
 */
final class WearUpdater {
    /** SHA-256 of the release signing certificate (the phone's Updater.RELEASE_CERT; build.py pins both). */
    static final String RELEASE_CERT = "a55fdd95f14cfabe194caa78a298769561dc4ac5fe5e8dbb9e73472774e81d56";
    static final String UA = "AitherWear-Updater";
    static volatile String last = "not checked yet";
    /** A confirmation Android asked for (the watch face); opened by the next visible screen. */
    static volatile Intent pendingConfirm;

    private final Context ctx;
    private final SharedPreferences p;

    WearUpdater(Context c) {
        ctx = c.getApplicationContext();
        p = ctx.getSharedPreferences("wear_update", Context.MODE_PRIVATE);
    }

    /** The background check: due, unmetered, off the main thread. Never throws. */
    void maybeCheck() {
        long now = System.currentTimeMillis();
        if (!WearUpdate.due(p.getLong("checked_at", 0), now)) return;
        ConnectivityManager cm = ctx.getSystemService(ConnectivityManager.class);
        if (cm == null || cm.getActiveNetwork() == null || cm.isActiveNetworkMetered()) return;
        p.edit().putLong("checked_at", now).apply();
        new Thread(this::check, "wear-update").start();
    }

    /** Opens a confirmation Android asked for, from a visible screen (WearActivity.onResume). */
    static void showPending(Activity a) {
        Intent confirm = pendingConfirm;
        if (confirm == null) return;
        pendingConfirm = null;
        try {
            a.startActivity(confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
        } catch (RuntimeException e) {
            last = "could not open the install confirmation: " + e.getClass().getSimpleName();
        }
    }

    void check() {
        try {
            if (!RELEASE_CERT.equals(certOf(ctx.getPackageName()))) {
                done("this build is not release-signed; it cannot update itself");
                return;
            }
            JSONArray rels = new JSONArray(text(WearUpdate.RELEASES));
            String best = "";
            JSONObject bestRel = null;
            for (int i = 0; i < rels.length(); i++) {
                JSONObject rel = rels.getJSONObject(i);
                String v = WearUpdate.tagVersion(rel.optString("tag_name", ""),
                        rel.optBoolean("draft"), rel.optBoolean("prerelease"));
                if (!v.isEmpty() && WearUpdate.newer(v, best.isEmpty() ? "0" : best)) {
                    best = v;
                    bestRel = rel;
                }
            }
            if (bestRel == null) {
                done("no release found");
                return;
            }
            String sums = text(asset(bestRel, "SHA256SUMS"));
            String app = updateApp(bestRel, best, sums);
            String face = updateFace(bestRel, sums);
            done(app + (face.isEmpty() ? "" : "; " + face));
        } catch (Exception e) {
            done("update check failed: " + e.getClass().getSimpleName());
        }
    }

    private String updateApp(JSONObject rel, String best, String sums) throws Exception {
        PackageInfo own = info(ctx.getPackageName());
        if (own == null || !WearUpdate.newer(best, own.versionName == null ? "0" : own.versionName)) {
            return "up to date (" + (own == null ? "?" : own.versionName) + ")";
        }
        String name = WearUpdate.wearAsset(best);
        File apk = fetch(rel, name, sums);
        if (apk == null) return best + ": " + name + " missing or not in SHA256SUMS";
        String why = verify(apk, WearUpdate.sumFor(sums, name), ctx.getPackageName(), own.getLongVersionCode(), true);
        if (!why.isEmpty()) {
            apk.delete();
            return "refused " + best + ": " + why;
        }
        install(apk, ctx.getPackageName(), best, true);
        return best + " verified, installing";
    }

    private String updateFace(JSONObject rel, String sums) throws Exception {
        PackageInfo face = info(WearUpdate.FACE_PACKAGE);
        if (face == null) return ""; // the watch face is not installed: nothing to keep current
        List<String> names = new ArrayList<>();
        JSONArray a = rel.optJSONArray("assets");
        for (int i = 0; a != null && i < a.length(); i++) names.add(a.getJSONObject(i).optString("name"));
        String name = WearUpdate.faceAsset(names);
        if (name.isEmpty()) return "";
        String v = name.substring(WearUpdate.FACE_PREFIX.length(), name.length() - 4);
        if (!WearUpdate.newer(v, face.versionName == null ? "0" : face.versionName)) return "";
        File apk = fetch(rel, name, sums);
        if (apk == null) return "watch face " + v + " not in SHA256SUMS";
        String why = verify(apk, WearUpdate.sumFor(sums, name), WearUpdate.FACE_PACKAGE,
                face.getLongVersionCode(), false);
        if (!why.isEmpty()) {
            apk.delete();
            return "refused watch face " + v + ": " + why;
        }
        install(apk, WearUpdate.FACE_PACKAGE, "watch face " + v, false);
        return "watch face " + v + " verified, waiting for a tap";
    }

    /** The asset downloaded into app-private storage (kept when its hash already matches). */
    private File fetch(JSONObject rel, String name, String sums) throws Exception {
        String want = WearUpdate.sumFor(sums, name), url = asset(rel, name);
        if (want.isEmpty() || url.isEmpty()) return null;
        File dir = new File(ctx.getFilesDir(), "update");
        dir.mkdirs();
        File apk = new File(dir, name);
        if (!apk.exists() || !want.equals(sha256(apk))) download(url, apk);
        return apk;
    }

    /** "" when {@code apk} is the expected bytes, package, newer build and release-signed. */
    String verify(File apk, String wantSha, String pkg, long installedCode, boolean watchApp) {
        try {
            if (wantSha.isEmpty() || !wantSha.equals(sha256(apk))) return "SHA-256 does not match SHA256SUMS";
            PackageManager pm = ctx.getPackageManager();
            PackageInfo pi = pm.getPackageArchiveInfo(apk.getPath(),
                    PackageManager.GET_SIGNING_CERTIFICATES | PackageManager.GET_CONFIGURATIONS);
            if (pi == null || pi.signingInfo == null) return "not a readable APK";
            if (!pkg.equals(pi.packageName)) return "a different app";
            if (pi.getLongVersionCode() <= installedCode) return "not newer than what is installed";
            if (watchApp && !declaresWatch(pi)) return "not the watch build";
            Signature[] s = pi.signingInfo.hasMultipleSigners()
                    ? pi.signingInfo.getApkContentsSigners() : pi.signingInfo.getSigningCertificateHistory();
            if (s == null || s.length != 1) return "not signed by exactly one certificate";
            if (!RELEASE_CERT.equals(hex(MessageDigest.getInstance("SHA-256").digest(s[0].toByteArray())))) {
                return "not signed with the release certificate";
            }
            return "";
        } catch (Exception e) {
            return "could not be checked (" + e.getClass().getSimpleName() + ")";
        }
    }

    private static boolean declaresWatch(PackageInfo pi) {
        if (pi.reqFeatures == null) return false;
        for (FeatureInfo f : pi.reqFeatures) {
            if ("android.hardware.type.watch".equals(f.name)) return true;
        }
        return false;
    }

    private void install(File apk, String pkg, String what, boolean self) throws Exception {
        PackageInstaller pi = ctx.getPackageManager().getPackageInstaller();
        PackageInstaller.SessionParams sp = new PackageInstaller.SessionParams(
                PackageInstaller.SessionParams.MODE_FULL_INSTALL);
        sp.setAppPackageName(pkg);
        if (self && android.os.Build.VERSION.SDK_INT >= 31) {
            sp.setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED);
        }
        int id = pi.createSession(sp);
        try (PackageInstaller.Session s = pi.openSession(id)) {
            try (InputStream in = new FileInputStream(apk); OutputStream o = s.openWrite("aither.apk", 0, apk.length())) {
                byte[] buf = new byte[1 << 16];
                int n;
                while ((n = in.read(buf)) > 0) o.write(buf, 0, n);
                s.fsync(o);
            }
            Intent back = new Intent(ctx, WearUpdateResult.class).putExtra("what", what);
            PendingIntent cb = PendingIntent.getBroadcast(ctx, id, back,
                    PendingIntent.FLAG_MUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
            s.commit(cb.getIntentSender());
        }
    }

    /** The installer wants a tap: open it from the next screen, and notify when allowed. */
    static void needsTap(Context ctx, Intent confirm, String what) {
        pendingConfirm = confirm;
        last = what + " needs a tap to install · " + new java.util.Date();
        try {
            NotificationManager nm = ctx.getSystemService(NotificationManager.class);
            if (nm == null || !nm.areNotificationsEnabled()) return;
            nm.createNotificationChannel(new NotificationChannel("update", "App updates",
                    NotificationManager.IMPORTANCE_DEFAULT));
            PendingIntent open = PendingIntent.getActivity(ctx, 11,
                    confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                    PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
            Notification n = new Notification.Builder(ctx, "update")
                    .setSmallIcon(android.R.drawable.stat_sys_download_done)
                    .setContentTitle("Aither update ready")
                    .setContentText("Tap to install " + what + ". Checked and signed by Aitherium.")
                    .setContentIntent(open)
                    .setAutoCancel(true)
                    .build();
            nm.notify(11, n);
        } catch (RuntimeException e) {
            last = what + " needs a tap; open Aither to install";
        }
    }

    private void done(String state) {
        last = state + " · " + new java.util.Date();
        p.edit().putString("last", last).apply();
    }

    // ------------------------------------------------------------ helpers

    private PackageInfo info(String pkg) {
        try {
            return ctx.getPackageManager().getPackageInfo(pkg, PackageManager.GET_SIGNING_CERTIFICATES);
        } catch (PackageManager.NameNotFoundException e) {
            return null;
        }
    }

    private String certOf(String pkg) throws Exception {
        PackageInfo pi = info(pkg);
        if (pi == null || pi.signingInfo == null) return "";
        Signature[] s = pi.signingInfo.getApkContentsSigners();
        return s != null && s.length == 1 ? hex(MessageDigest.getInstance("SHA-256").digest(s[0].toByteArray())) : "";
    }

    private static String asset(JSONObject rel, String name) {
        JSONArray a = rel.optJSONArray("assets");
        for (int i = 0; a != null && i < a.length(); i++) {
            JSONObject o = a.optJSONObject(i);
            if (o != null && name.equals(o.optString("name"))) {
                String u = o.optString("browser_download_url", "");
                return WearUpdate.trustedUrl(u) ? u : "";
            }
        }
        return "";
    }

    private static HttpURLConnection open(String url) throws java.io.IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(20000);
        c.setReadTimeout(60000);
        c.setRequestProperty("User-Agent", UA);
        if (c.getResponseCode() != 200) throw new java.io.IOException("HTTP " + c.getResponseCode());
        return c;
    }

    private static String text(String url) throws java.io.IOException {
        if (url == null || url.isEmpty()) return "";
        try (InputStream in = open(url).getInputStream()) {
            ByteArrayOutputStream b = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) {
                if (b.size() > (4 << 20)) throw new java.io.IOException("too large");
                b.write(buf, 0, n);
            }
            return new String(b.toByteArray(), StandardCharsets.UTF_8);
        }
    }

    private static void download(String url, File to) throws java.io.IOException {
        File tmp = new File(to.getPath() + ".part");
        try (InputStream in = open(url).getInputStream(); FileOutputStream o = new FileOutputStream(tmp)) {
            byte[] b = new byte[1 << 16];
            int n;
            long total = 0;
            while ((n = in.read(b)) > 0) {
                total += n;
                if (total > (100L << 20)) throw new java.io.IOException("too large");
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

    private static String hex(byte[] d) {
        StringBuilder sb = new StringBuilder();
        for (byte b : d) sb.append(String.format("%02x", b & 0xff));
        return sb.toString();
    }
}
