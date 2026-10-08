package com.aitherium.aither;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** After a reboot or an update: the check-in, and whatever the owner left on. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context c, Intent i) {
        Config cfg = new Config(c);
        // a new version reloads AitherOS fresh: no cached page from the old build (old logo)
        if (Intent.ACTION_MY_PACKAGE_REPLACED.equals(i.getAction())) cfg.set("refresh_app", true);
        HeartbeatJob.schedule(c);
        if (cfg.enabled()) c.startForegroundService(new Intent(c, HolderService.class));
        if (cfg.llmEnabled()) c.startForegroundService(new Intent(c, LlmService.class));
        ShieldVpnService.sync(c); // Family Shield, when the household turned it on (arms its watch)
        DutyService.sync(c); // on duty for approvals, when this phone's guardian left it on
    }
}
