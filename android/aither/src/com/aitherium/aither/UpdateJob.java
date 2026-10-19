package com.aitherium.aither;

import android.app.job.JobInfo;
import android.app.job.JobParameters;
import android.app.job.JobScheduler;
import android.app.job.JobService;
import android.content.ComponentName;
import android.content.Context;

/**
 * The quiet retry of a self-update, in the background: on an unmetered network, battery not
 * low, after Android's silent-update throttle has passed. It runs the same verified check
 * and install as everything else (Updater.check), so nothing unverified is ever installed.
 */
public class UpdateJob extends JobService {
    static final int JOB_ID = 4202;

    static void schedule(Context c, long afterMs) {
        JobScheduler js = c.getSystemService(JobScheduler.class);
        if (js == null) return;
        JobInfo job = new JobInfo.Builder(JOB_ID, new ComponentName(c, UpdateJob.class))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_UNMETERED)
                .setRequiresBatteryNotLow(true)
                .setMinimumLatency(afterMs)
                .setBackoffCriteria(5 * 60 * 1000L, JobInfo.BACKOFF_POLICY_EXPONENTIAL)
                .setPersisted(true)
                .build();
        js.schedule(job);
    }

    @Override
    public boolean onStartJob(JobParameters params) {
        new Thread(() -> jobFinished(params, !new Updater(this).check(true).ok), "aither-update").start();
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters params) {
        return true; // try again later
    }
}
