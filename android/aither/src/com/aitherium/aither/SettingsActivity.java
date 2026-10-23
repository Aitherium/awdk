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
import android.widget.LinearLayout;
import android.widget.Switch;
import android.widget.TextView;

import org.json.JSONObject;

/** The owner's controls on the phone: what it lends, when, and a stop switch. */
public class SettingsActivity extends Activity {
    private Config cfg;
    private TextView status;
    private TextView storageStatus;
    private TextView localStatus;
    private TextView nodeStatus;
    private TextView alertsStatus;
    private TextView workerStatus;
    private Switch alerts;
    private TextView connected;
    private final Handler h = new Handler(Looper.getMainLooper());
    private int ticks;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        cfg = new Config(this);
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != 0) {
            getSharedPreferences("push", MODE_PRIVATE).edit().putBoolean("notif_asked", true).apply();
            requestPermissions(new String[] {Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
        // A child's phone gets the lite screen: no lending, assistant, calendar or model switches.
        boolean lite = cfg.childDevice();
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        int pad = Ui.dp(this, 16);
        col.setPadding(pad, pad / 2, pad, pad * 2);
        col.addView(Ui.note(this, lite
                ? "This phone's part of Aither. Your apps are in the bar at the bottom."
                : "This phone's part of Aither. Your apps and your account are in the bar at the bottom."));
        status = dim(); // lending status, refreshed every second by tick
        storageStatus = dim(); // the family storage pool, refreshed by tick
        localStatus = dim(); // the model on this phone
        nodeStatus = dim(); // version, workspace node, updates
        alertsStatus = dim(); // instant alerts: duty + permission

        // ---- Alerts: first, for every adult. Decision cards and household approvals reach
        // this phone through its own inbox (Notices); "instant" = DutyService's long-poll.
        // A child's phone keeps the family wording (no switch here).
        if (!"child".equals(cfg.profileKind()) && !lite) {
            col.addView(Ui.section(this, "Alerts"));
            LinearLayout al = Ui.card(this);
            alerts = Ui.style(new Switch(this));
            alerts.setText("Instant alerts on this phone");
            alerts.setChecked(alertsOn());
            alerts.setOnCheckedChangeListener((v, checked) -> setAlerts(checked));
            row(al, alerts, "Decisions waiting for you and your home's approvals reach this phone in "
                    + "seconds, even with Aither closed. Off: they arrive within 15 minutes.");
            al.addView(alertsStatus);
            row(al, Ui.action(this, "Send a test alert", v -> sendTest()),
                    "Aither sends this phone one alert, the same way a decision card arrives.");
            col.addView(al);
        }

        // ---- Account & this phone
        col.addView(Ui.section(this, "Account & this phone"));
        LinearLayout acct = Ui.card(this);
        if (!lite) {
            row(acct, Ui.action(this, "My account", v -> startActivity(new Intent(Intent.ACTION_VIEW,
                    Uri.parse(AppTabs.ORIGIN + AppTabs.ACCOUNT_PATH)).setClass(this, MainActivity.class))),
                    "Your profile, sign-in and security, opened in Aither.");
            row(acct, Ui.action(this, "Link a device", v -> startActivity(new Intent(this, LinkActivity.class))),
                    "Sign in your watch, TV or laptop: scan its code with your camera, or type it here.");
            row(acct, Ui.action(this, "Nearby devices", v -> startActivity(new Intent(this, NearbyDevicesActivity.class))),
                    "Add a watch or a new phone that is next to you, over Bluetooth.");
            row(acct, Ui.action(this, "Add this phone nearby", v -> startActivity(new Intent(this, BleJoinActivity.class))),
                    "Join your devices from a phone that is already yours, over Bluetooth.");
        }
        row(acct, Ui.action(this, "Allow running with the screen off", v -> askBattery()),
                "Lets Aither check in and finish work while the phone sleeps.");
        if (!lite) {
            row(acct, Ui.action(this, "Make Aither this phone's assistant", v -> askAssistantRole()),
                    "Hold the home button (or your assistant gesture) to ask Aither anything.");
            Switch cal = Ui.style(new Switch(this));
            cal.setText("Agents may read my calendar");
            cal.setChecked(cfg.toolCalendar());
            cal.setOnCheckedChangeListener((v, checked) -> {
                cfg.set("tool_calendar", checked);
                if (checked && checkSelfPermission(Manifest.permission.READ_CALENDAR) != 0) {
                    requestPermissions(new String[] {Manifest.permission.READ_CALENDAR}, 2);
                }
                syncHearth(checked);
            });
            row(acct, cal, "Only read when an agent needs it; Android asks you too. Your Hearth also "
                    + "sees the next 14 days (titles, times, places). Off deletes them there.");
            // ---- Connected: what Hearth reads for real (one row; Hearth's own page is elsewhere)
            connected = dim();
            connected.setText("Connected: checking…");
            acct.addView(connected);
            row(acct, Ui.action(this, "Connect email", v -> startActivity(new Intent(Intent.ACTION_VIEW,
                    Uri.parse(AppTabs.ORIGIN + "/workspace/settings?tab=mail-calendar")).setClass(this, MainActivity.class))),
                    "Your mailbox for Hearth to read (read only; sends still ask you first).");
            refreshConnected();
        }
        col.addView(acct);

        // ---- Updates
        col.addView(Ui.section(this, "Updates"));
        LinearLayout upd = Ui.card(this);
        if (!Flavor.STORE) { // the Play build is updated by Google Play
            row(upd, Ui.action(this, "Check for an update", v -> new Thread(() -> {
                Updater.Check c = new Updater(this).check(false);
                if (c.apk != null) startActivity(new Intent(this, UpdateActivity.class));
            }, "aither-update").start()), "Aither also checks by itself and tells you when one is ready.");
        } else {
            row(upd, Ui.text(this, "Google Play keeps Aither up to date", 16, Ui.INK), "Nothing to do here.");
        }
        col.addView(upd);

        // ---- AI & voice on this phone
        if (!lite) {
            col.addView(Ui.section(this, "AI & voice on this phone"));
            LinearLayout ai = Ui.card(this);
            row(ai, toggleLlm(), "A small AI model runs right here, so Aither keeps working offline.");
            row(ai, toggleNano(), NANO_DISCLOSURE);
            row(ai, Ui.action(this, "Use it from AitherOS in the browser", v -> {
                // a new token for the browser: the page stores it per origin, the old one stops working
                String t = cfg.rotateLlmToken();
                cfg.set("llm_paired", true);
                Uri u = Uri.parse("https://app.aitherium.com/#local-pair=" + t + "&port=" + LocalProxy.PORT);
                try { startActivity(new Intent(Intent.ACTION_VIEW, u).addCategory(Intent.CATEGORY_BROWSABLE)); }
                catch (RuntimeException e) { /* no browser */ }
            }), "Opens AitherOS in your browser, paired with this phone's model.");
            ai.addView(localStatus);
            row(ai, toggleVision(), "SmolVLM 500M (" + Vision.sizeMb() + ", Apache-2.0), downloaded"
                    + " when you ask about a picture. Off deletes it; pictures then go to Aither online.");
            row(ai, Ui.action(this, "Ask about a picture", v ->
                    startActivity(new Intent(this, DescribeActivity.class))),
                    "Take or choose a picture and ask what is in it.");
            row(ai, toggle("Lend memory to my models", "enabled", cfg.enabled()),
                    "Your own models may keep part of their memory on this phone.");
            row(ai, toggle("Only while charging", "only_charging", cfg.onlyCharging()),
                    "Lend only while plugged in, so the battery never drains.");
            row(ai, toggle("Only on Wi-Fi", "only_wifi", cfg.onlyWifi()),
                    "Never use mobile data for lending.");
            ai.addView(status);
            col.addView(ai);

            // ---- Help my fleet (WorkerService): this phone takes whole small jobs in parallel
            col.addView(Ui.section(this, "Help my fleet"));
            LinearLayout fleet = Ui.card(this);
            row(fleet, workerToggle("Help my fleet: run small jobs", "worker_on", cfg.workerOn()),
                    "Bonsai 1.7B on this phone answers small jobs for your AitherOS fleet "
                    + "(tagging, sorting, short summaries), next to your other devices. Reachable "
                    + "only over your private mesh (Tailscale), only with this phone's own key. The fleet "
                    + "uses it once the workspace owner has made this phone a worker (worker-on).");
            TextView par = Ui.action(this, parallelLabel(), null);
            par.setOnClickListener(v -> {
                cfg.set("worker_parallel", cfg.workerParallel() % Worker.MAX_PARALLEL + 1);
                par.setText(parallelLabel());
                WorkerService.sync(this);
            });
            row(fleet, par, "Tap to change (1 to 4). More jobs at once use more memory and battery.");
            row(fleet, workerToggle("Only when charging and on Wi-Fi", "worker_guard", cfg.workerGuard()),
                    "On by default. The model stops the moment the phone is unplugged or leaves Wi-Fi.");
            Switch gpu = Ui.style(new Switch(this));
            gpu.setText("Use the GPU");
            gpu.setChecked(false);
            gpu.setEnabled(false);
            row(fleet, gpu, "Not available yet: this version's on-phone engine runs on the CPU only.");
            workerStatus = dim();
            fleet.addView(workerStatus);
            col.addView(fleet);
        }

        // ---- Family
        col.addView(Ui.section(this, "Family"));
        LinearLayout famCard = Ui.card(this);
        Switch fam = Ui.style(new Switch(this));
        fam.setText("Share it with my family (only while charging)");
        fam.setChecked(cfg.shareFamily());
        if ("child".equals(cfg.profileKind())) {
            fam.setEnabled(false);
            fam.setText("Shared with the family when your guardian turns it on");
        }
        fam.setOnCheckedChangeListener((v, checked) -> {
            cfg.setShareLocally(checked);
            if (cfg.llmEnabled()) startForegroundService(new Intent(this, LlmService.class));
        });
        row(famCard, fam, lite ? "Your grown-up decides this."
                : "Your family's devices may use this phone's AI while it charges.");
        row(famCard, storageToggle(), "child".equals(cfg.profileKind())
                ? "Your grown-up decides this, and how much space it may use."
                : "Lends part of this phone's free space to your family's mesh, only on Wi-Fi "
                        + "while charging, never more than the amount you choose.");
        famCard.addView(storageStatus);
        // (an adult's "on duty for approvals" is the Alerts switch at the top)
        col.addView(famCard);

        // ---- About
        col.addView(Ui.section(this, "About"));
        LinearLayout about = Ui.card(this);
        // flag AI content or anything else (Play AI policy)
        row(about, Ui.action(this, "Report a problem or an AI answer", v -> Report.open(this, "", "Aither settings")),
                "Tell us what went wrong; it goes straight to the team.");
        about.addView(nodeStatus);
        col.addView(about);

        android.widget.ScrollView scroll = new android.widget.ScrollView(this);
        scroll.addView(col);
        LinearLayout screen = new LinearLayout(this);
        screen.setOrientation(LinearLayout.VERTICAL);
        screen.setBackgroundColor(Ui.BG);
        LinearLayout bar = Ui.bar(this);
        bar.addView(Ui.icon(this, R.drawable.ic_nav_back, "Back to Aither", v -> finish()));
        TextView title = Ui.barTitle(this);
        title.setText("Settings");
        bar.addView(title);
        screen.addView(bar);
        screen.addView(Ui.rule(this));
        screen.addView(scroll, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));
        setContentView(screen);
        Edge.fit(screen);
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
        syncAlertsSwitch(); // back from Android's notification or battery page
        h.post(tick);
    }

    /** Instant alerts are on: on duty AND Aither may post notifications. */
    private boolean alertsOn() {
        return cfg.approvalsOnDuty() && FamilyNotices.allowed(this);
    }

    private void syncAlertsSwitch() {
        if (alerts == null) return;
        alerts.setOnCheckedChangeListener(null);
        alerts.setChecked(alertsOn());
        alerts.setOnCheckedChangeListener((v, checked) -> setAlerts(checked));
    }

    /** One switch: on duty (DutyService), the notification permission, then the battery hint
     *  (deep sleep pauses the long-poll unless Aither may run with the screen off). */
    private void setAlerts(boolean on) {
        cfg.set("approvals_duty", on);
        DutyService.sync(this);
        if (!on) return;
        if (FamilyNotices.askAlerts(this, 4)) askBattery(); // else after Android's answer
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        super.onRequestPermissionsResult(code, perms, results);
        // READ_CALENDAR granted for the calendar switch: send Hearth the calendar now
        if (code == 2 && results.length > 0 && results[0] == 0) syncHearth(true);
        if (code == 4) {
            if (results.length > 0 && results[0] == 0) {
                DutyService.sync(this);
                askBattery();
            } else {
                android.widget.Toast.makeText(this, "Aither needs notifications to alert you. "
                        + "Tap the switch again to open Android's settings.", android.widget.Toast.LENGTH_LONG).show();
            }
        }
    }

    /** Settings > Alerts > "Send a test alert". */
    private void sendTest() {
        android.widget.Toast.makeText(this, "Sending a test alert…", android.widget.Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            int code = Notices.test(this);
            String say = code == 201 ? (FamilyNotices.allowed(this) ? "Sent. It should show in a moment."
                            : "Sent, but notifications are off for Aither: turn on Instant alerts.")
                    : code == 401 ? "Sign in to Aither first, then try again."
                    : code == 429 ? "That's enough tests for now. Try again in a few minutes."
                    : code == 0 ? "No connection. Try again." : "The test was not sent (" + code + ").";
            runOnUiThread(() -> android.widget.Toast.makeText(this, say, android.widget.Toast.LENGTH_LONG).show());
        }, "aither-alert-test").start();
    }

    @Override
    protected void onPause() {
        h.removeCallbacks(tick);
        super.onPause();
    }

    /** A control and the one plain line under it that says what it does. */
    private void row(LinearLayout card, View control, String explain) {
        card.addView(control);
        card.addView(Ui.note(this, explain));
    }

    /** A live status line (tick fills it). */
    private TextView dim() {
        TextView t = Ui.text(this, "", 12, Ui.FAINT);
        t.setPadding(0, Ui.dp(this, 2), 0, Ui.dp(this, 10));
        return t;
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

    /** Android's own dialog: the owner decides, and can undo it in Android's settings. */
    private void askAssistantRole() {
        android.app.role.RoleManager rm = getSystemService(android.app.role.RoleManager.class);
        if (rm == null || !rm.isRoleAvailable(android.app.role.RoleManager.ROLE_ASSISTANT)) return;
        if (rm.isRoleHeld(android.app.role.RoleManager.ROLE_ASSISTANT)) {
            android.widget.Toast.makeText(this, "Aither is already the assistant", android.widget.Toast.LENGTH_SHORT).show();
            return;
        }
        try {
            startActivityForResult(rm.createRequestRoleIntent(android.app.role.RoleManager.ROLE_ASSISTANT), 3);
        } catch (RuntimeException e) {
            // some phones only offer it in Settings > Apps > Default apps > Digital assistant app
            try { startActivity(new Intent(Settings.ACTION_VOICE_INPUT_SETTINGS)); } catch (RuntimeException ignored) { /* none */ }
        }
    }

    private void askBattery() {
        PowerManager pm = getSystemService(PowerManager.class);
        if (pm.isIgnoringBatteryOptimizations(getPackageName())) return;
        try { startActivity(Flavor.battery(this)); } catch (RuntimeException e) { /* settings app refused */ }
    }

    private View toggleLlm() {
        Switch s = Ui.style(new Switch(this));
        s.setText("Run Bonsai 1.7B here for AitherOS");
        s.setChecked(cfg.llmEnabled());
        s.setOnCheckedChangeListener((v, checked) -> {
            cfg.set("llm_enabled", checked);
            Intent svc = new Intent(this, LlmService.class);
            if (checked) startForegroundService(svc);
            else stopService(svc);
        });
        return s;
    }

    /** The picture model (Vision): on = may be offered and kept; off = deleted, not offered. */
    private View toggleVision() {
        Switch s = Ui.style(new Switch(this));
        s.setText("Look at pictures on this phone");
        s.setChecked(!cfg.visionDeclined());
        s.setOnCheckedChangeListener((v, checked) -> {
            cfg.set("vision_declined", !checked);
            if (!checked) new Thread(() -> VisionEngine.remove(this), "aither-vision-remove").start();
        });
        return s;
    }

    /** What turning Gemini Nano on means, in plain words, shown under its switch. */
    static final String NANO_DISCLOSURE =
            "Off unless you turn it on. Gemini Nano is Google's model built into Android; your questions "
            + "stay on this phone. Google's ML Kit, which Aither uses to reach it, sends Google usage "
            + "diagnostics (device, app, how long answers took). Bonsai stays the default.";

    /** "Use Gemini Nano on this phone": opt-in, never on a child's phone (NanoRoute's gate). */
    private View toggleNano() {
        Switch s = Ui.style(new Switch(this));
        s.setText("Use Gemini Nano on this phone");
        boolean built = GeminiNano.engine(this) != null;
        s.setChecked(built && cfg.nanoPreferred());
        s.setEnabled(built && cfg.localAiBlocked().isEmpty());
        if (!built) s.setText("Use Gemini Nano on this phone (not in this version)");
        s.setOnCheckedChangeListener((v, checked) -> cfg.set("nano_preferred", checked));
        return s;
    }

    /** Quotas offered when the owner turns storage sharing on (GiB). */
    static final int[] STORAGE_QUOTAS = {4, 8, 16, 32, 64};

    /**
     * "Share storage with my family's mesh": off by default. An adult picks how much
     * (a quota is required) and the household is told; a child's phone shows the switch
     * disabled, because only the guardian can turn it on (from Family in any browser).
     */
    private View storageToggle() {
        Switch s = Ui.style(new Switch(this));
        s.setText("Share storage with my family's mesh");
        s.setChecked(cfg.storageShare() && cfg.storageQuotaGb() > 0);
        if ("child".equals(cfg.profileKind())) {
            s.setEnabled(false);
            s.setText("Storage shared with the family when your guardian turns it on");
            return s;
        }
        s.setOnCheckedChangeListener((v, checked) -> {
            if (!checked) {
                sendStorage(s, false, 0);
                return;
            }
            String[] labels = new String[STORAGE_QUOTAS.length];
            for (int i = 0; i < labels.length; i++) labels[i] = "Up to " + STORAGE_QUOTAS[i] + " GB";
            new android.app.AlertDialog.Builder(this)
                    .setTitle("How much space may your family use?")
                    .setItems(labels, (d, which) -> sendStorage(s, true, STORAGE_QUOTAS[which]))
                    .setOnCancelListener(d -> {
                        s.setOnCheckedChangeListener(null);
                        s.setChecked(false);
                        recreate();
                    })
                    .show();
        });
        return s;
    }

    private void sendStorage(Switch s, boolean on, int quotaGb) {
        new Thread(() -> {
            String err = StorageShare.optIn(this, on, quotaGb);
            runOnUiThread(() -> {
                if (!err.isEmpty()) {
                    android.widget.Toast.makeText(this, err, android.widget.Toast.LENGTH_LONG).show();
                    s.setOnCheckedChangeListener(null);
                    s.setChecked(!on);
                    recreate();
                }
            });
        }, "aither-storage-share").start();
    }

    private String parallelLabel() {
        return "Parallel jobs: " + cfg.workerParallel();
    }

    /** A "Help my fleet" switch: store it, then let the service follow it. */
    private View workerToggle(String label, String key, boolean on) {
        Switch s = Ui.style(new Switch(this));
        s.setText(label);
        s.setChecked(on);
        s.setOnCheckedChangeListener((v, checked) -> {
            cfg.set(key, checked);
            WorkerService.sync(this);
        });
        return s;
    }

    private View toggle(String label, String key, boolean on) {
        Switch s = Ui.style(new Switch(this));
        s.setText(label);
        s.setChecked(on);
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
            if (ticks++ % 5 == 0) StorageShare.verdict(SettingsActivity.this); // disk + power, every 5 s
            storageStatus.setText("Family storage: " + StorageShare.state + " · " + StorageWorker.last);
            String blocked = cfg.localAiBlocked();
            localStatus.setText((blocked.isEmpty() ? LlmService.reason : "Off: " + blocked)
                    + "\nFamily sharing: " + FamilyShare.state
                    + "\nHousehold check-in: " + HeartbeatJob.last
                    + (cfg.profileKind().isEmpty() ? "" : " (" + cfg.profileKind() + ")")
                    + "\nApprovals: " + (cfg.approvalsOnDuty() ? DutyService.state : "each check-in · " + Notices.last));
            alertsStatus.setText(!cfg.approvalsOnDuty() ? "Off · each check-in · " + Notices.last
                    : !FamilyNotices.allowed(SettingsActivity.this) ? "Notifications are off for Aither: tap the switch"
                    : "On · " + DutyService.state);
            if (workerStatus != null) {
                workerStatus.setText("Fleet worker: " + (cfg.workerOn() ? WorkerService.state : "off"));
            }
            NodeLink n = new NodeLink(SettingsActivity.this);
            nodeStatus.setText("Version " + Config.VERSION
                    + "\nWorkspace node: " + (n.linked() ? n.nodeId() : "-") + " · " + NodeLink.last
                    + "\nUpdates: " + Updater.last);
            h.postDelayed(this, 1000);
        }
    };

    /** The calendar switch moved: push this phone's calendar to Hearth now, or forget it. */
    private void syncHearth(boolean on) {
        new Thread(() -> {
            if (on) HearthLink.push(this);
            else HearthLink.forget(this);
            refreshConnected();
        }, "aither-hearth-sources").start();
    }

    /** "Calendar: phone ✓ · Email: you@example.com ✓", from Hearth itself. */
    private void refreshConnected() {
        new Thread(() -> {
            String line = HearthLink.connectedLine(this);
            h.post(() -> {
                if (connected != null) connected.setText(line == null ? "Connected: not reachable right now" : line);
            });
        }, "aither-hearth-connected").start();
    }
}
