package com.aitherium.aither;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;

/**
 * Desktop-JVM check of FileIndex (the walker's plan, cutoff, part cutting, wire rows) and
 * FileIndexPush (refusals before any network, the 409 resync, a push that wrote nothing).
 *
 *   java ... FileIndexCheck                 -> the checks, "OK" last
 *   java ... FileIndexCheck --emit ROWS.tsv SCAN_ID ROOT SINCE TO SCANNED_AT MAX_BYTES MAX_ROWS
 *                                           -> the part bodies, one per line (the pytest
 *                                              compares them with awstorage's push_parts and
 *                                              runs them through awstorage's ingest_push)
 */
public final class FileIndexCheck {
    private static int failed;

    private static void check(boolean ok, String what) {
        if (!ok) {
            failed++;
            System.out.println("FAIL " + what);
        }
    }

    public static void main(String[] args) throws Exception {
        if (args.length > 0 && args[0].equals("--emit")) {
            emit(args);
            return;
        }
        paths();
        plans();
        parts();
        push();
        System.out.println(failed == 0 ? "OK" : failed + " FAILED");
        if (failed > 0) System.exit(1);
    }

    private static void emit(String[] a) throws IOException {
        List<FileIndex.Entry> rows = new ArrayList<>();
        for (String line : Files.readAllLines(Paths.get(a[1]), StandardCharsets.UTF_8)) {
            if (line.isEmpty()) continue;
            String[] f = line.split("\t", -1);
            rows.add(new FileIndex.Entry(f[0], Long.parseLong(f[1]), Long.parseLong(f[2]), f[3]));
        }
        long since = Long.parseLong(a[4]), to = Long.parseLong(a[5]);
        FileIndex.Plan plan = new FileIndex.Plan(since == 0, since, to, Long.MIN_VALUE);
        List<String> bodies = FileIndex.parts(a[2], a[3], plan, false, a[6], FileIndex.select(rows, plan),
                Integer.parseInt(a[7]), Integer.parseInt(a[8]));
        java.io.PrintStream out = new java.io.PrintStream(System.out, true, "UTF-8");
        for (String b : bodies) out.println(b);
    }

    private static void paths() {
        check("/storage/emulated/0/DCIM/Camera".equals(FileIndex.pathOf("primary:DCIM/Camera")), "primary tree");
        check("/storage/emulated/0".equals(FileIndex.pathOf("primary:")), "primary root");
        check("/storage/1A2B-3C4D/Music/a.mp3".equals(FileIndex.pathOf("1A2B-3C4D:Music/a.mp3")), "sd card");
        check("/storage/emulated/0/Documents/x".equals(FileIndex.pathOf("home:x")), "home is Documents");
        check(FileIndex.pathOf("primary:DCIM/../Android") == null, "no .. segment");
        check(FileIndex.pathOf("raw:/data/x") == null && FileIndex.pathOf("msf:42") == null, "other providers refused");
        check(FileIndex.pathOf("nocolon") == null && FileIndex.pathOf(null) == null, "not an id");
        check(FileIndex.under("/storage/emulated/0/DCIM/a.jpg", "/storage/emulated/0/DCIM"), "under root");
        check(!FileIndex.under("/storage/emulated/0/DCIMX/a.jpg", "/storage/emulated/0/DCIM"), "sibling prefix is not under");
        check(FileIndex.ext("/a/b/Photo.JPG").equals("jpg"), "ext lower-cased");
        check(FileIndex.ext("/a/.bashrc").equals("") && FileIndex.ext("/a/README").equals(""), "dotfile / no ext");
        check(FileIndex.ext("/a/x.tar.gz").equals("gz"), "last suffix");
    }

    private static void plans() {
        long now = 1_800_000_000_000L, day = 24 * 3600 * 1000L;
        FileIndex.Plan p = FileIndex.plan(0, 0, 0, now, false);
        check(p.full && p.sinceSeq == 0 && p.cutoffMs == Long.MIN_VALUE, "first push is a full resync");
        check(p.toSeq == now / 1000, "to_seq from the clock on a fresh install");
        long seq = now / 1000 - 100;
        p = FileIndex.plan(seq, now - day, now - 6 * 3600_000L, now, false);
        check(!p.full && p.sinceSeq == seq && p.toSeq == now / 1000, "delta continues the acknowledged seq");
        check(p.cutoffMs == now - 6 * 3600_000L - FileIndex.CUTOFF_SLACK_MS, "cutoff = last walk - slack");
        p = FileIndex.plan(now, now - day, now - day, now, false); // seq already past the clock
        check(p.toSeq == now + 1, "to_seq only grows");
        p = FileIndex.plan(seq, now - 8 * day, now - day, now, false);
        check(p.full && p.sinceSeq == 0, "weekly full resync (deletes and moves)");
        p = FileIndex.plan(seq, now - day, now - day, now, true);
        check(p.full && p.sinceSeq == 0, "forced full after a 409");
        p = FileIndex.plan(seq, now + day, now - day, now, false);
        check(p.full, "a clock that went backwards resyncs");

        // incremental cutoff: only files modified after it, sorted, bad paths dropped
        List<FileIndex.Entry> walked = new ArrayList<>();
        long cut = now - 6 * 3600_000L;
        walked.add(new FileIndex.Entry("/storage/emulated/0/b.jpg", 10, cut + 1, "image/jpeg"));
        walked.add(new FileIndex.Entry("/storage/emulated/0/a.jpg", 10, cut + 5, "image/jpeg"));
        walked.add(new FileIndex.Entry("/storage/emulated/0/old.jpg", 10, cut - 1, "image/jpeg"));
        walked.add(new FileIndex.Entry("/storage/emulated/0/same.jpg", 10, cut, "image/jpeg"));
        walked.add(new FileIndex.Entry("/storage/emulated/0/nul\0.jpg", 10, now, null));
        FileIndex.Plan delta = new FileIndex.Plan(false, 5, 6, cut);
        List<FileIndex.Entry> sel = FileIndex.select(walked, delta);
        check(sel.size() == 2 && sel.get(0).path.endsWith("/a.jpg") && sel.get(1).path.endsWith("/b.jpg"),
                "delta = mtime > cutoff, sorted by path (got " + sel.size() + ")");
        check(FileIndex.select(walked, new FileIndex.Plan(true, 0, 6, Long.MIN_VALUE)).size() == 4,
                "full sends every good row");
        check(new FileIndex.Entry("/x", -5, -9, "").bytes == 0 && new FileIndex.Entry("/x", -5, -9, "").mtimeMs == 0
                && new FileIndex.Entry("/x", 1, 1, "").mime == null, "negatives clamped, empty mime is null");
    }

    private static void parts() {
        FileIndex.Plan plan = new FileIndex.Plan(true, 0, 77, Long.MIN_VALUE);
        String root = "/storage/emulated/0/DCIM";
        // empty: still one part
        List<String> b = FileIndex.parts("n.1.abcd", root, plan, false, "2026-11-07T00:00:00+00:00",
                new ArrayList<>(), FileIndex.PART_BYTES, FileIndex.MAX_PART_ROWS);
        check(b.size() == 1 && b.get(0).contains("\"part\":1") && b.get(0).contains("\"parts\":1")
                && b.get(0).contains("\"upserts\":[]"), "an empty push is one part");
        // 60k rows with ~300-char paths: split under the byte cap AND the row cap
        List<FileIndex.Entry> rows = new ArrayList<>();
        String pad = "x".repeat(260);
        for (int i = 0; i < 60_000; i++) {
            rows.add(new FileIndex.Entry(root + "/" + pad + String.format("%06d", i) + ".jpg", i, 1_700_000_000_000L + i,
                    "image/jpeg"));
        }
        b = FileIndex.parts("n.1.abcd", root, plan, false, "2026-11-07T00:00:00+00:00", rows,
                FileIndex.PART_BYTES, FileIndex.MAX_PART_ROWS);
        int total = 0;
        boolean underCap = true, numbered = true;
        for (int i = 0; i < b.size(); i++) {
            String s = b.get(i);
            underCap &= FileIndex.bytes(s) <= FileIndex.PART_BYTES;
            numbered &= s.contains("\"part\":" + (i + 1) + ",") && s.contains("\"parts\":" + b.size() + ",");
            total += count(s, "{\"path\":");
        }
        check(b.size() >= 3, "60k long rows need several parts (got " + b.size() + ")");
        check(underCap, "every part is under the 8 MB cap minus the margin");
        check(numbered, "parts are numbered 1..N and all say N");
        check(total == 60_000, "no row lost or duplicated across parts (" + total + ")");
        // the row cap alone
        List<FileIndex.Entry> small = new ArrayList<>();
        for (int i = 0; i < 25; i++) small.add(new FileIndex.Entry(root + "/f" + i, 1, 1, null));
        b = FileIndex.parts("n.1.abcd", root, plan, false, null, small, FileIndex.PART_BYTES, 10);
        check(b.size() == 3 && count(b.get(2), "{\"path\":") == 5, "max rows per part");
        // the byte cap alone, exactly
        b = FileIndex.parts("n.1.abcd", root, plan, false, null, small, 2000, 1000);
        boolean exact = true;
        for (String s : b) exact &= FileIndex.bytes(s) <= 2000;
        check(b.size() > 1 && exact, "a small byte cap is honoured exactly");
        // wire row: awstorage _wire_row keys, ASCII-only, the ns mtime exact
        String row = FileIndex.row(new FileIndex.Entry(root + "/été \"1\".JPG", 5, 1_700_000_000_123L, "image/jpeg"),
                77, "2026-11-07T00:00:00+00:00");
        check(row.startsWith("{\"path\":\"/storage/emulated/0/DCIM/\\u00e9t\\u00e9 \\\"1\\\".JPG\",\"bytes\":5,"
                + "\"mtime\":1700000000.123,\"mtime_ns\":1700000000123000000,\"ext\":\"jpg\",\"mime\":\"image/jpeg\","
                + "\"partial_hash\":null,\"partial_algo\":null,\"sha256\":null,\"dev\":null,\"ino\":null,"
                + "\"nlink\":null,\"git_root\":null,\"sensitive\":0,\"never\":0,\"seq\":77,"), "wire row: " + row);
        check(FileIndex.seconds(1_700_000_000_000L).equals("1700000000.0") && FileIndex.seconds(1500).equals("1.5"),
                "seconds like Python's float repr");
        check(FileIndex.scanId("fdev_ab cd", 1700000000L, "0011aabb").equals("fdev_ab-cd.1700000000.0011aabb"),
                "scan id like awstorage's");
    }

    private static void push() throws Exception {
        // refused before any network
        check(refuses(() -> new FileIndexPush("http://api.aitherium.com", "fdev_1", "t")), "plain http refused");
        check(refuses(() -> new FileIndexPush("ftp://x", "fdev_1", "t")), "not https refused");
        check(refuses(() -> new FileIndexPush("https://api.aitherium.com", "", "t")), "no node refused");
        check(refuses(() -> new FileIndexPush("https://api.aitherium.com", "fdev_1", "")), "no token refused");
        FileIndexPush c = new FileIndexPush("https://api.aitherium.com/", "fdev_1", "t");
        check(c.url("fdev_1").equals("https://api.aitherium.com/v1/storage/files/fdev_1"), "the route");
        check(refuses(() -> c.post("fdev_2", "{}")), "another node's push refused (token node mismatch)");
        check(refuses(() -> { c.guard("fdev_1", "x".repeat(FileIndex.MAX_PART_BYTES + 1)); return null; }), "a body over 8 MB refused");
        c.guard("fdev_1", "{}"); // its own node, a small body: allowed

        List<String> two = new ArrayList<>();
        two.add("{part1}");
        two.add("{part2}");
        // a good push sums the answers
        FileIndexPush.Sent s = FileIndexPush.pushRoot((n, body) -> new FileIndexPush.Reply(200,
                "{\"written\":3,\"unchanged\":2,\"deleted\":0,\"rejected\":1,\"errors\":[],\"pruned\":0}"),
                "fdev_1", "/r", two, 6);
        check(s.parts == 2 && s.written == 6 && s.unchanged == 4 && s.rejected == 2, "answers summed");
        // 409: resync
        boolean resync = false;
        try {
            FileIndexPush.pushRoot((n, body) -> new FileIndexPush.Reply(409, "{\"detail\":{\"resync\":true}}"),
                    "fdev_1", "/r", two, 6);
        } catch (FileIndexPush.ResyncNeeded e) {
            resync = true;
        }
        check(resync, "409 asks for a full resync");
        // 403 and a push that wrote nothing are failures, never silent
        check(fails(() -> FileIndexPush.pushRoot((n, body) -> new FileIndexPush.Reply(403, "{\"detail\":\"no\"}"),
                "fdev_1", "/r", two, 6)), "403 fails");
        check(fails(() -> FileIndexPush.pushRoot((n, body) -> new FileIndexPush.Reply(200,
                "{\"written\":0,\"unchanged\":0,\"rejected\":6}"), "fdev_1", "/r", two, 6)), "wrote none fails");
        FileIndexPush.Sent empty = FileIndexPush.pushRoot((n, body) -> new FileIndexPush.Reply(200,
                "{\"written\":0,\"unchanged\":0}"), "fdev_1", "/r", two.subList(0, 1), 0);
        check(empty.parts == 1, "an empty delta is fine");
    }

    interface Throwing {
        Object run() throws Exception;
    }

    private static boolean refuses(Throwing t) {
        try {
            t.run();
            return false;
        } catch (IllegalArgumentException e) {
            return true;
        } catch (Exception e) {
            return false; // a network attempt: not refused locally
        }
    }

    private static boolean fails(Throwing t) {
        try {
            t.run();
            return false;
        } catch (java.io.IOException e) {
            return !(e instanceof FileIndexPush.ResyncNeeded);
        } catch (Exception e) {
            return false;
        }
    }

    private static int count(String s, String needle) {
        int n = 0;
        for (int i = s.indexOf(needle); i >= 0; i = s.indexOf(needle, i + 1)) n++;
        return n;
    }
}
