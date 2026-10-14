package com.aitherium.aither;

import android.app.job.JobInfo;
import android.app.job.JobParameters;
import android.app.job.JobScheduler;
import android.app.job.JobService;
import android.content.ComponentName;
import android.content.Context;

/**
 * The watch's device check-in, in the background. Identity marks a node offline when it
 * stops checking in, and the watch used to check in only while the app or the Tile was in
 * use, so after half an hour on the wrist it read as offline in "my devices".
 *
 * JobScheduler runs it about every 15 minutes when there is a network (the phone's
 * HeartbeatJob does the same), defers it in doze and backs off on failure; no service stays
 * up for it. Scheduled when the app opens, when the watch joins, after a reboot and after an
 * update (WearBootReceiver), so an over-the-air update starts it without the app being opened.
 */
public class WearBeatJob extends JobService {
    static final int JOB_ID = 4301;
    static final long EVERY_MS = 15 * 60 * 1000L;

    static void schedule(Context c) {
        JobScheduler js = c.getSystemService(JobScheduler.class);
        // already pending: schedule() would replace it and restart its 15-minute clock
        if (js == null || js.getPendingJob(JOB_ID) != null) return;
        JobInfo job = new JobInfo.Builder(JOB_ID, new ComponentName(c, WearBeatJob.class))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setPeriodic(EVERY_MS, 5 * 60 * 1000L)
                .setBackoffCriteria(60 * 1000L, JobInfo.BACKOFF_POLICY_EXPONENTIAL)
                .setPersisted(true)
                .build();
        try {
            js.schedule(job);
        } catch (RuntimeException e) {
            // too many jobs or a missing declaration: the app and Tile still beat while used
            android.util.Log.w("AitherWearNode", "beat job not scheduled: " + e.getMessage());
        }
    }

    @Override
    public boolean onStartJob(JobParameters params) {
        new Thread(() -> jobFinished(params, !new WearNodeLink(this).beatNow()), "wear-beat-job").start();
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters params) {
        return true; // retry with back-off
    }
}
