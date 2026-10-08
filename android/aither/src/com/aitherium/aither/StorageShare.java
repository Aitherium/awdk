package com.aitherium.aither;

import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.BatteryManager;
import android.os.PowerManager;
import android.os.StatFs;
import android.webkit.CookieManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * "Share storage with my family's mesh": this phone may lend part of its free space to the
 * household's storage pool. Off unless the household says so (StorageRules has the rules).
 *
 * The switch is the device's household row. On an adult's own phone the toggle in Settings
 * writes it (POST /me/device/storage/share, the device token plus the signed-in owner). A
 * child's phone is refused there and the toggle is disabled: only the guardian turns it on.
 * Each Identity check-in (NodeLink) then says what the phone lends: ``storage`` in its
 * capabilities and the numbers in ``capability_detail.storage``, the same shape adk sends,
 * so the family's pool view (GET /api/tutor/family/storage) adds it up.
 */
final class StorageShare {
    static final String API = "https://api.aitherium.com/api/tutor/me/device/storage/share";
    /** What NodeLink re-asserts besides storage (the pairing's own capabilities). */
    static final String[] BASE_CAPS = {"kvholder", "commands"};
    static volatile String state = "off";

    private StorageShare() {}

    /** This phone's verdict right now. */
    static StorageRules.Verdict verdict(Context ctx) {
        Config cfg = new Config(ctx);
        PowerManager pm = ctx.getSystemService(PowerManager.class);
        StorageRules.Verdict v = StorageRules.decide("child".equals(cfg.profileKind()),
                cfg.storageShare(), cfg.storageQuotaGb(), cfg.storageWifiOnly(),
                cfg.storageChargingOnly(), unmeteredWifi(ctx), charging(ctx),
                pm != null && pm.isPowerSaveMode(), freeBytes(ctx));
        state = v.lent ? "lending " + StorageRules.gib(v.contributedBytes) + " GiB"
                : !v.paused.isEmpty() ? "paused: " + v.paused : v.reason;
        return v;
    }

    /**
     * The check-in fields: ``capabilities`` (only when storage was or is offered, so a phone
     * that never shared keeps whatever Identity stored) and ``capability_detail.storage``.
     * Returns true when it changed the beat.
     */
    static boolean addTo(Context ctx, JSONObject beat) {
        Config cfg = new Config(ctx);
        StorageRules.Verdict v = verdict(ctx);
        boolean opted = cfg.storageShare() && cfg.storageQuotaGb() > 0;
        boolean wasAdvertised = ctx.getSharedPreferences("kvholder", Context.MODE_PRIVATE)
                .getBoolean("storage_advertised", false);
        if (!opted && !wasAdvertised) return false;
        try {
            JSONArray caps = new JSONArray();
            for (String c : BASE_CAPS) caps.put(c);
            if (v.lent) caps.put("storage");
            beat.put("capabilities", caps);
            JSONObject detail = new JSONObject()
                    .put("contributed_gib", StorageRules.gib(v.contributedBytes))
                    .put("quota_gib", StorageRules.gib(v.quotaBytes))
                    .put("used_gib", StorageRules.gib(usedBytes(ctx)))
                    .put("free_gib", StorageRules.gib(freeBytes(ctx)))
                    .put("paused", v.paused)
                    .put("device_class", "phone");
            JSONObject entry = new JSONObject().put("available", true).put("lent", v.lent)
                    .put("detail", detail);
            String why = !v.paused.isEmpty() ? "opted in, paused: " + v.paused : v.reason;
            if (!why.isEmpty()) entry.put("reason", why);
            beat.put("capability_detail", new JSONObject().put("storage", entry));
        } catch (Exception e) {
            return false;
        }
        ctx.getSharedPreferences("kvholder", Context.MODE_PRIVATE).edit()
                .putBoolean("storage_advertised", opted).apply();
        return true;
    }

    /** The owner flipped the toggle on their own phone: tell the household. Off a UI thread. */
    static String optIn(Context ctx, boolean on, int quotaGb) {
        Config cfg = new Config(ctx);
        String token;
        try {
            token = new JSONObject(cfg.familyDevice()).optString("token", "");
        } catch (Exception e) {
            token = "";
        }
        if (token.isEmpty()) return "this phone is not in a household yet";
        try {
            JSONObject body = new JSONObject().put("token", token).put("storage_share", on);
            if (on) {
                body.put("storage_limits", new JSONObject().put("quota_gb", quotaGb)
                        .put("wifi_only", true).put("charging_only", true));
            }
            HttpURLConnection c = (HttpURLConnection) new URL(API).openConnection();
            c.setRequestMethod("POST");
            c.setConnectTimeout(15000);
            c.setReadTimeout(20000);
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json");
            c.setRequestProperty("Origin", "https://aitherium.com");
            c.setRequestProperty("User-Agent", NodeLink.UA);
            String cookie = CookieManager.getInstance().getCookie("https://api.aitherium.com");
            if (cookie != null) c.setRequestProperty("Cookie", cookie);
            try (OutputStream o = c.getOutputStream()) {
                o.write(body.toString().getBytes(StandardCharsets.UTF_8));
            }
            int code = c.getResponseCode();
            InputStream in = code < 400 ? c.getInputStream() : c.getErrorStream();
            String text = in == null ? "" : NodeLink.read(in, 1 << 16);
            if (code == 200) {
                cfg.setStorageLocally(on, quotaGb);
                return "";
            }
            if (code == 403 && text.contains("child_needs_guardian")) return "only a guardian can turn this on";
            if (code == 401) return "signed out: open Aither to sign in again";
            return "the household answered " + code;
        } catch (Exception e) {
            return "could not reach the household (" + e.getClass().getSimpleName() + ")";
        }
    }

    // ------------------------------------------------------------ measurements

    static long freeBytes(Context ctx) {
        try {
            return new StatFs(ctx.getFilesDir().getPath()).getAvailableBytes();
        } catch (RuntimeException e) {
            return 0;
        }
    }

    /** What the pool has placed here: the app-private pool folder (bounded walk). */
    static long usedBytes(Context ctx) {
        File root = new File(ctx.getFilesDir(), "pool");
        if (!root.isDirectory()) return 0;
        long total = 0;
        int seen = 0;
        java.util.ArrayDeque<File> todo = new java.util.ArrayDeque<>();
        todo.add(root);
        while (!todo.isEmpty() && seen < 20000) {
            File[] kids = todo.poll().listFiles();
            if (kids == null) continue;
            for (File f : kids) {
                seen++;
                if (f.isDirectory()) todo.add(f);
                else total += f.length();
            }
        }
        return total;
    }

    static boolean charging(Context ctx) {
        Intent b = ctx.registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        return b != null && b.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0;
    }

    static boolean unmeteredWifi(Context ctx) {
        ConnectivityManager cm = ctx.getSystemService(ConnectivityManager.class);
        if (cm == null) return false;
        Network n = cm.getActiveNetwork();
        NetworkCapabilities nc = n == null ? null : cm.getNetworkCapabilities(n);
        return nc != null && nc.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
                && nc.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED);
    }
}
