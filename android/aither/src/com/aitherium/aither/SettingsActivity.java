package com.aitherium.aither;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.Switch;
import android.widget.TextView;

import org.json.JSONObject;

/** The owner's controls on the phone: what it lends, when, and a stop switch. */
public class SettingsActivity extends Activity {
    private Config cfg;
    private TextView status;
    private TextView localStatus;
    private final Handler h = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        cfg = new Config(this);
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != 0) {
            requestPermissions(new String[] {Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        col.setPadding(pad, pad * 2, pad, pad);
        TextView title = new TextView(this);
        title.setText("Aither on this phone");
        title.setTextSize(22);
        col.addView(title);
        status = new TextView(this);
        status.setTextSize(15);
        status.setPadding(0, pad, 0, pad);
        col.addView(status);
        col.addView(toggle("Lend memory to my models", "enabled", cfg.enabled()));
        col.addView(toggle("Only while charging", "only_charging", cfg.onlyCharging()));
        col.addView(toggle("Only on Wi-Fi", "only_wifi", cfg.onlyWifi()));
        Button battery = new Button(this);
        battery.setText("Allow running with the screen off");
        battery.setOnClickListener(v -> askBattery());
        col.addView(battery);

        TextView ai = new TextView(this);
        ai.setText("AI on this phone");
        ai.setTextSize(18);
        ai.setPadding(0, pad * 2, 0, pad / 2);
        col.addView(ai);
        col.addView(toggleLlm());
        Button pair = new Button(this);
        pair.setText("Use it from AitherOS in the browser");
        pair.setOnClickListener(v -> {
            // a new token for the browser: the page stores it per origin, the old one stops working
            String t = cfg.rotateLlmToken();
            cfg.set("llm_paired", true);
            Uri u = Uri.parse("https://app.aitherium.com/#local-pair=" + t + "&port=" + LocalProxy.PORT);
            try { startActivity(new Intent(Intent.ACTION_VIEW, u).addCategory(Intent.CATEGORY_BROWSABLE)); }
            catch (RuntimeException e) { /* no browser */ }
        });
        col.addView(pair);
        localStatus = new TextView(this);
        localStatus.setTextSize(14);
        col.addView(localStatus);
        android.widget.ScrollView scroll = new android.widget.ScrollView(this);
        scroll.addView(col);
        setContentView(scroll);
        // only a fresh launch carries a link to act on, never a task restored from history
        if (b == null && (getIntent().getFlags() & Intent.FLAG_ACTIVITY_LAUNCHED_FROM_HISTORY) == 0) {
            handle(getIntent());
        }
    }

    @Override
    protected void onNewIntent(Intent i) {
        super.onNewIntent(i);
        handle(i);
    }

    @Override
    protected void onResume() {
        super.onResume();
        h.post(tick);
    }

    @Override
    protected void onPause() {
        h.removeCallbacks(tick);
        super.onPause();
    }

    private void handle(Intent i) {
        Uri u = i == null ? null : i.getData();
        if (u != null && cfg.acceptPairLink(u)) {
            startForegroundService(new Intent(this, HolderService.class));
            askBattery();
        } else if (cfg.enabled()) {
            startForegroundService(new Intent(this, HolderService.class));
        }
    }

    private void askBattery() {
        PowerManager pm = getSystemService(PowerManager.class);
        if (pm.isIgnoringBatteryOptimizations(getPackageName())) return;
        Intent i = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                Uri.parse("package:" + getPackageName()));
        try { startActivity(i); } catch (RuntimeException e) { /* settings app refused */ }
    }

    private View toggleLlm() {
        Switch s = new Switch(this);
        s.setText("Run Bonsai 1.7B here for AitherOS");
        s.setChecked(cfg.llmEnabled());
        s.setPadding(0, 12, 0, 12);
        s.setOnCheckedChangeListener((v, checked) -> {
            cfg.set("llm_enabled", checked);
            Intent svc = new Intent(this, LlmService.class);
            if (checked) startForegroundService(svc);
            else stopService(svc);
        });
        return s;
    }

    private View toggle(String label, String key, boolean on) {
        Switch s = new Switch(this);
        s.setText(label);
        s.setChecked(on);
        s.setPadding(0, 12, 0, 12);
        s.setOnCheckedChangeListener((v, checked) -> {
            cfg.set(key, checked);
            Intent svc = new Intent(this, HolderService.class);
            if ("enabled".equals(key) && !checked) svc.setAction("stop");
            startForegroundService(svc);
        });
        return s;
    }

    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            StringBuilder sb = new StringBuilder();
            sb.append("Device: ").append(cfg.deviceId().isEmpty() ? "-" : cfg.deviceId()).append('\n');
            sb.append("Relay: ").append(cfg.relay().isEmpty() ? "-" : cfg.relay()).append('\n');
            sb.append("Lends up to ").append(cfg.mb()).append(" MB\n");
            // the public half of this phone's device key: the owner can compare it with the
            // workspace's record (or add it to a sovereign relay's --keys-file)
            sb.append("Key: ").append(cfg.pubkey().isEmpty() ? "-" : cfg.pubkey()).append("\n\n");
            sb.append(HolderService.reason).append('\n');
            try {
                JSONObject st = new JSONObject(HolderService.lastStatus);
                if (st.length() > 0) {
                    sb.append(st.optString("state")).append(" · ").append(st.optString("held"))
                            .append(" keys · ").append(st.optString("calls")).append(" calls · ")
                            .append(st.optString("engine"));
                }
            } catch (Exception e) { /* nothing yet */ }
            status.setText(sb.toString());
            String blocked = cfg.localAiBlocked();
            localStatus.setText((blocked.isEmpty() ? LlmService.reason : "Off: " + blocked)
                    + "\nHousehold check-in: " + HeartbeatJob.last
                    + (cfg.profileKind().isEmpty() ? "" : " (" + cfg.profileKind() + ")"));
            h.postDelayed(this, 1000);
        }
    };
}
