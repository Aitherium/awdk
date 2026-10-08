package com.aitherium.aither;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInstaller;

/**
 * The outcome of a quiet self-update. Success needs nothing here (BootReceiver sees
 * MY_PACKAGE_REPLACED and refreshes the app). Anything else, including Android wanting a
 * confirmation, falls back to the one-tap notification; a background receiver never
 * starts the confirmation screen itself.
 */
public class UpdateResult extends BroadcastReceiver {
    @Override
    public void onReceive(Context ctx, Intent i) {
        int st = i.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
        String version = i.getStringExtra("version");
        if (st == PackageInstaller.STATUS_SUCCESS) {
            Updater.last = (version == null ? "update" : version) + " installed · " + new java.util.Date();
            return;
        }
        String msg = i.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
        Updater.last = "quiet install " + (st == PackageInstaller.STATUS_PENDING_USER_ACTION
                ? "needs a tap" : "failed" + (msg == null ? "" : ": " + msg)) + " · " + new java.util.Date();
        new Updater(ctx).offer(version == null ? "An update" : version);
    }
}
