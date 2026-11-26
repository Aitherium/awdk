package com.aitherium.aither;

import java.net.URI;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Pure-Java rules for AitherMesh voice & posture routing, shared by both the phone app
 * (awdk/android/aither) and the Wear OS watch app (awdk/android/aither-wear, via build.py
 * WEAR_SHARED). No android.* imports so test/MeshRemoteCheck.java runs it on a desktop JVM.
 *
 * Security boundary:
 * - Only HTTPS URLs on private mesh/LAN hosts (Tailscale 100.64.0.0/10, RFC1918 LAN,
 *   .ts.net, .local) are accepted as mesh voice hubs; public hosts are refused so self-signed
 *   mesh TLS never weakens public internet / aitherium.com connections.
 * - Target nodes and model postures are closed allowlists.
 */
final class MeshRemote {
    static final String DEFAULT_PRIMARY_HUB = "https://100.64.0.38:9448";
    static final String DEFAULT_FALLBACK_HUB = "https://100.64.0.46:9449";

    /** Target ids for mesh routing: {id, label}. */
    static final String[][] NODES = {
        {"auto", "Auto (mesh)"},
        {"dgx-spark", "DGX Spark"},
        {"pc", "PC 5090"},
        {"optiplex", "OptiPlex"},
    };

    /** Supported model postures on AitherMesh nodes: {id, label}. */
    static final String[][] POSTURES = {
        {"fleet-multi", "Fleet Multi"},
        {"heavy-moe", "Heavy MoE (284B)"},
        {"creator", "Creator"},
        {"quiet", "Quiet"},
    };

    private static final Pattern TOKEN_RE = Pattern.compile("[A-Za-z0-9._-]{12,128}");

    private MeshRemote() {}

    /** Parsed QR / deep-link pairing parameters. */
    static final class Pair {
        final String hub;
        final String token;
        final String node;

        Pair(String hub, String token, String node) {
            this.hub = hub;
            this.token = token;
            this.node = node;
        }
    }

    /** Voice routing hint extracted from a spoken/typed prompt. */
    static final class IntentRoute {
        final String node;     // "" when unchanged
        final String posture;  // "" unless an explicit posture switch command
        final boolean meshRelevant;

        IntentRoute(String node, String posture, boolean meshRelevant) {
            this.node = node;
            this.posture = posture;
            this.meshRelevant = meshRelevant;
        }
    }

    static boolean validNode(String id) {
        if (id == null) return false;
        for (String[] n : NODES) if (n[0].equals(id)) return true;
        return false;
    }

    static String normalizeNode(String id, String fallback) {
        if (id == null) return fallback;
        String s = id.trim().toLowerCase(Locale.ROOT);
        if (validNode(s)) return s;
        if ("spark".equals(s) || "dgx".equals(s)) return "dgx-spark";
        if ("5090".equals(s) || "desktop".equals(s)) return "pc";
        return fallback;
    }

    static String nodeLabel(String id) {
        if ("phone".equals(id)) return "This phone";
        if ("cloud".equals(id)) return "Cloud (anywhere)";
        for (String[] n : NODES) if (n[0].equals(id)) return n[1];
        return "Auto (mesh)";
    }

    static boolean validPosture(String id) {
        if (id == null) return false;
        for (String[] p : POSTURES) if (p[0].equals(id)) return true;
        return false;
    }

    static String postureLabel(String id) {
        for (String[] p : POSTURES) if (p[0].equals(id)) return p[1];
        return id == null ? "" : id;
    }

    static boolean validToken(String token) {
        return token != null && TOKEN_RE.matcher(token.trim()).matches();
    }

    /** True ONLY for Tailscale 100.64.0.0/10, RFC1918 private IPv4, .ts.net, or .local hosts. */
    static boolean isPrivateMeshHost(String host) {
        if (host == null) return false;
        String h = host.trim().toLowerCase(Locale.ROOT);
        if (h.isEmpty() || h.contains("/") || h.contains("@")) return false;
        if (h.endsWith(".ts.net") || h.endsWith(".local")) {
            return h.matches("[a-z0-9][a-z0-9.-]{1,120}");
        }
        String[] parts = h.split("\\.", -1);
        if (parts.length != 4) return false;
        int[] o = new int[4];
        for (int i = 0; i < 4; i++) {
            if (parts[i].isEmpty() || parts[i].length() > 3) return false;
            for (int j = 0; j < parts[i].length(); j++) {
                char c = parts[i].charAt(j);
                if (c < '0' || c > '9') return false;
            }
            o[i] = Integer.parseInt(parts[i]);
            if (o[i] > 255) return false;
        }
        if (o[0] == 100 && o[1] >= 64 && o[1] <= 127) return true; // Tailscale CGNAT / AitherMesh
        if (o[0] == 10) return true;                                // RFC1918 10/8
        if (o[0] == 192 && o[1] == 168) return true;                // RFC1918 192.168/16
        return o[0] == 172 && o[1] >= 16 && o[1] <= 31;             // RFC1918 172.16/12
    }

    /** Normalized "https://host:port" for a valid private mesh hub, else "". */
    static String normalizeHubUrl(String raw) {
        if (raw == null) return "";
        String s = raw.trim();
        if (s.isEmpty()) return "";
        if (!s.contains("://")) s = "https://" + s;
        try {
            URI u = new URI(s);
            if (!"https".equalsIgnoreCase(u.getScheme())) return "";
            String host = u.getHost();
            if (!isPrivateMeshHost(host)) return "";
            int port = u.getPort();
            if (port == -1) port = 9448;
            if (port < 1 || port > 65535) return "";
            return "https://" + host.toLowerCase(Locale.ROOT) + ":" + port;
        } catch (Exception e) {
            return "";
        }
    }

    /**
     * Parse either:
     * - aither://mesh?hub=https://100.64.0.38:9448&token=<TOK>[&node=dgx-spark]
     * - https://100.64.0.38:9448/?token=<TOK> (awnix-voice-remote QR code directly)
     * Returns null when the hub or token is invalid.
     */
    static Pair parsePairUrl(String raw) {
        if (raw == null || raw.trim().isEmpty()) return null;
        try {
            URI u = new URI(raw.trim());
            String scheme = u.getScheme() == null ? "" : u.getScheme().toLowerCase(Locale.ROOT);
            String query = u.getRawQuery() == null ? "" : u.getRawQuery();
            String hub = "";
            String token = "";
            String node = "";
            for (String kv : query.split("&")) {
                int eq = kv.indexOf('=');
                if (eq <= 0) continue;
                String k = URLDecoder.decode(kv.substring(0, eq), StandardCharsets.UTF_8.name());
                String v = URLDecoder.decode(kv.substring(eq + 1), StandardCharsets.UTF_8.name());
                if ("hub".equals(k)) hub = v;
                else if ("token".equals(k) || "t".equals(k)) token = v;
                else if ("node".equals(k)) node = v;
            }
            if ("aither".equals(scheme) && "mesh".equalsIgnoreCase(u.getHost())) {
                hub = normalizeHubUrl(hub);
            } else if ("https".equals(scheme)) {
                int port = u.getPort() > 0 ? u.getPort() : 9448;
                hub = normalizeHubUrl("https://" + u.getHost() + ":" + port);
            } else {
                return null;
            }
            if (hub.isEmpty() || !validToken(token)) return null;
            String normNode = node.isEmpty() ? "" : normalizeNode(node, "");
            return new Pair(hub, token.trim(), normNode);
        } catch (Exception e) {
            return null;
        }
    }

    private static final Pattern SPARK_RE = Pattern.compile("\\b(dgx|spark|dgx[- ]spark)\\b", Pattern.CASE_INSENSITIVE);
    private static final Pattern PC_RE = Pattern.compile("\\b(my pc|the pc|5090|desktop|workstation|windows pc)\\b", Pattern.CASE_INSENSITIVE);
    private static final Pattern OPTI_RE = Pattern.compile("\\b(optiplex|opti)\\b", Pattern.CASE_INSENSITIVE);
    private static final Pattern MESH_Q_RE = Pattern.compile(
            "\\b(aither ?mesh|mesh status|mesh nodes|which nodes|what'?s running on|posture|heavy[- ]?moe|fleet[- ]?multi|creator mode|quiet mode)\\b",
            Pattern.CASE_INSENSITIVE);
    private static final Pattern SWITCH_RE = Pattern.compile(
            "\\b(switch|set|change|put|apply|use)\\b.{0,40}\\b(fleet[- ]?multi|heavy[- ]?moe|284b|creator|quiet)\\b",
            Pattern.CASE_INSENSITIVE);

    /** Inspect a spoken/typed turn for explicit mesh node or posture intent. */
    static IntentRoute routeIntent(String text) {
        if (text == null || text.trim().isEmpty()) return new IntentRoute("", "", false);
        String t = text.trim();
        String node = "";
        if (SPARK_RE.matcher(t).find()) node = "dgx-spark";
        else if (PC_RE.matcher(t).find()) node = "pc";
        else if (OPTI_RE.matcher(t).find()) node = "optiplex";

        String posture = "";
        Matcher sw = SWITCH_RE.matcher(t);
        if (sw.find()) {
            String raw = sw.group(2).toLowerCase(Locale.ROOT).replace(' ', '-');
            if (raw.contains("fleet")) posture = "fleet-multi";
            else if (raw.contains("heavy") || raw.contains("284")) posture = "heavy-moe";
            else if (raw.contains("creator")) posture = "creator";
            else if (raw.contains("quiet")) posture = "quiet";
        }
        boolean mesh = !node.isEmpty() || !posture.isEmpty() || MESH_Q_RE.matcher(t).find();
        return new IntentRoute(node, posture, mesh);
    }

    /** Minimal JSON body for POST /api/chat. */
    static String chatBody(String text, String node, boolean speak) {
        String n = normalizeNode(node, "auto");
        return "{\"text\":\"" + jsonEscape(text == null ? "" : text)
                + "\",\"node\":\"" + n
                + "\",\"speak\":" + speak + "}";
    }

    /** Minimal JSON body for POST /api/posture, or "" when posture is invalid. */
    static String postureBody(String posture, String node) {
        if (!validPosture(posture)) return "";
        String n = normalizeNode(node, "dgx-spark");
        if ("auto".equals(n)) n = "dgx-spark";
        return "{\"posture\":\"" + posture + "\",\"node\":\"" + n + "\"}";
    }

    static String jsonEscape(String s) {
        StringBuilder b = new StringBuilder(s.length() + 16);
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"' || c == '\\') b.append('\\').append(c);
            else if (c == '\n') b.append("\\n");
            else if (c == '\r') b.append("\\r");
            else if (c == '\t') b.append("\\t");
            else if (c < 0x20) b.append(String.format(Locale.ROOT, "\\u%04x", (int) c));
            else b.append(c);
        }
        return b.toString();
    }
}
