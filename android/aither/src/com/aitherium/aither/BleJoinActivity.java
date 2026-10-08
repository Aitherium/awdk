package com.aitherium.aither;

import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.view.WindowManager;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.security.SecureRandom;

/**
 * Settings > Add this phone with a nearby phone: the phone-to-phone half of "Nearby devices".
 * This phone is the device being added (the watch's WearBlePair, on a phone): it advertises
 * only while this screen is open (BleCandidate; BlePair.LIFETIME_MS at most), shows the SAS,
 * and confirms the received code with Identity itself once its owner tapped Matches. The
 * answer is kept exactly as a portal-minted link keeps it (NodeLink.remember).
 *
 * A child's phone is never added this way: it joins through the guardian's household flow,
 * which marks it a child's device (Identity pairing_confirm). An adult's approval here would
 * enrol it as an ordinary device, so the screen refuses.
 */
public class BleJoinActivity extends Activity {
    private static final int ASK_BLE = 12;

    private Config cfg;
    private LinearLayout body;
    private BleCandidate radio;
    private String shownSas;
    private boolean matched;
    private boolean interrupted;
    private int screen;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        cfg = new Config(this);
        body = new LinearLayout(this);
        body.setOrientation(LinearLayout.VERTICAL);
        int pad = Ui.dp(this, 16);
        body.setPadding(pad, pad, pad, pad * 2);
        android.widget.ScrollView scroll = new android.widget.ScrollView(this);
        scroll.addView(body);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Ui.BG);
        LinearLayout bar = Ui.bar(this);
        bar.addView(Ui.icon(this, R.drawable.ic_nav_back, "Back", v -> finish()));
        TextView title = Ui.barTitle(this);
        title.setText("Add this phone nearby");
        bar.addView(title);
        root.addView(bar);
        root.addView(Ui.rule(this));
        root.addView(scroll, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));
        setContentView(root);
        Edge.fit(root);
        intro();
    }

    @Override
    protected void onPause() {
        if (radio != null) {
            stopRadio();
            interrupted = true;
        }
        super.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (interrupted) {
            interrupted = false;
            failed("Stopped when you left the screen. Nothing was added.");
        }
    }

    private int clear() {
        body.removeAllViews();
        return ++screen;
    }

    private void intro() {
        stopRadio();
        clear();
        matched = false;
        String no = DeviceLink.childRefusal(cfg.profileKind(), cfg.childDevice());
        if (no != null) {
            body.addView(Ui.note(this, "A grown-up adds a child's phone from their own phone, "
                    + "in the family setup."));
            body.addView(Ui.action(this, "Close", v -> finish()));
            return;
        }
        if (new NodeLink(this).linked()) {
            body.addView(Ui.note(this, "This phone is already one of your devices."));
            body.addView(Ui.action(this, "Done", v -> finish()));
            return;
        }
        body.addView(Ui.note(this, "On a phone that is already yours, open Aither > Settings > Nearby devices, "
                + "then tap Start here. Keep both screens open."));
        body.addView(Ui.action(this, "Start", v -> begin()));
        body.addView(Ui.action(this, "Cancel", v -> finish()));
    }

    private void begin() {
        if (!BleCandidate.granted(this)) {
            requestPermissions(BleCandidate.permissions(), ASK_BLE);
            return;
        }
        looking();
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        super.onRequestPermissionsResult(code, perms, results);
        if (code != ASK_BLE) return;
        for (int r : results) {
            if (r != PackageManager.PERMISSION_GRANTED) {
                failed("Without Nearby devices this phone can't be found. Sign in to Aither instead.");
                return;
            }
        }
        looking();
    }

    private void looking() {
        stopRadio();
        radio = new BleCandidate(this, BlePair.CLASS_PHONE, new BleCandidate.Listener() {
            @Override public void onSas(String sas) { sasShown(sas); }
            @Override public void onSasGone() { matched = false; waiting(); }
            @Override public void onCodeIn() { maybeJoin(); }
            @Override public void onEnded(String why) { failed(why); }
        });
        String[] why = {null};
        if (!radio.start(why)) {
            radio = null;
            failed(why[0]);
            return;
        }
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        waiting();
    }

    private void waiting() {
        if (radio == null) return;
        clear();
        shownSas = null;
        body.addView(Ui.note(this, "Looking for your other phone… (up to "
                + (BlePair.LIFETIME_MS / 60000) + " minutes)"));
        body.addView(Ui.action(this, "Cancel", v -> { stopRadio(); finish(); }));
    }

    private void sasShown(String sas) {
        clear();
        // a new SAS (another phone's handshake) needs its own Matches tap
        matched = false;
        shownSas = sas;
        body.addView(Ui.text(this, "Does your other phone show", 16, Ui.INK));
        TextView big = Ui.text(this, sas, 34, Ui.INK);
        big.setTypeface(android.graphics.Typeface.MONOSPACE);
        body.addView(big);
        body.addView(Ui.action(this, "Matches", v -> { matched = true; maybeJoin(); }));
        body.addView(Ui.action(this, "Doesn't match", v -> {
            if (radio != null) radio.cancel();
            radio = null;
            failed("Stopped. Nothing was added.");
        }));
    }

    private void maybeJoin() {
        if (!matched || radio == null || shownSas == null) return;
        String code = radio.matches(shownSas);
        if (code == null) {
            clear();
            body.addView(Ui.note(this, "Waiting for your other phone to approve…"));
            body.addView(Ui.action(this, "Cancel", v -> { stopRadio(); finish(); }));
            return;
        }
        stopRadio();
        int at = clear();
        body.addView(Ui.note(this, "Adding this phone…"));
        Context app = getApplicationContext();
        new Thread(() -> {
            String said = join(app, code);
            runOnUiThread(() -> {
                if (at != screen) return;
                clear();
                body.addView(Ui.note(this, said));
                body.addView(Ui.action(this, "Done", v -> finish()));
            });
        }, "aither-ble-join").start();
    }

    private void failed(String why) {
        stopRadio();
        clear();
        matched = false;
        body.addView(Ui.note(this, why == null ? "Stopped." : why));
        body.addView(Ui.action(this, "Try again", v -> intro()));
        body.addView(Ui.action(this, "Close", v -> finish()));
    }

    private void stopRadio() {
        if (radio != null) radio.stop();
        radio = null;
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
    }

    /** The node id this phone asks for: its lending or household id, else one made once. */
    static String nodeId(Context c) {
        NodeLink link = new NodeLink(c);
        String id = link.wantedId();
        if (!id.isEmpty()) return id;
        SharedPreferences p = link.prefs();
        id = p.getString("want_id", "");
        if (id.isEmpty()) {
            byte[] b = new byte[6];
            new SecureRandom().nextBytes(b);
            id = "phone-" + BlePair.hex(b);
            p.edit().putString("want_id", id).apply();
        }
        return id;
    }

    /** Confirm the code as this phone (NodeLink.link's registration); the outcome in words. */
    static String join(Context c, String code) {
        try {
            Config cfg = new Config(c);
            JSONObject reg = new JSONObject()
                    .put("node_id", nodeId(c))
                    .put("hostname", NodeLink.hostname(Build.MODEL))
                    .put("platform", "android")
                    .put("node_class", BlePair.nodeClass(BlePair.CLASS_PHONE))
                    .put("inference_kind", "llama-server")
                    .put("seal_pubkey", cfg.pubkey())
                    .put("cpu_count", Runtime.getRuntime().availableProcessors())
                    .put("capabilities", new JSONArray().put("kvholder").put("commands"));
            String idp = cfg.identity().isEmpty() ? "https://idp.aitherium.com" : cfg.identity();
            BleCandidate.Answer a = BleCandidate.confirm(idp, code, reg);
            if (a.status != 200 || !new NodeLink(c).remember(a.json)) return BleCandidate.refused(a.status);
            return "Added. This phone is one of your devices.";
        } catch (Exception e) {
            return BleCandidate.refused(0);
        }
    }
}
