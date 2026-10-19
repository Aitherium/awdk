package com.aitherium.aither;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInstaller;

/**
 * The outcome of a quiet self-update. Every status is recorded (collect-diagnostics reads
 * it after the restart). Success needs nothing else (BootReceiver sees MY_PACKAGE_REPLACED
 * and brings the app's services back). Android wanting a confirmation is first retried
 * quietly past its silent-update throttle when UpdatePolicy says a silent install is
 * possible; only then does one notification carry Android's own confirmation, so the tap
 * lands on it directly. A background receiver never starts that screen itself.
 */
public class UpdateResult extends BroadcastReceiver {
    @Override
    public void onReceive(Context ctx, Intent i) {
        int st = i.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
        String version = i.getStringExtra("version");
        String msg = i.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
        Updater.recordStatus(ctx, version, st, msg, true);
        String v = version == null ? "update" : version;
        if (st == PackageInstaller.STATUS_SUCCESS) {
            Updater.remember(ctx, v + " installed quietly");
            return;
        }
        Updater u = new Updater(ctx);
        UpdatePolicy.Facts f = Updater.facts(ctx);
        UpdatePolicy.Next next = UpdatePolicy.after(st, msg, f, u.quietTries(version));
        if (next == UpdatePolicy.Next.DONE) {
            // superseded by a newer session of ours: that one reports for itself
            return;
        }
        String what = UpdatePolicy.statusName(st) + (msg == null || msg.isEmpty() ? "" : " (" + msg + ")");
        if (next == UpdatePolicy.Next.RETRY_QUIET) {
            UpdateJob.schedule(ctx, UpdatePolicy.RETRY_AFTER_MS);
            Updater.remember(ctx, "quiet install of " + v + " answered " + what + "; retrying quietly");
            return;
        }
        if (next == UpdatePolicy.Next.BLOCKED) {
            // no notification on a child's phone: the child cannot approve it, a parent can
            boolean child = new Config(ctx).childDevice();
            Updater.remember(ctx, v + " not installed: " + UpdatePolicy.blockedText(child) + " [" + what + "]");
            if (!child) u.offer(version == null ? "An update" : version, null);
            return;
        }
        Intent confirm = null;
        if (st == PackageInstaller.STATUS_PENDING_USER_ACTION) {
            @SuppressWarnings("deprecation") // the typed overload is API 33; this app runs from 29
            Intent c = i.getParcelableExtra(Intent.EXTRA_INTENT);
            confirm = c;
        }
        String why = UpdatePolicy.whyNotSilent(f);
        Updater.remember(ctx, "quiet install of " + v + " answered " + what + "; asking once"
                + (why.isEmpty() ? "" : " because " + why));
        u.offer(version == null ? "An update" : version, confirm);
    }
}
