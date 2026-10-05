package com.aitherium.aither;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** After a reboot or an update: the check-in, and whatever the owner left on. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context c, Intent i) {
        Config cfg = new Config(c);
        HeartbeatJob.schedule(c);
        if (cfg.enabled()) c.startForegroundService(new Intent(c, HolderService.class));
        if (cfg.llmEnabled()) c.startForegroundService(new Intent(c, LlmService.class));
        ShieldVpnService.sync(c); // Family Shield, when the household turned it on
    }
}
