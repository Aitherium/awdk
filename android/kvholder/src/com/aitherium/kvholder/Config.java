package com.aitherium.kvholder;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;

import org.json.JSONException;
import org.json.JSONObject;

/** What this phone knows: the relay, its device id, the owner's cap and its lending policy. */
final class Config {
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

    void set(String k, boolean v) { p.edit().putBoolean(k, v).apply(); }
    void set(String k, String v) { p.edit().putString(k, v).apply(); }
    void set(String k, int v) { p.edit().putInt(k, v).apply(); }

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
        int mb = 2048;
        try { mb = Integer.parseInt(u.getQueryParameter("mb")); } catch (RuntimeException e) { /* default */ }
        p.edit().putString("relay", relay).putString("identity", offline ? "" : idp)
                .putString("code", offline ? "" : code).putString("device_id", dev)
                .putInt("mb", Math.max(256, Math.min(mb, 16384)))
                .putBoolean("paired", offline).putBoolean("enabled", true).apply();
        return true;
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
