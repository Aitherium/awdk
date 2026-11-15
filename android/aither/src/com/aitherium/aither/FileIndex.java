package com.aitherium.aither;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.regex.Pattern;

/**
 * "Index my files in Strata": the pure half (no Android imports, so the desktop JVM checks it).
 *
 * The phone sends what a desktop's {@code awstorage files push} sends, to the same route
 * ({@code POST /v1/storage/files/{node}}): one JSON body per part, {scan_id, root, since_seq,
 * to_seq, parts, truncated, scanned_at, part, upserts, deletes, volumes}, each upsert row in
 * awstorage's {@code _wire_row} shape. Metadata only: path, size, mtime, type. No content is
 * read and nothing is hashed (a phone's battery), so the hash fields are null.
 *
 * Incremental by mtime: a delta carries the files modified since the last good walk (with a
 * slack), and continues the fleet's seq for that root. A full resync (since_seq 0) every
 * {@link #FULL_EVERY_MS}, on the first push, or when Genesis answers 409, is how deletions
 * and moved files (Android keeps a moved file's mtime) reach the index: Genesis prunes what a
 * complete, untruncated full resync did not carry.
 */
final class FileIndex {
    /** Genesis's per-body cap (routers/storage.py MAX_PART_BYTES). */
    static final int MAX_PART_BYTES = 8 * 1024 * 1024;
    /** What a part is cut at: awstorage's own margin under the cap (remote.py _CHUNK_SAFETY_MARGIN). */
    static final int PART_BYTES = MAX_PART_BYTES - 512 * 1024;
    /** Upserts per part (awstorage.files.MAX_PART_ROWS). */
    static final int MAX_PART_ROWS = 50_000;
    /** awstorage.files.MAX_PATH_CHARS / MAX_EXT_CHARS: Genesis refuses longer. */
    static final int MAX_PATH_CHARS = 4096;
    static final int MAX_EXT_CHARS = 32;
    /** A full resync this often, so deleted and moved files leave the index. */
    static final long FULL_EVERY_MS = 7L * 24 * 3600 * 1000;
    /** A delta also re-sends files modified this long before the last walk began. */
    static final long CUTOFF_SLACK_MS = 10 * 60 * 1000L;
    /** Files per granted folder; past it the walk stops and the push says truncated. */
    static final int MAX_FILES_PER_ROOT = 250_000;

    /** ExternalStorageProvider's tree authority: the only grants indexed (a cloud provider's
     *  "folder" would make the walk download a listing over the network, per directory). */
    static final String AUTHORITY = "com.android.externalstorage.documents";
    static final String PRIMARY = "/storage/emulated/0";
    private static final Pattern VOLUME = Pattern.compile("[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}|[0-9A-Fa-f]{16}");

    private FileIndex() {}

    /** One file the walk saw. mtimeMs and bytes are clamped to >= 0 (Genesis refuses negatives). */
    static final class Entry {
        final String path;
        final long bytes;
        final long mtimeMs;
        final String mime;

        Entry(String path, long bytes, long mtimeMs, String mime) {
            this.path = path;
            this.bytes = Math.max(0, bytes);
            this.mtimeMs = Math.max(0, mtimeMs);
            this.mime = mime == null || mime.isEmpty() ? null : mime;
        }
    }

    /**
     * The filesystem path of an ExternalStorageProvider document id, in awstorage's spelling:
     * {@code primary:DCIM/Camera} is {@code /storage/emulated/0/DCIM/Camera}, {@code 1A2B-3C4D:Music}
     * is {@code /storage/1A2B-3C4D/Music}, {@code home:x} is under Documents. Null for anything
     * else (another provider's id, a {@code ..} segment, a NUL, an over-long path).
     */
    static String pathOf(String docId) {
        if (docId == null) return null;
        int colon = docId.indexOf(':');
        if (colon <= 0) return null;
        String vol = docId.substring(0, colon), rel = docId.substring(colon + 1);
        String base;
        if (vol.equals("primary")) base = PRIMARY;
        else if (vol.equals("home")) base = PRIMARY + "/Documents";
        else if (VOLUME.matcher(vol).matches()) base = "/storage/" + vol;
        else return null;
        if (rel.indexOf('\0') >= 0 || rel.indexOf('\\') >= 0) return null;
        while (rel.startsWith("/")) rel = rel.substring(1);
        while (rel.endsWith("/")) rel = rel.substring(0, rel.length() - 1);
        for (String seg : rel.split("/", -1)) {
            if (seg.equals("..") || seg.equals(".") || (seg.isEmpty() && !rel.isEmpty())) return null;
        }
        String p = rel.isEmpty() ? base : base + "/" + rel;
        return p.length() > MAX_PATH_CHARS ? null : p;
    }

    /** True when {@code path} is {@code root} itself or below it (never a sibling prefix). */
    static boolean under(String path, String root) {
        return path != null && root != null && path.startsWith(root.endsWith("/") ? root : root + "/");
    }

    /** awstorage.files.ext_of: the lower-cased suffix after the last dot, "" for none or a dotfile. */
    static String ext(String path) {
        String base = path.substring(path.lastIndexOf('/') + 1);
        String stripped = base;
        while (stripped.startsWith(".")) stripped = stripped.substring(1);
        if (stripped.indexOf('.') < 0) return "";
        String e = base.substring(base.lastIndexOf('.') + 1).toLowerCase(Locale.ROOT);
        return e.length() > MAX_EXT_CHARS ? e.substring(0, MAX_EXT_CHARS) : e;
    }

    /** What this run sends for one root. */
    static final class Plan {
        final boolean full;
        final long sinceSeq;
        final long toSeq;
        /** Rows with mtimeMs > cutoffMs are sent (Long.MIN_VALUE: all of them). */
        final long cutoffMs;

        Plan(boolean full, long sinceSeq, long toSeq, long cutoffMs) {
            this.full = full;
            this.sinceSeq = sinceSeq;
            this.toSeq = toSeq;
            this.cutoffMs = cutoffMs;
        }
    }

    /**
     * Full or delta. {@code pushedSeq} is what Genesis acknowledged last for this root (0: never),
     * {@code lastFullMs} when the last full resync completed, {@code lastWalkMs} when the last
     * acknowledged walk began. to_seq only grows: the larger of pushedSeq + 1 and the clock in
     * seconds, so a reinstalled app (pushedSeq 0) still sends a seq past what it sent before.
     */
    static Plan plan(long pushedSeq, long lastFullMs, long lastWalkMs, long nowMs, boolean forceFull) {
        boolean full = forceFull || pushedSeq <= 0 || lastWalkMs <= 0 || lastFullMs <= 0
                || nowMs - lastFullMs >= FULL_EVERY_MS || nowMs < lastFullMs;
        long to = Math.max(Math.max(0, pushedSeq) + 1, nowMs / 1000);
        return new Plan(full, full ? 0 : pushedSeq, to, full ? Long.MIN_VALUE : lastWalkMs - CUTOFF_SLACK_MS);
    }

    /** The rows a plan sends, sorted by path (awstorage's part order). */
    static List<Entry> select(List<Entry> walked, Plan plan) {
        List<Entry> out = new ArrayList<>();
        for (Entry e : walked) {
            if (e.path == null || e.path.isEmpty() || e.path.length() > MAX_PATH_CHARS || e.path.indexOf('\0') >= 0) continue;
            if (plan.full || e.mtimeMs > plan.cutoffMs) out.add(e);
        }
        Collections.sort(out, (a, b) -> a.path.compareTo(b.path));
        return out;
    }

    /** One upsert in awstorage's _wire_row key order. */
    static String row(Entry e, long seq, String scannedAt) {
        StringBuilder b = new StringBuilder(160 + e.path.length());
        b.append("{\"path\":").append(str(e.path))
                .append(",\"bytes\":").append(e.bytes)
                .append(",\"mtime\":").append(seconds(e.mtimeMs))
                .append(",\"mtime_ns\":").append(e.mtimeMs * 1_000_000L)
                .append(",\"ext\":").append(str(ext(e.path)))
                .append(",\"mime\":").append(e.mime == null ? "null" : str(e.mime))
                .append(",\"partial_hash\":null,\"partial_algo\":null,\"sha256\":null")
                .append(",\"dev\":null,\"ino\":null,\"nlink\":null,\"git_root\":null")
                .append(",\"sensitive\":0,\"never\":0")
                .append(",\"seq\":").append(seq)
                .append(",\"scanned_at\":").append(scannedAt == null ? "null" : str(scannedAt))
                .append('}');
        return b.toString();
    }

    /**
     * The part bodies for one root, in awstorage's push_parts envelope. Every body is at most
     * {@code maxBytes} UTF-8 bytes (exact: the encoding is ASCII, non-ASCII escaped as Python's
     * json.dumps does) and carries at most {@code maxRows} upserts. Always at least one part:
     * an empty delta still tells Genesis the root is alive and moves its seq.
     */
    static List<String> parts(String scanId, String root, Plan plan, boolean truncated, String scannedAt,
                              List<Entry> rows, int maxBytes, int maxRows) {
        // pass 1: the cut points from each row's exact encoded size (rows are not kept encoded:
        // 250k encoded rows would be ~200 MB of chars on a phone)
        int overhead = envelope(scanId, root, plan, truncated, scannedAt, 100_000, 100_000, "").length();
        List<int[]> bounds = new ArrayList<>(); // [from, to) into rows
        int start = 0;
        long size = overhead;
        for (int i = 0; i < rows.size(); i++) {
            int len = row(rows.get(i), plan.toSeq, scannedAt).length();
            if (overhead + len > maxBytes) {
                throw new IllegalArgumentException("one row is larger than a part: " + rows.get(i).path);
            }
            int add = len + (i > start ? 1 : 0);
            if (i > start && (size + add > maxBytes || i - start >= maxRows)) {
                bounds.add(new int[] {start, i});
                start = i;
                size = overhead;
                add = len;
            }
            size += add;
        }
        if (start < rows.size()) bounds.add(new int[] {start, rows.size()});
        // pass 2, lazily: part i is encoded when it is sent, so one part is in memory at a time
        final int total = Math.max(1, bounds.size());
        return new java.util.AbstractList<String>() {
            @Override
            public String get(int p) {
                if (bounds.isEmpty()) {
                    if (p != 0) throw new IndexOutOfBoundsException();
                    return envelope(scanId, root, plan, truncated, scannedAt, 1, 1, "");
                }
                int[] r = bounds.get(p);
                StringBuilder b = new StringBuilder();
                for (int i = r[0]; i < r[1]; i++) {
                    if (i > r[0]) b.append(',');
                    b.append(row(rows.get(i), plan.toSeq, scannedAt));
                }
                return envelope(scanId, root, plan, truncated, scannedAt, p + 1, total, b.toString());
            }

            @Override
            public int size() {
                return total;
            }
        };
    }

    private static String envelope(String scanId, String root, Plan plan, boolean truncated, String scannedAt,
                                   int part, int parts, String upserts) {
        return "{\"scan_id\":" + str(scanId)
                + ",\"root\":" + str(root)
                + ",\"since_seq\":" + plan.sinceSeq
                + ",\"to_seq\":" + plan.toSeq
                + ",\"parts\":" + parts
                + ",\"truncated\":" + truncated
                + ",\"scanned_at\":" + (scannedAt == null ? "null" : str(scannedAt))
                + ",\"part\":" + part
                + ",\"upserts\":[" + upserts + "]"
                + ",\"deletes\":[],\"volumes\":[]}";
    }

    /** awstorage remote._scan_id: {node, [^A-Za-z0-9_.-] made '-', 32 max}.{unix s}.{8 hex}. */
    static String scanId(String node, long nowS, String hex8) {
        String safe = (node == null ? "" : node).replaceAll("[^A-Za-z0-9_.-]", "-");
        if (safe.length() > 32) safe = safe.substring(0, 32);
        if (safe.isEmpty()) safe = "node";
        return safe + "." + nowS + "." + hex8;
    }

    /** Milliseconds as a JSON number of seconds: 1700000000123 is 1700000000.123. */
    static String seconds(long ms) {
        long s = ms / 1000, frac = ms % 1000;
        if (frac == 0) return s + ".0";
        String f = String.format(Locale.ROOT, "%03d", frac).replaceAll("0+$", "");
        return s + "." + f;
    }

    /** A JSON string as Python's json.dumps writes it (ensure_ascii): ASCII out, always. */
    static String str(String s) {
        StringBuilder b = new StringBuilder(s.length() + 2).append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': b.append("\\\""); break;
                case '\\': b.append("\\\\"); break;
                case '\n': b.append("\\n"); break;
                case '\r': b.append("\\r"); break;
                case '\t': b.append("\\t"); break;
                case '\b': b.append("\\b"); break;
                case '\f': b.append("\\f"); break;
                default:
                    if (c < 0x20 || c > 0x7e) b.append(String.format(Locale.ROOT, "\\u%04x", (int) c));
                    else b.append(c);
            }
        }
        return b.append('"').toString();
    }

    /** UTF-8 size of a body (equal to its length: the encoding is ASCII). */
    static int bytes(String body) {
        return body.getBytes(StandardCharsets.UTF_8).length;
    }
}
