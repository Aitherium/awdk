package com.aitherium.kvholder;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** After a reboot or an update: start lending again if the owner left it on. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context c, Intent i) {
        if (new Config(c).enabled()) c.startForegroundService(new Intent(c, HolderService.class));
    }
}
