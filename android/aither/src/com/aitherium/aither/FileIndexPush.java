package com.aitherium.aither;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * The push half of "Index my files in Strata" (pure Java: the desktop JVM checks it).
 *
 * This phone's own Identity device token ({@link NodeLink#bearer}, scope endpoint:node) on
 * {@code POST https://api.aitherium.com/v1/storage/files/{node}}, the route awstorage's
 * NodeTokenClient uses: Genesis binds the write to the TOKEN's tenant and node. Refused here
 * before a byte leaves: a non-https base (the bearer would travel in clear), a push for any
 * node but the token's own, a body over Genesis's 8 MB cap. Redirects are not followed, so the
 * bearer is never replayed to another host.
 */
final class FileIndexPush {
    static final String API = "https://api.aitherium.com";
    static final String PATH = "/v1/storage/files/";
    /** Set by FileIndexJob to carry the app version (Config is Android-only). */
    static volatile String UA = "AitherAndroid file-index";

    /** What one part's POST answered. */
    static final class Reply {
        final int code;
        final String body;

        Reply(int code, String body) {
            this.code = code;
            this.body = body == null ? "" : body;
        }

        /** A top-level integer field of the answer (written, unchanged, rejected, pruned); 0 if absent. */
        long num(String key) {
            Matcher m = Pattern.compile("\"" + Pattern.quote(key) + "\"\\s*:\\s*(\\d+)").matcher(body);
            return m.find() ? Long.parseLong(m.group(1)) : 0;
        }

        /** Genesis's seq-gap answer: HTTP 409 {"detail": {"resync": true, ...}}. */
        boolean resync() {
            return code == 409;
        }
    }

    interface Transport {
        Reply post(String node, String body) throws IOException;
    }

    /** Genesis holds another seq for this root: resend it in full (once). */
    static final class ResyncNeeded extends IOException {
        ResyncNeeded(String m) { super(m); }
    }

    private final String base;
    private final String node;
    private final String token;

    FileIndexPush(String base, String node, String token) {
        String b = base == null ? "" : base.trim();
        while (b.endsWith("/")) b = b.substring(0, b.length() - 1);
        if (!b.regionMatches(true, 0, "https://", 0, 8) || b.length() <= 8) {
            throw new IllegalArgumentException("refusing to send this phone's token over " + b
                    + ": the API must be https");
        }
        if (node == null || node.isEmpty() || token == null || token.isEmpty()) {
            throw new IllegalArgumentException("the push needs this phone's node id and its token");
        }
        this.base = b;
        this.node = node;
        this.token = token;
    }

    String url(String forNode) {
        try {
            return base + PATH + URLEncoder.encode(forNode, "UTF-8").replace("+", "%20");
        } catch (java.io.UnsupportedEncodingException e) {
            throw new IllegalStateException(e);
        }
    }

    /** Refuses before any network: another node's push, or a body over the cap. */
    void guard(String forNode, String body) {
        if (!node.equals(forNode)) {
            throw new IllegalArgumentException("this token is for node " + node + "; refusing to push as " + forNode);
        }
        if (FileIndex.bytes(body) > FileIndex.MAX_PART_BYTES) {
            throw new IllegalArgumentException("a part of " + FileIndex.bytes(body) + " bytes is over Genesis's 8 MB cap");
        }
    }

    Reply post(String forNode, String body) throws IOException {
        guard(forNode, body);
        HttpURLConnection c = (HttpURLConnection) new URL(url(forNode)).openConnection();
        c.setInstanceFollowRedirects(false);
        c.setRequestMethod("POST");
        c.setConnectTimeout(20000);
        c.setReadTimeout(120000);
        c.setDoOutput(true);
        c.setRequestProperty("Authorization", "Bearer " + token);
        c.setRequestProperty("Content-Type", "application/json");
        c.setRequestProperty("Accept", "application/json");
        c.setRequestProperty("User-Agent", UA);
        byte[] out = body.getBytes(StandardCharsets.UTF_8);
        c.setFixedLengthStreamingMode(out.length);
        try (OutputStream o = c.getOutputStream()) {
            o.write(out);
        }
        int code = c.getResponseCode();
        InputStream in = code < 400 ? c.getInputStream() : c.getErrorStream();
        return new Reply(code, in == null ? "" : read(in));
    }

    private static String read(InputStream in) throws IOException {
        ByteArrayOutputStream b = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        try {
            while (b.size() < (1 << 20) && (n = in.read(buf)) > 0) b.write(buf, 0, n);
        } finally {
            in.close();
        }
        return b.toString("UTF-8");
    }

    /** What one root's push did. */
    static final class Sent {
        long rows, written, unchanged, rejected, pruned;
        int parts;
    }

    /**
     * Every part of one root, in order. ResyncNeeded on a 409 (the caller resends in full);
     * IOException on any other refusal, and on a push that sent rows and wrote none
     * (awstorage's "treated as a FAILED push").
     */
    static Sent pushRoot(Transport t, String node, String root, List<String> bodies, long rows) throws IOException {
        Sent s = new Sent();
        for (int i = 0; i < bodies.size(); i++) {
            Reply r = t.post(node, bodies.get(i));
            if (r.resync()) throw new ResyncNeeded(root + ": the fleet index is at another seq");
            if (r.code == 202) { // a large prune became a server-side job: accepted, never re-sent
                s.parts++;
                continue;
            }
            if (r.code < 200 || r.code >= 300) {
                String why = r.body.length() > 300 ? r.body.substring(0, 300) : r.body;
                throw new IOException("part " + (i + 1) + "/" + bodies.size() + " of " + root
                        + " answered " + r.code + (why.isEmpty() ? "" : ": " + why));
            }
            s.written += r.num("written");
            s.unchanged += r.num("unchanged");
            s.rejected += r.num("rejected");
            s.pruned += r.num("pruned");
            s.parts++;
        }
        s.rows = rows;
        if (rows > 0 && s.written + s.unchanged == 0 && s.pruned == 0) {
            throw new IOException(root + ": sent " + rows + " files, Genesis wrote none ("
                    + s.rejected + " rejected)");
        }
        return s;
    }
}
