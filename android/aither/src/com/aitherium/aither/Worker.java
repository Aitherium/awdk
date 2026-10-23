package com.aitherium.aither;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * The pure half of "Help my fleet" (WorkerService): this phone runs its own small model and
 * takes WHOLE small jobs from the fleet in parallel with the other devices -- data
 * parallelism, never a model split (a phone in a ggml-RPC split measured 850 ms/token).
 *
 * No Android imports, so a desktop JVM checks it (test/WorkerCheck.java). The rules:
 * <ul>
 * <li>The server binds ONLY a Tailscale address (100.64.0.0/10, {@link MeshLend#tailnet}):
 *     never 0.0.0.0, a LAN address or loopback.</li>
 * <li>It is never open: {@code --api-key} is {@link #apiKey}, derived one-way from this
 *     phone's command key, which only Identity and this phone hold. MicroScheduler reads the
 *     same key from Identity's internal worker-keys route (identity_node_commands.worker_api_key,
 *     adk.mesh_worker.worker_key); it never travels in a heartbeat or a command.</li>
 * <li>{@code -np} is the owner's "parallel jobs", 1..4, and nothing else.</li>
 * <li>It runs only while {@link #paused} says "": on, a grown-up's phone, on the mesh, not hot,
 *     and (by default) charging AND on Wi-Fi.</li>
 * </ul>
 */
final class Worker {
    static final int PORT = 8488;
    static final int MAX_PARALLEL = 4;
    static final int CTX_PER_SLOT = 2048;
    static final String KIND = "llama-worker";
    /** The closed worker-on arguments: the same tuples as Identity's VERBS["worker-on"]. */
    static final List<String> PARALLEL = Arrays.asList("1", "2", "3", "4");
    static final List<String> MODELS = Arrays.asList("bonsai-1.7b", "bonsai-4b", "bonsai-8b");
    /** The one model this phone ships (LlmService.MODEL_ID); worker-on naming another is refused. */
    static final String PHONE_MODEL = "bonsai-1.7b";
    static final String KEY_LABEL = "aither-worker-api-key/v1|";

    private Worker() { }

    /** Clamp the owner's setting to 1..4 (a stored value is never trusted blindly). */
    static int parallel(int n) {
        return Math.max(1, Math.min(MAX_PARALLEL, n));
    }

    /** Is {@code v} a worker-on argument value this phone accepts (strings only)? */
    static boolean argOk(String k, Object v) {
        if (!(v instanceof String)) return false;
        if ("parallel".equals(k)) return PARALLEL.contains(v);
        if ("model".equals(k)) return MODELS.contains(v);
        return false;
    }

    /** HMAC-SHA256(key = the command key's hex text, "aither-worker-api-key/v1|" + node id), hex. */
    static String apiKey(String commandKeyHex, String nodeId) {
        if (commandKeyHex == null || commandKeyHex.isEmpty() || nodeId == null || nodeId.isEmpty()) {
            return "";
        }
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(commandKeyHex.getBytes(StandardCharsets.US_ASCII), "HmacSHA256"));
            byte[] d = mac.doFinal((KEY_LABEL + nodeId).getBytes(StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder();
            for (byte b : d) sb.append(String.format("%02x", b & 0xff));
            return sb.toString();
        } catch (Exception e) {
            return "";
        }
    }

    /** The first 8 hex of the key's SHA-256: what a status line may show. Never the key. */
    static String fingerprint(String key) {
        try {
            byte[] d = java.security.MessageDigest.getInstance("SHA-256")
                    .digest(key.getBytes(StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < 4; i++) sb.append(String.format("%02x", d[i] & 0xff));
            return sb.toString();
        } catch (Exception e) {
            return "?";
        }
    }

    /**
     * The server's argv. Refuses a host off the tailnet and an empty key: a worker is never
     * reachable from the LAN and never open.
     */
    static List<String> argv(String exe, String model, String host, String key, int parallel,
                             int threads) {
        if (!MeshLend.tailnet(host)) throw new IllegalArgumentException("not a tailnet address: " + host);
        if (key == null || key.length() < 32) throw new IllegalArgumentException("no worker key");
        int np = parallel(parallel);
        return new ArrayList<>(Arrays.asList(exe, "-m", model, "--host", host,
                "--port", String.valueOf(PORT), "--api-key", key, "--alias", PHONE_MODEL,
                "-np", String.valueOf(np), "-c", String.valueOf(CTX_PER_SLOT * np),
                "-t", String.valueOf(Math.max(1, threads)), "-ub", "128", "--cache-ram", "0",
                "--no-webui", "--jinja"));
    }

    /** The argv with the key replaced: what a log or a status may show. */
    static List<String> redact(List<String> argv) {
        List<String> out = new ArrayList<>(argv);
        for (int i = 0; i + 1 < out.size(); i++) {
            if ("--api-key".equals(out.get(i))) out.set(i + 1, "<redacted>");
        }
        return out;
    }

    /**
     * Why the worker must not serve right now, or "" when it may. {@code guard} is the
     * owner's "only when charging and on Wi-Fi" (on by default).
     */
    static String paused(boolean on, boolean child, String meshIp, String key, boolean hot,
                         boolean guard, boolean charging, boolean wifi) {
        if (!on) return "off";
        if (child) return "not on a child's phone";
        if (key == null || key.isEmpty()) return "this phone is not one of your devices yet (no command key)";
        if (!MeshLend.tailnet(meshIp)) return "not on the mesh (Tailscale)";
        if (hot) return "the phone is hot";
        if (guard && !charging) return "waiting: not charging";
        if (guard && !wifi) return "waiting: not on Wi-Fi";
        return "";
    }
}
