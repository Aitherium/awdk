package com.aitherium.aither;

import android.app.job.JobInfo;
import android.app.job.JobParameters;
import android.app.job.JobScheduler;
import android.app.job.JobService;
import android.content.ComponentName;
import android.content.Context;
import android.webkit.CookieManager;

import org.json.JSONObject;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * The household check-in, in the background: so the owner's device list stays current when
 * AitherOS is not open on this phone. JobScheduler runs it about every 15 minutes when there
 * is a network, defers it in doze, and backs off exponentially on failure; no service stays
 * up for it.
 *
 * It sends what the AitherOS page itself sends (family's POST /api/tutor/me/device/heartbeat
 * with the household device token), with the page's own session cookie, both read from this
 * app's WebView storage. The answer's kv_lend and profile_kind are kept: profile_kind is how
 * this app knows a child's phone (Config.localAiBlocked).
 */
public class HeartbeatJob extends JobService {
    static final int JOB_ID = 4201;
    static final String API = "https://api.aitherium.com";
    static volatile String last = "never";

    static void schedule(Context c) {
        JobScheduler js = c.getSystemService(JobScheduler.class);
        JobInfo job = new JobInfo.Builder(JOB_ID, new ComponentName(c, HeartbeatJob.class))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setPeriodic(15 * 60 * 1000L, 5 * 60 * 1000L)
                .setBackoffCriteria(60 * 1000L, JobInfo.BACKOFF_POLICY_EXPONENTIAL)
                .setPersisted(true)
                .build();
        js.schedule(job);
    }

    @Override
    public boolean onStartJob(JobParameters params) {
        new Thread(() -> jobFinished(params, !checkIn(this)), "aither-heartbeat").start();
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters params) {
        return true; // retry with back-off
    }

    /** Everything the 15-minute check-in does. True when done (no retry needed). */
    static boolean checkIn(Context ctx) {
        boolean family = beat(ctx);
        FamilyNotices.poll(ctx);
        // approval cards: on duty, DutyService holds the inbox open; otherwise read it here
        if (new Config(ctx).approvalsOnDuty()) DutyService.sync(ctx);
        else Notices.poll(ctx, 0);
        // the family storage pool: run this phone's jobs while it may lend (Wi-Fi, charging,
        // within quota); before the Identity beat, so the beat reports what it holds now
        StorageWorker.drain(ctx, 3 * 60_000L);
        NodeLink node = new NodeLink(ctx);
        node.ensureLinked();
        boolean commands = node.checkIn();
        new Updater(ctx).maybeCheck();
        return family && commands;
    }

    /** The household check-in. Returns true when it is done (sent, or nothing to send). */
    static boolean beat(Context ctx) {
        Config cfg = new Config(ctx);
        String dev = cfg.familyDevice();
        if (dev.isEmpty()) {
            last = "not in a household yet";
            return true;
        }
        try {
            String token = new JSONObject(dev).optString("token", "");
            if (token.isEmpty()) return true;
            String cookie = CookieManager.getInstance().getCookie(API);
            HttpURLConnection c = (HttpURLConnection) new URL(API + "/api/tutor/me/device/heartbeat").openConnection();
            c.setRequestMethod("POST");
            c.setConnectTimeout(20000);
            c.setReadTimeout(30000);
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json");
            c.setRequestProperty("Origin", "https://aitherium.com");
            c.setRequestProperty("User-Agent", "Mozilla/5.0 (Linux; Android) Aither/0.2");
            if (cookie != null) c.setRequestProperty("Cookie", cookie);
            try (OutputStream o = c.getOutputStream()) {
                o.write(new JSONObject().put("token", token).toString().getBytes(StandardCharsets.UTF_8));
            }
            int code = c.getResponseCode();
            if (code == 410) { // revoked by the owner: forget the device, AitherOS signs out
                last = "removed from the household";
                cfg.set("profile_kind", "");
                cfg.set("family_device", "");
                cfg.set("shield_mode", "off");
                ShieldVpnService.sync(ctx);
                return true;
            }
            if (code == 401) {
                // Identity's word on the session decides, not this route's: during an outage
                // a 401 here once read as "signed out" while the session was fine.
                if (Session.check() == Session.State.OUT) {
                    last = "signed out: tap Sign in at the top of Aither";
                    return true; // nothing to retry until someone signs in
                }
                last = "check-in refused (401) with a good session; retrying";
                return false;
            }
            if (code >= 500) Session.note(Session.State.UNKNOWN); // the tabs reload once it answers
            if (code != 200) {
                last = "heartbeat answered " + code;
                return false;
            }
            StringBuilder sb = new StringBuilder();
            try (InputStream in = c.getInputStream()) {
                byte[] b = new byte[4096];
                int n;
                while ((n = in.read(b)) > 0) sb.append(new String(b, 0, n, StandardCharsets.UTF_8));
            }
            JSONObject j = new JSONObject(sb.toString());
            String kind = j.optString("profile_kind", "");
            if (!kind.isEmpty()) cfg.set("profile_kind", kind);
            cfg.set("family_kv_lend", j.optBoolean("kv_lend", false));
            // Family Shield: the guardian's mode for this phone starts or stops the filter
            cfg.set("shield_mode", j.optString("internet_mode", "off"));
            ShieldVpnService.sync(ctx);
            // the family storage pool: what the household lets this phone lend (StorageShare)
            JSONObject slim = j.optJSONObject("storage_limits");
            cfg.storageFromHousehold(j.optBoolean("storage_share", false),
                    slim == null ? 0 : slim.optInt("quota_gb", 0),
                    slim == null || slim.optBoolean("wifi_only", true),
                    slim == null || slim.optBoolean("charging_only", true));
            JSONObject lim = j.optJSONObject("share_limits");
            boolean was = cfg.shareFamily();
            cfg.shareFromHousehold(j.optBoolean("compute_share", false),
                    lim == null || lim.optBoolean("ac_only", true),
                    lim == null || lim.optBoolean("idle_only", true));
            if (cfg.shareFamily() != was && cfg.llmEnabled()) {
                // the share worker lives in the model service: start it (or let it stop itself)
                try {
                    ctx.startForegroundService(new android.content.Intent(ctx, LlmService.class));
                } catch (RuntimeException e) {
                    last = "sharing changed; it applies when Aither next runs";
                }
            }
            last = "sent " + new java.util.Date();
            return true;
        } catch (Exception e) {
            last = "heartbeat failed: " + e.getClass().getSimpleName();
            return false;
        }
    }
}
