package com.aitherium.aither;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;

/**
 * 127.0.0.1:8486, the door the AitherOS page uses to reach the model on this phone.
 *
 * The contract (localapp, Veil local-runtimes): GET /health without a token; everything under
 * /v1/ needs "Authorization: Bearer <per-install token>"; CORS only for https://aitherium.com
 * and https://app.aitherium.com (any other Origin is 403, never echoed); a child's phone gets
 * 403 on /v1/. Requests are forwarded to llama-server on an internal loopback port that has
 * its own key, so nothing but this door can reach the model.
 */
final class LocalProxy {
    static final int PORT = 8486;
    static final String[] ORIGINS = {"https://aitherium.com", "https://app.aitherium.com"};

    interface Backend {
        /** Make sure the model is up; "" when ready, else why not (503). */
        String ensure();
        /** The same, for a Family AI Pool job: allowed on a phone whose household switched
         *  sharing on (a child's phone included), where the page's own use may be blocked. */
        String ensurePool();
        int port();
        String key();
        String modelId();
        boolean loaded();
        void touched();
    }

    private final Config cfg;
    private final Backend backend;
    private ServerSocket server;

    LocalProxy(Config cfg, Backend backend) {
        this.cfg = cfg;
        this.backend = backend;
    }

    void start() throws IOException {
        server = new ServerSocket(PORT, 16, InetAddress.getByName("127.0.0.1"));
        Thread t = new Thread(() -> {
            while (!server.isClosed()) {
                try {
                    Socket s = server.accept();
                    new Thread(() -> serve(s), "aither-local").start();
                } catch (IOException e) {
                    return;
                }
            }
        }, "aither-local-accept");
        t.setDaemon(true);
        t.start();
    }

    void stop() {
        try { if (server != null) server.close(); } catch (IOException e) { /* closed */ }
    }

    // ------------------------------------------------------------ one request

    private void serve(Socket s) {
        try (Socket sock = s) {
            sock.setSoTimeout(30000);
            InputStream in = sock.getInputStream();
            OutputStream out = sock.getOutputStream();
            String head = readHead(in);
            if (head == null) return;
            String[] lines = head.split("\r\n");
            String[] req = lines[0].split(" ");
            if (req.length < 2) return;
            String method = req[0], path = req[1];
            Map<String, String> h = headers(lines);
            String origin = h.get("origin");
            if (origin != null && !allowed(origin)) {
                reply(out, 403, null, "{\"error\":\"origin not allowed\"}");
                return;
            }
            if ("OPTIONS".equals(method)) {
                reply(out, 204, origin, null);
                return;
            }
            if (path.equals("/health")) {
                JSONObject j = new JSONObject()
                        .put("service", "aitherium-android-llm")
                        .put("model", backend.modelId())
                        .put("paired", cfg.llmPaired())
                        .put("child", "child".equals(cfg.profileKind()))
                        .put("loaded", backend.loaded());
                reply(out, 200, origin, j.toString());
                return;
            }
            if (!path.startsWith("/v1/")) {
                reply(out, 404, origin, "{\"error\":\"not found\"}");
                return;
            }
            String blocked = cfg.localAiBlocked();
            if (!blocked.isEmpty()) {
                reply(out, 403, origin, new JSONObject().put("error", blocked).toString());
                return;
            }
            if (!tokenOk(h.get("authorization"))) {
                reply(out, 401, origin, "{\"error\":\"missing or wrong token\"}");
                return;
            }
            byte[] body = new byte[0];
            String cl = h.get("content-length");
            if (cl != null) body = readN(in, Integer.parseInt(cl.trim()));
            String why = backend.ensure();
            if (!why.isEmpty()) {
                reply(out, 503, origin, new JSONObject().put("error", why).toString());
                return;
            }
            backend.touched();
            forward(method, path, h, body, out, origin);
            backend.touched();
        } catch (Exception e) {
            // the page sees a dropped request; nothing secret to log
        }
    }

    private boolean allowed(String origin) {
        for (String o : ORIGINS) if (o.equals(origin)) return true;
        return false;
    }

    private boolean tokenOk(String auth) {
        if (auth == null || !auth.startsWith("Bearer ")) return false;
        byte[] a = auth.substring(7).trim().getBytes(StandardCharsets.UTF_8);
        byte[] b = cfg.llmToken().getBytes(StandardCharsets.UTF_8);
        return MessageDigest.isEqual(a, b);
    }

    /** Pass the request to llama-server and stream its answer back, with our CORS headers. */
    private void forward(String method, String path, Map<String, String> h, byte[] body,
                         OutputStream out, String origin) throws IOException {
        try (Socket up = new Socket(InetAddress.getByName("127.0.0.1"), backend.port())) {
            up.setSoTimeout(600000);
            StringBuilder r = new StringBuilder();
            r.append(method).append(' ').append(path).append(" HTTP/1.1\r\n");
            r.append("Host: 127.0.0.1:").append(backend.port()).append("\r\n");
            r.append("Authorization: Bearer ").append(backend.key()).append("\r\n");
            r.append("Content-Type: ").append(h.getOrDefault("content-type", "application/json")).append("\r\n");
            r.append("Accept: ").append(h.getOrDefault("accept", "*/*")).append("\r\n");
            r.append("Content-Length: ").append(body.length).append("\r\n");
            r.append("Connection: close\r\n\r\n");
            OutputStream uo = up.getOutputStream();
            uo.write(r.toString().getBytes(StandardCharsets.ISO_8859_1));
            uo.write(body);
            uo.flush();
            InputStream ui = up.getInputStream();
            String head = readHead(ui);
            if (head == null) throw new IOException("no answer");
            String[] lines = head.split("\r\n");
            StringBuilder o = new StringBuilder(lines[0]).append("\r\n");
            for (int i = 1; i < lines.length; i++) {
                String l = lines[i].toLowerCase(Locale.ROOT);
                if (l.startsWith("access-control-") || l.startsWith("connection:")
                        || l.startsWith("keep-alive")) continue;
                o.append(lines[i]).append("\r\n");
            }
            o.append(cors(origin)).append("Connection: close\r\n\r\n");
            out.write(o.toString().getBytes(StandardCharsets.ISO_8859_1));
            byte[] buf = new byte[8192];
            int n;
            while ((n = ui.read(buf)) > 0) {
                out.write(buf, 0, n);
                out.flush(); // stream:true -- every SSE line reaches the page as it is made
                backend.touched();
            }
        }
    }

    private static String cors(String origin) {
        if (origin == null) return "";
        return "Access-Control-Allow-Origin: " + origin + "\r\n"
                + "Vary: Origin\r\n"
                + "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
                + "Access-Control-Allow-Headers: Authorization, Content-Type\r\n"
                + "Access-Control-Allow-Private-Network: true\r\n"
                + "Access-Control-Max-Age: 600\r\n";
    }

    private static void reply(OutputStream out, int code, String origin, String json) throws IOException {
        byte[] b = json == null ? new byte[0] : json.getBytes(StandardCharsets.UTF_8);
        String reason = code == 200 ? "OK" : code == 204 ? "No Content" : code == 401 ? "Unauthorized"
                : code == 403 ? "Forbidden" : code == 404 ? "Not Found" : "Service Unavailable";
        String head = "HTTP/1.1 " + code + " " + reason + "\r\n"
                + (json == null ? "" : "Content-Type: application/json\r\n")
                + "Content-Length: " + b.length + "\r\n"
                + "Cache-Control: no-store\r\n" + cors(origin) + "Connection: close\r\n\r\n";
        out.write(head.getBytes(StandardCharsets.ISO_8859_1));
        out.write(b);
        out.flush();
    }

    private static String readHead(InputStream in) throws IOException {
        ByteArrayOutputStream b = new ByteArrayOutputStream();
        int c, state = 0;
        while ((c = in.read()) >= 0) {
            b.write(c);
            if (b.size() > 65536) return null;
            state = (c == '\r' && (state == 0 || state == 2)) ? state + 1
                    : (c == '\n' && (state == 1 || state == 3)) ? state + 1 : 0;
            if (state == 4) return b.toString("ISO-8859-1");
        }
        return null;
    }

    private static Map<String, String> headers(String[] lines) {
        Map<String, String> h = new HashMap<>();
        for (int i = 1; i < lines.length; i++) {
            int k = lines[i].indexOf(':');
            if (k > 0) h.put(lines[i].substring(0, k).trim().toLowerCase(Locale.ROOT), lines[i].substring(k + 1).trim());
        }
        return h;
    }

    private static byte[] readN(InputStream in, int n) throws IOException {
        if (n < 0 || n > 4 << 20) throw new IOException("body too big");
        byte[] b = new byte[n];
        int off = 0;
        while (off < n) {
            int r = in.read(b, off, n - off);
            if (r < 0) throw new IOException("short body");
            off += r;
        }
        return b;
    }
}
