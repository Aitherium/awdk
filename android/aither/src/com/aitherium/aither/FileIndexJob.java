package com.aitherium.aither;

import android.app.job.JobInfo;
import android.app.job.JobParameters;
import android.app.job.JobScheduler;
import android.app.job.JobService;
import android.content.ComponentName;
import android.content.Context;
import android.content.SharedPreferences;
import android.content.UriPermission;
import android.database.Cursor;
import android.net.Uri;
import android.provider.DocumentsContract;

import java.security.SecureRandom;
import java.text.SimpleDateFormat;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.TimeZone;

/**
 * "Index my files in Strata" in the background: every 6 hours, only while charging on an
 * unmetered network (JobScheduler holds it until both are true), it walks the folders the
 * owner granted (Storage Access Framework tree grants, nothing else) for path, name, size and
 * mtime, and pushes the changes to Strata as this phone's own node ({@link FileIndexPush}).
 *
 * Off by default, and nothing is read without the owner's pick of a folder in Android's own
 * picker. Never on a child's phone. A failure is never silent: the job asks to be rescheduled
 * (back-off) and {@link #status} says what went wrong under the switch in Settings.
 */
public class FileIndexJob extends JobService {
    static final int JOB_ID = 4204;       // periodic
    static final int JOB_ID_NOW = 4205;   // one run soon after the switch is turned on
    static final long PERIOD_MS = 6 * 3600 * 1000L;
    static final String PREFS = "file_index";

    private volatile boolean stopped;

    /** The owner's switch, applied: scheduled while on with a grant, cancelled otherwise. */
    static void sync(Context c) {
        JobScheduler js = c.getSystemService(JobScheduler.class);
        if (js == null) return;
        if (!enabled(c)) {
            js.cancel(JOB_ID);
            js.cancel(JOB_ID_NOW);
            return;
        }
        ComponentName me = new ComponentName(c, FileIndexJob.class);
        js.schedule(new JobInfo.Builder(JOB_ID, me)
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_UNMETERED)
                .setRequiresCharging(true)
                .setPeriodic(PERIOD_MS, 60 * 60 * 1000L)
                .setBackoffCriteria(10 * 60 * 1000L, JobInfo.BACKOFF_POLICY_EXPONENTIAL)
                .setPersisted(true)
                .build());
    }

    /** One run as soon as the phone is charging on Wi-Fi (after a folder was granted). */
    static void runSoon(Context c) {
        JobScheduler js = c.getSystemService(JobScheduler.class);
        if (js == null || !enabled(c)) return;
        js.schedule(new JobInfo.Builder(JOB_ID_NOW, new ComponentName(c, FileIndexJob.class))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_UNMETERED)
                .setRequiresCharging(true)
                .setBackoffCriteria(10 * 60 * 1000L, JobInfo.BACKOFF_POLICY_EXPONENTIAL)
                .setPersisted(true)
                .build());
    }

    static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    /** On = the owner's switch AND at least one granted folder AND not a child's phone. */
    static boolean enabled(Context c) {
        return prefs(c).getBoolean("on", false) && !grants(c).isEmpty() && !new Config(c).childDevice();
    }

    /** The folder grants this feature may walk: read grants on ExternalStorageProvider trees. */
    static List<Uri> grants(Context c) {
        List<Uri> out = new ArrayList<>();
        for (UriPermission p : c.getContentResolver().getPersistedUriPermissions()) {
            Uri u = p.getUri();
            if (p.isReadPermission() && FileIndex.AUTHORITY.equals(u.getAuthority())
                    && DocumentsContract.isTreeUri(u)) out.add(u);
        }
        return out;
    }

    /** The owner's switch moved. Off forgets every folder grant and the push state. */
    static void setOn(Context c, boolean on) {
        if (!on) {
            for (Uri u : grants(c)) {
                try {
                    c.getContentResolver().releasePersistableUriPermission(u,
                            android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION);
                } catch (SecurityException e) { /* already gone */ }
            }
            prefs(c).edit().clear().putBoolean("on", false).putString("status", "off").apply();
        } else {
            prefs(c).edit().putBoolean("on", true).apply();
        }
        sync(c);
    }

    /** The last sync, for Settings: "ok ..." / "failed ..." / "off" / what it waits for. */
    static String status(Context c) {
        SharedPreferences p = prefs(c);
        if (!p.getBoolean("on", false)) return "off";
        if (grants(c).isEmpty()) return "no folder granted: tap Add a folder";
        String s = p.getString("status", "");
        return s.isEmpty() ? "waiting to run (needs charging + Wi-Fi)" : s;
    }

    @Override
    public boolean onStartJob(JobParameters params) {
        stopped = false;
        new Thread(() -> {
            String err = run(this);
            jobFinished(params, !err.isEmpty() && !stopped);
        }, "aither-file-index").start();
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters params) {
        stopped = true; // charger pulled, Wi-Fi lost, or time's up: the walk stops, retried later
        return true;
    }

    /** One sync of every granted folder. "" when every root was acknowledged, else why not. */
    String run(Context ctx) {
        SharedPreferences p = prefs(ctx);
        String err = sync1(ctx, p);
        String at = new SimpleDateFormat("MMM d HH:mm", Locale.ROOT).format(new Date());
        if (err.isEmpty()) {
            p.edit().putInt("fails", 0).putLong("last_ok_ms", System.currentTimeMillis()).apply();
        } else {
            int fails = p.getInt("fails", 0) + 1;
            p.edit().putInt("fails", fails)
                    .putString("status", "failed " + at + " (" + fails + " in a row): " + err).apply();
        }
        return err;
    }

    private String sync1(Context ctx, SharedPreferences p) {
        if (!p.getBoolean("on", false)) return "";
        if (new Config(ctx).childDevice()) return "not on a child's phone";
        NodeLink link = new NodeLink(ctx);
        link.ensureLinked();
        if (!link.linked()) return "this phone is not linked to your workspace yet (" + NodeLink.last + ")";
        List<Uri> trees = grants(ctx);
        if (trees.isEmpty()) return "no folder granted";
        FileIndexPush.UA = "AitherAndroid/" + Config.VERSION + " file-index";
        FileIndexPush client;
        try {
            client = new FileIndexPush(FileIndexPush.API, link.nodeId(), link.bearer());
        } catch (IllegalArgumentException e) {
            return e.getMessage();
        }
        long files = 0, written = 0, rejected = 0;
        int roots = 0;
        for (Uri tree : trees) {
            if (stopped) return "stopped before it finished (charger or Wi-Fi went away)";
            String root = FileIndex.pathOf(DocumentsContract.getTreeDocumentId(tree));
            if (root == null) continue;
            try {
                FileIndexPush.Sent s = pushTree(ctx, p, client, link.nodeId(), tree, root, false);
                files += s.rows;
                written += s.written;
                rejected += s.rejected;
                roots++;
            } catch (FileIndexPush.ResyncNeeded e) {
                return root + ": Genesis refused a full resync";
            } catch (Exception e) {
                return root + ": " + (e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage());
            }
        }
        String at = new SimpleDateFormat("MMM d HH:mm", Locale.ROOT).format(new Date());
        p.edit().putString("status", "ok " + at + " · " + roots + " folder" + (roots == 1 ? "" : "s")
                + " · " + files + " sent · " + written + " new or changed"
                + (rejected > 0 ? " · " + rejected + " refused" : "")).apply();
        return "";
    }

    /** One root: plan, walk, cut into parts, push; a 409 resends it in full, once. */
    private FileIndexPush.Sent pushTree(Context ctx, SharedPreferences p, FileIndexPush client, String node,
                                        Uri tree, String root, boolean forceFull) throws Exception {
        long now = System.currentTimeMillis();
        FileIndex.Plan plan = FileIndex.plan(p.getLong("seq:" + root, 0), p.getLong("full:" + root, 0),
                p.getLong("walk:" + root, 0), now, forceFull);
        boolean[] truncated = {false};
        List<FileIndex.Entry> walked = walk(ctx, tree, root, plan, truncated);
        if (stopped) throw new java.io.IOException("stopped before it finished");
        List<FileIndex.Entry> rows = FileIndex.select(walked, plan);
        byte[] r = new byte[4];
        new SecureRandom().nextBytes(r);
        String hex = String.format(Locale.ROOT, "%02x%02x%02x%02x", r[0], r[1], r[2], r[3]);
        String scannedAt = iso(now);
        List<String> bodies = FileIndex.parts(FileIndex.scanId(node, now / 1000, hex), root, plan,
                truncated[0], scannedAt, rows, FileIndex.PART_BYTES, FileIndex.MAX_PART_ROWS);
        FileIndexPush.Sent s;
        try {
            s = FileIndexPush.pushRoot(client::post, node, root, bodies, rows.size());
        } catch (FileIndexPush.ResyncNeeded e) {
            if (plan.full) throw e;
            return pushTree(ctx, p, client, node, tree, root, true);
        }
        SharedPreferences.Editor ed = p.edit().putLong("seq:" + root, plan.toSeq).putLong("walk:" + root, now);
        if (plan.full) ed.putLong("full:" + root, now);
        ed.apply();
        return s;
    }

    /** Breadth-first over the granted tree: metadata columns only, never a file's content. */
    private List<FileIndex.Entry> walk(Context ctx, Uri tree, String root, FileIndex.Plan plan, boolean[] truncated) {
        List<FileIndex.Entry> out = new ArrayList<>();
        int seen = 0;
        String[] cols = {
            DocumentsContract.Document.COLUMN_DOCUMENT_ID,
            DocumentsContract.Document.COLUMN_DISPLAY_NAME,
            DocumentsContract.Document.COLUMN_MIME_TYPE,
            DocumentsContract.Document.COLUMN_SIZE,
            DocumentsContract.Document.COLUMN_LAST_MODIFIED,
        };
        ArrayDeque<String> dirs = new ArrayDeque<>();
        dirs.add(DocumentsContract.getTreeDocumentId(tree));
        while (!dirs.isEmpty() && !stopped) {
            Uri kids = DocumentsContract.buildChildDocumentsUriUsingTree(tree, dirs.poll());
            try (Cursor c = ctx.getContentResolver().query(kids, cols, null, null, null)) {
                if (c == null) continue;
                while (c.moveToNext()) {
                    String id = c.getString(0), name = c.getString(1), mime = c.getString(2);
                    if (DocumentsContract.Document.MIME_TYPE_DIR.equals(mime)) {
                        // hidden folders (.thumbnails, .trashed-*, app caches) are not the owner's files
                        if (name != null && !name.startsWith(".")) dirs.add(id);
                        continue;
                    }
                    String path = FileIndex.pathOf(id);
                    if (!FileIndex.under(path, root)) continue;
                    if (++seen > FileIndex.MAX_FILES_PER_ROOT) {
                        truncated[0] = true;
                        return out;
                    }
                    long mtime = c.isNull(4) ? 0 : c.getLong(4);
                    // a delta keeps only what changed since the cutoff (memory, on a phone)
                    if (!plan.full && mtime <= plan.cutoffMs) continue;
                    out.add(new FileIndex.Entry(path, c.isNull(3) ? 0 : c.getLong(3), mtime, mime));
                }
            } catch (RuntimeException e) {
                truncated[0] = true; // a folder that would not list: never prune what it holds
            }
        }
        if (stopped) truncated[0] = true;
        return out;
    }

    static String iso(long ms) {
        SimpleDateFormat f = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'+00:00'", Locale.ROOT);
        f.setTimeZone(TimeZone.getTimeZone("UTC"));
        return f.format(new Date(ms));
    }
}
