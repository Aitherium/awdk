package com.aitherium.aither;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** After a reboot or an update (including the watch's own over-the-air update): the
 *  background check-in (WearBeatJob), so the watch stays online without the app being opened. */
public class WearBootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context c, Intent i) {
        String a = i == null ? null : i.getAction();
        if (Intent.ACTION_BOOT_COMPLETED.equals(a) || Intent.ACTION_MY_PACKAGE_REPLACED.equals(a)) {
            WearBeatJob.schedule(c);
        }
    }
}
