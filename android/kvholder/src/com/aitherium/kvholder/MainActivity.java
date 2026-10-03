package com.aitherium.kvholder;

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
public class MainActivity extends Activity {
    private Config cfg;
    private TextView status;
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
        title.setText("Aither KV holder");
        title.setTextSize(22);
        col.addView(title);
        status = new TextView(this);
        status.setTextSize(15);
        status.setPadding(0, pad, 0, pad);
        col.addView(status);
        col.addView(toggle("Lend memory", "enabled", cfg.enabled()));
        col.addView(toggle("Only while charging", "only_charging", cfg.onlyCharging()));
        col.addView(toggle("Only on Wi-Fi", "only_wifi", cfg.onlyWifi()));
        Button battery = new Button(this);
        battery.setText("Allow running with the screen off");
        battery.setOnClickListener(v -> askBattery());
        col.addView(battery);
        setContentView(col);
        handle(getIntent());
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
            h.postDelayed(this, 1000);
        }
    };
}
