package com.aitherium.aither;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInstaller;

/**
 * The outcome of a watch update session (WearUpdater). Success needs nothing: Android
 * restarts the app on its new version. "Pending user action" (the watch face, or a watch
 * that refuses the quiet self-update) keeps the confirmation for the next screen and
 * notifies; a background receiver never opens it itself.
 */
public class WearUpdateResult extends BroadcastReceiver {
    @Override
    public void onReceive(Context ctx, Intent i) {
        int st = i.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
        String what = i.getStringExtra("what");
        what = what == null ? "an update" : what;
        if (st == PackageInstaller.STATUS_SUCCESS) {
            WearUpdater.last = what + " installed · " + new java.util.Date();
            return;
        }
        if (st == PackageInstaller.STATUS_PENDING_USER_ACTION) {
            @SuppressWarnings("deprecation") // the typed overload is API 33; the watch app runs from 30
            Intent confirm = i.getParcelableExtra(Intent.EXTRA_INTENT);
            if (confirm != null) WearUpdater.needsTap(ctx, confirm, what);
            return;
        }
        String msg = i.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
        WearUpdater.last = what + " not installed" + (msg == null ? "" : ": " + msg) + " · " + new java.util.Date();
    }
}
