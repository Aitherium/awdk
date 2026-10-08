package com.aitherium.aither;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;

import org.json.JSONException;
import org.json.JSONObject;

/** What this phone knows: the relay, its device id, the owner's cap and its lending policy. */
final class Config {
    /** The app's version; build.py refuses a manifest whose versionName differs. */
    static final String VERSION = "0.3.19";

    private final SharedPreferences p;

    Config(Context c) {
        p = c.getSharedPreferences("kvholder", Context.MODE_PRIVATE);
    }

    String relay() { return p.getString("relay", ""); }
    String identity() { return p.getString("identity", "https://idp.aitherium.com"); }
    String deviceId() { return p.getString("device_id", ""); }
    int mb() { return p.getInt("mb", 2048); }
    String pendingCode() { return p.getString("code", ""); }
    boolean paired() { return p.getBoolean("paired", false); }
    boolean enabled() { return p.getBoolean("enabled", false); }
    boolean onlyCharging() { return p.getBoolean("only_charging", true); }
    boolean onlyWifi() { return p.getBoolean("only_wifi", true); }
    String pubkey() { return p.getString("pubkey", ""); }
    boolean refreshApp() { return p.getBoolean("refresh_app", false); }
    /** A child signed in on this phone (the page's aither.device.child): child shortcuts. */
    boolean childDevice() { return p.getBoolean("child_device", false); }
    /** The signed-in account is the platform owner (Session.owner): owner-only Home apps. */
    boolean platformOwner() { return p.getBoolean("platform_owner", false); }
    /**
     * Answer the household's Family AI Pool with this phone's model. The household's own
     * flag (compute_share on this device's row) is the switch: the owner or a guardian sets it
     * from any browser, and the check-in carries it here. The toggle on this phone writes the
     * same flag; for a few minutes after it is flipped here, the phone's choice wins over a
     * check-in that has not seen it yet.
     */
    boolean shareFamily() { return p.getBoolean("share_family", false); }
    /** DutyService: hold the household inbox open so approval cards arrive in seconds. Off by default. */
    boolean approvalsOnDuty() { return p.getBoolean("approvals_duty", false); }
    static final long LOCAL_WINS_MS = 10 * 60_000L;

    void setShareLocally(boolean on) {
        p.edit().putBoolean("share_family", on).putLong("share_family_at", System.currentTimeMillis()).apply();
    }

    /** From the household check-in: compute_share and its share_limits. */
    void shareFromHousehold(boolean on, boolean acOnly, boolean idleOnly) {
        SharedPreferences.Editor e = p.edit().putBoolean("share_ac_only", acOnly)
                .putBoolean("share_idle_only", idleOnly);
        if (System.currentTimeMillis() - p.getLong("share_family_at", 0) > LOCAL_WINS_MS) {
            e.putBoolean("share_family", on);
        }
        e.apply();
    }

    boolean shareAcOnly() { return p.getBoolean("share_ac_only", true); }
    boolean shareIdleOnly() { return p.getBoolean("share_idle_only", true); }

    /**
     * Lend disk to the family's mesh storage pool (StorageShare / StorageRules). Same shape
     * as shareFamily: the household row's storage_share is the switch and the check-in
     * carries it here; this phone's toggle writes the same flag (an adult's own phone only)
     * and wins for a few minutes over a check-in that has not seen it yet. Off by default.
     */
    boolean storageShare() { return p.getBoolean("storage_share", false); }
    int storageQuotaGb() { return p.getInt("storage_quota_gb", 0); }
    boolean storageWifiOnly() { return p.getBoolean("storage_wifi_only", true); }
    boolean storageChargingOnly() { return p.getBoolean("storage_charging_only", true); }

    void setStorageLocally(boolean on, int quotaGb) {
        SharedPreferences.Editor e = p.edit().putBoolean("storage_share", on)
                .putLong("storage_share_at", System.currentTimeMillis());
        if (quotaGb > 0) e.putInt("storage_quota_gb", quotaGb);
        e.apply();
    }

    /** From the household check-in: storage_share and its storage_limits. */
    void storageFromHousehold(boolean on, int quotaGb, boolean wifiOnly, boolean chargingOnly) {
        SharedPreferences.Editor e = p.edit().putBoolean("storage_wifi_only", wifiOnly)
                .putBoolean("storage_charging_only", chargingOnly);
        if (System.currentTimeMillis() - p.getLong("storage_share_at", 0) > LOCAL_WINS_MS) {
            e.putBoolean("storage_share", on).putInt("storage_quota_gb", Math.max(0, quotaGb));
        }
        e.apply();
    }

    /** Why this phone may not answer the pool now, or "". A child's phone shares only what
     *  its guardian switched on, and never runs the model for the child's own use. */
    String poolBlocked() {
        if (!shareFamily()) return "sharing is off";
        return "";
    }
    /** May agents on this phone read the calendar (the owner's switch; Android asks too)? */
    boolean toolCalendar() { return p.getBoolean("tool_calendar", false); }
    /** Ask Aither: let Gemini Nano (AICore, on this phone) take short tool-free questions. */
    boolean nanoPreferred() { return p.getBoolean("nano_preferred", false); } // opt-in (Settings)
    /** The owner said no to downloading Gemini Nano: do not offer it again. */
    boolean nanoDeclined() { return p.getBoolean("nano_declined", false); }
    /** Does Ask Aither read its answers aloud (on-device voice only; its own switch)? */
    boolean assistSpeak() { return p.getBoolean(Talk.PREF_SPEAK, true); }

    // ---- local AI (LlmService) and the household registry (HeartbeatJob)
    boolean llmEnabled() { return p.getBoolean("llm_enabled", true); }
    /** The per-install token the AitherOS page presents to 127.0.0.1:8486; made once. */
    String llmToken() {
        String t = p.getString("llm_token", "");
        if (t.isEmpty()) t = rotateLlmToken();
        return t;
    }
    String rotateLlmToken() {
        byte[] b = new byte[32];
        new java.security.SecureRandom().nextBytes(b);
        StringBuilder sb = new StringBuilder();
        for (byte x : b) sb.append(String.format("%02x", x & 0xff));
        String t = sb.toString();
        p.edit().putString("llm_token", t).putBoolean("llm_paired", false).apply();
        return t;
    }
    boolean llmPaired() { return p.getBoolean("llm_paired", false); }
    /** "owner" | "child" | "" (not known yet), from the family registry's heartbeat. */
    String profileKind() { return p.getString("profile_kind", ""); }
    String familyDevice() { return p.getString("family_device", ""); }
    /** Family Shield: "off" | "filtered" | "allowlist", as the household last said. */
    String shieldMode() { return p.getString("shield_mode", "off"); }
    /** The ETag of the last household policy this phone read. */
    String shieldEtag() { return p.getString("shield_etag", ""); }
    /** Family Shield's watch (ShieldWatch): what the household last heard, "on" | "off:<why>" | "". */
    String shieldReported() { return p.getString("shield_reported", ""); }
    /** When this phone first saw its filter down (0 = it is not, as far as it knows). */
    long shieldOffSince() { return p.getLong("shield_off_since", 0); }
    /** onRevoke ran (Settings, or another VPN) since the filter last came up. */
    boolean shieldRevoked() { return p.getBoolean("shield_revoked", false); }
    /** Has this app asked Android to stop battery-optimizing it (once, for a household phone)? */
    boolean batteryAsked() { return p.getBoolean("battery_asked", false); }

    /** May this phone run the owner's local model? Never a child's; and only once the
     *  workspace has vouched for it (registry says owner, or the owner paired it). */
    String localAiBlocked() {
        if ("child".equals(profileKind())) return "not on a child's phone";
        if ("owner".equals(profileKind()) || paired()) return "";
        return "connect this phone to your workspace first";
    }

    void set(String k, boolean v) { p.edit().putBoolean(k, v).apply(); }
    void set(String k, String v) { p.edit().putString(k, v).apply(); }
    void set(String k, int v) { p.edit().putInt(k, v).apply(); }
    void set(String k, long v) { p.edit().putLong(k, v).apply(); }

    /** A pairing link from the owner: store it; the service confirms the code with identity. */
    boolean acceptPairLink(Uri u) {
        if (u == null || !"aitherkv".equals(u.getScheme()) || !"pair".equals(u.getHost())) return false;
        String relay = u.getQueryParameter("r"), idp = u.getQueryParameter("i");
        String code = u.getQueryParameter("c"), dev = u.getQueryParameter("d");
        if (relay == null || !relay.startsWith("wss://") || dev == null) return false;
        // No code = a sovereign relay (`serve --keys-file`): the owner trusts this phone's key
        // by adding it to the relay's file, so there is nothing to confirm with identity.
        boolean offline = code == null || code.isEmpty();
        if (!offline && (idp == null || !idp.startsWith("https://"))) return false;
        // a link is used once: Android re-delivers the launching intent when the activity is
        // recreated (an app update, a restored task), and that must not unpair the phone
        String once = code == null ? "" : code;
        if (!once.isEmpty() && once.equals(p.getString("last_code", ""))) return false;
        if (!once.isEmpty()) p.edit().putString("last_code", once).apply();
        int mb = 2048;
        try { mb = Integer.parseInt(u.getQueryParameter("mb")); } catch (RuntimeException e) { /* default */ }
        p.edit().putString("relay", relay).putString("identity", offline ? "" : idp)
                .putString("code", offline ? "" : code).putString("device_id", dev)
                .putInt("mb", Math.max(256, Math.min(mb, 16384)))
                .putBoolean("paired", offline).putBoolean("enabled", true).apply();
        return true;
    }

    /** A guardian's single-use workspace pairing code ({code, until}), kept until it expires. */
    boolean acceptPairCode(String json) {
        try {
            JSONObject j = new JSONObject(json);
            String code = j.optString("code", "");
            long until = j.optLong("until", 0);
            if (!code.matches("[A-Z0-9]{6,12}") || until <= System.currentTimeMillis()) return false;
            if (code.equals(p.getString("last_pair_code", ""))) return false; // used once
            if (code.equals(p.getString("pair_code", ""))) return false; // already waiting
            p.edit().putString("pair_code", code).putLong("pair_until", until).apply();
            return true;
        } catch (JSONException e) {
            return false;
        }
    }

    /** The waiting pairing code, or "" (none, or expired). Taking it spends it. */
    String takePairCode() {
        String code = p.getString("pair_code", "");
        if (code.isEmpty() || p.getLong("pair_until", 0) <= System.currentTimeMillis()) return "";
        p.edit().remove("pair_code").putString("last_pair_code", code).apply();
        return code;
    }

    /** What the holder page needs. No pairing code: the page never sees a credential. */
    String forPage() {
        try {
            return new JSONObject().put("relay", relay()).put("device_id", deviceId())
                    .put("mb", mb()).toString();
        } catch (JSONException e) {
            return "{}";
        }
    }
}
