package com.aitherium.aither;

import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Typeface;
import android.os.Build;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONObject;

import java.security.SecureRandom;

/**
 * "Add to my devices": this watch joins its owner's devices (the mesh: an Identity node, like
 * the phone) over Bluetooth from a phone in range. The watch advertises only while this
 * screen is open (BleCandidate, stopped in onPause, and BlePair.LIFETIME_MS at most), shows
 * the six-digit SAS once a phone has finished the key exchange, and confirms the received
 * code with Identity itself only after its owner tapped Matches here (BlePair.Candidate).
 *
 * The phone side is Aither > Settings > Nearby devices; Identity refuses a child's account
 * there. The device token Identity answers is kept in this app's private storage ("node")
 * and never shown. Signing in to talk and answer approvals stays the device-code sign-in
 * (WearApi); this is the watch as a device, not as a session.
 */
public class WearBlePair extends Activity {
    private static final int ASK_BLE = 7;
    static final String IDP = "https://idp.aitherium.com";

    private LinearLayout col;
    private BleCandidate radio;
    private volatile int screen;
    private String shownSas;
    private boolean matched;
    private boolean interrupted;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(Ui.BG);
        scroll.setFillViewport(true);
        col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setGravity(Gravity.CENTER_HORIZONTAL);
        int w = getResources().getDisplayMetrics().widthPixels;
        boolean round = getResources().getConfiguration().isScreenRound();
        int side = round ? Math.round(w * 0.146f) : Ui.dp(this, 8);
        col.setPadding(side, round ? side : Ui.dp(this, 8), side, round ? side * 2 : Ui.dp(this, 16));
        scroll.addView(col);
        setContentView(scroll);
        intro();
    }

    @Override
    protected void onPause() {
        // time-boxed to this screen: leaving it stops the advert and the server
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
            failed("Stopped when the screen closed. Nothing was added.");
        }
    }

    // ------------------------------------------------------------------ blocks

    private int clear() {
        col.removeAllViews();
        return ++screen;
    }

    private TextView line(String s, float sp, int color) {
        TextView t = Ui.text(this, s, sp, color);
        t.setGravity(Gravity.CENTER_HORIZONTAL);
        t.setPadding(0, Ui.dp(this, 4), 0, Ui.dp(this, 4));
        col.addView(t, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));
        return t;
    }

    private TextView button(String label, boolean primary, View.OnClickListener l) {
        TextView b = Ui.text(this, label, 15, primary ? Ui.BG : Ui.INK);
        b.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        b.setGravity(Gravity.CENTER);
        b.setMinHeight(Ui.dp(this, 48));
        b.setPadding(Ui.dp(this, 12), Ui.dp(this, 8), Ui.dp(this, 12), Ui.dp(this, 8));
        b.setBackground(Ui.round(this, primary ? Ui.ACCENT : Ui.RAISED, 24, primary ? 0 : Ui.LINE));
        b.setOnClickListener(l);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.topMargin = Ui.dp(this, 6);
        col.addView(b, lp);
        return b;
    }

    private void onUi(int at, Runnable r) {
        runOnUiThread(() -> { if (at == screen) r.run(); });
    }

    // ------------------------------------------------------------------ screens

    private void intro() {
        stopRadio();
        clear();
        matched = false;
        TextView t = line("Add to my devices", 17, Ui.INK);
        t.setTypeface(Typeface.create("sans-serif", Typeface.BOLD));
        if (linked(this)) {
            line("This watch is already one of your devices.", 13, Ui.DIM);
            button("Done", true, v -> finish());
            return;
        }
        line("On your phone open Aither > Settings > Nearby devices, then tap Start.", 13, Ui.DIM);
        button("Start", true, v -> begin());
        button("Cancel", false, v -> finish());
    }

    private void begin() {
        String[] need = BleCandidate.permissions();
        if (!BleCandidate.granted(this)) {
            requestPermissions(need, ASK_BLE);
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
                failed("Without Nearby devices the watch can't be found. Sign in with a code instead.");
                return;
            }
        }
        looking();
    }

    /** Start the radio (once per Start tap), then wait for a phone. */
    private void looking() {
        stopRadio();
        radio = new BleCandidate(this, BlePair.CLASS_WATCH, new BleCandidate.Listener() {
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

    /** "Looking for your phone" with the time left in this opening. */
    private void waiting() {
        final BleCandidate r = radio;
        if (r == null) return;
        int at = clear();
        shownSas = null;
        line("Looking for your phone…", 15, Ui.INK);
        TextView left = line("", 12, Ui.FAINT);
        button("Cancel", false, v -> { stopRadio(); finish(); });
        Runnable count = new Runnable() {
            @Override public void run() {
                if (at != screen || r != radio) return;
                long ms = BlePair.LIFETIME_MS - (System.currentTimeMillis() - r.engine.startedAt());
                left.setText(ms <= 0 ? "" : String.format(java.util.Locale.ROOT, "%d:%02d left",
                        ms / 60000, (ms / 1000) % 60));
                col.postDelayed(this, 1000);
            }
        };
        count.run();
    }

    private void sasShown(String sas) {
        clear();
        // a new SAS (another phone's handshake) needs its own Matches tap
        matched = false;
        shownSas = sas;
        line("Does your phone show", 13, Ui.DIM);
        TextView big = line(sas, 30, Ui.INK);
        big.setTypeface(Typeface.MONOSPACE);
        button("Matches", true, v -> {
            matched = true;
            maybeJoin();
        });
        button("Doesn't match", false, v -> {
            if (radio != null) radio.cancel();
            radio = null;
            failed("Stopped. Nothing was added.");
        });
    }

    /** Both halves in (Matches here, a code from the phone): confirm with Identity. */
    private void maybeJoin() {
        if (!matched || radio == null || shownSas == null) return;
        String code = radio.matches(shownSas);
        if (code == null) {
            clear();
            line("Waiting for your phone to approve…", 14, Ui.INK);
            button("Cancel", false, v -> { stopRadio(); finish(); });
            return;
        }
        stopRadio();
        int at = clear();
        line("Adding this watch…", 15, Ui.INK);
        Context app = getApplicationContext();
        new Thread(() -> {
            String said = join(app, code);
            onUi(at, () -> {
                clear();
                line(said, 14, Ui.INK);
                button("Done", true, v -> finish());
            });
        }, "aither-wear-join").start();
    }

    private void failed(String why) {
        stopRadio();
        clear();
        matched = false;
        line(why == null ? "Stopped." : why, 13, Ui.INK);
        button("Try again", true, v -> intro());
        button("Close", false, v -> finish());
    }

    private void stopRadio() {
        if (radio != null) radio.stop();
        radio = null;
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
    }

    // ------------------------------------------------------------------ Identity

    static SharedPreferences node(Context c) {
        return c.getSharedPreferences("node", Context.MODE_PRIVATE);
    }

    static boolean linked(Context c) {
        SharedPreferences p = node(c);
        return !p.getString("node_id", "").isEmpty() && !p.getString("bearer", "").isEmpty();
    }

    /** This watch's node id: made once, kept. */
    static String nodeId(Context c) {
        SharedPreferences p = node(c);
        String id = p.getString("want_id", "");
        if (id.isEmpty()) {
            byte[] b = new byte[6];
            new SecureRandom().nextBytes(b);
            id = "watch-" + BlePair.hex(b);
            p.edit().putString("want_id", id).apply();
        }
        return id;
    }

    /** Confirm the code as this watch; the outcome in words. Blocking. */
    static String join(Context c, String code) {
        try {
            JSONObject reg = new JSONObject()
                    .put("node_id", nodeId(c))
                    .put("hostname", BlePair.hostname(Build.MODEL, "watch"))
                    .put("platform", "android")
                    .put("node_class", BlePair.nodeClass(BlePair.CLASS_WATCH))
                    .put("inference_kind", "none")
                    .put("cpu_count", Runtime.getRuntime().availableProcessors());
            BleCandidate.Answer a = BleCandidate.confirm(IDP, code, reg);
            String node = a.json.optString("node_id", ""), tok = a.json.optString("bearer_token", "");
            if (a.status != 200 || node.isEmpty() || tok.isEmpty()) return BleCandidate.refused(a.status);
            node(c).edit().putString("node_id", node).putString("bearer", tok)
                    .putString("key", a.json.optString("command_key", "")).apply();
            return "Added. This watch is one of your devices.";
        } catch (Exception e) {
            return BleCandidate.refused(0);
        }
    }
}
