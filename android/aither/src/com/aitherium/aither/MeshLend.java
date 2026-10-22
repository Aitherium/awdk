package com.aitherium.aither;

import java.io.File;
import java.io.FileInputStream;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.regex.Pattern;

/**
 * The pure half of {@code mesh-lend-on}: lend this phone's RAM to the pooled model over the
 * mesh with ggml's RPC server (the same release, files and flags as awdk's adk.mesh_lend).
 *
 * No Android imports, so a desktop JVM checks it (test/MeshLendCheck.java). The rules:
 * <ul>
 * <li>{@code gb} is a decimal 1..256 STRING, nothing else (Identity's MESH_LEND_GB_RE).</li>
 * <li>The server ships INSIDE the APK as {@code lib*.so} in {@code lib/arm64-v8a/}: an app
 *     targeting API 29+ may not exec a file it wrote (W^X), only one from nativeLibraryDir.
 *     Each copy is checked against {@link #BUILDS} (adk.mesh_lend.PINS) before it runs.</li>
 * <li>It binds ONLY to a Tailscale address (100.64.0.0/10): the RPC server is
 *     unauthenticated, so never 0.0.0.0, a LAN address or loopback on a phone.</li>
 * </ul>
 */
final class MeshLend {
    /** The aitherkvcache release the builds come from (adk.mesh_lend.RELEASE). */
    static final String RELEASE = "v41-lend-rpc-20261022";
    static final int PORT = 50052;
    /** Kept free for the phone itself; Android's own killer reclaims cached apps first. */
    static final long RESERVE_MB = 1536;
    static final long MIN_LEND_MB = 1024;
    static final Pattern GB = Pattern.compile(
            "\\A(?:[1-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-6])\\z");

    /**
     * {lib name in nativeLibraryDir, release asset, size, sha256}, fastest first. Same
     * numbers as adk.mesh_lend.PINS (a test pins that); build.py reads this table to fetch
     * and verify the files it packs.
     */
    static final String[][] BUILDS = {
        {"libggml_rpc_v82.so", "ggml-rpc-server-linux-arm64-v82", "1868104",
            "bf2605dc6f9488f2186cb227ee7ccba712308e1b04b399a6c03d8699d4aa551d"},
        {"libggml_rpc_v8a.so", "ggml-rpc-server-linux-arm64-v8a", "1847888",
            "413c18aa23ae280e2e897daad93f5ba4a0c2726fb24388fdb06b5c01cf42b0f6"},
    };

    private MeshLend() { }

    /** Is {@code v} a gb argument this phone accepts? A JSON number is NOT (Identity sends text). */
    static boolean gbOk(Object v) {
        return v instanceof String && GB.matcher((String) v).matches();
    }

    /** Lowercase hex SHA-256 of a file. */
    static String sha256(File f) throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        try (FileInputStream in = new FileInputStream(f)) {
            byte[] b = new byte[1 << 16];
            int n;
            while ((n = in.read(b)) > 0) md.update(b, 0, n);
        }
        StringBuilder hex = new StringBuilder();
        for (byte x : md.digest()) hex.append(String.format("%02x", x & 0xff));
        return hex.toString();
    }

    /** Why {@code f} may not run as {@code build}, or "" when its size and SHA-256 are the pins. */
    static String verify(File f, String[] build) {
        if (!f.isFile()) return build[0] + " is not in this build of Aither";
        if (f.length() != Long.parseLong(build[2])) return build[0] + " has the wrong size";
        try {
            if (!build[3].equals(sha256(f))) return build[0] + " does not match its pinned sha256";
        } catch (Exception e) {
            return build[0] + " could not be read (" + e.getClass().getSimpleName() + ")";
        }
        return "";
    }

    /** Is {@code ip} a dotted IPv4 address inside Tailscale's 100.64.0.0/10? */
    static boolean tailnet(String ip) {
        if (ip == null) return false;
        String[] o = ip.split("\\.", -1);
        if (o.length != 4) return false;
        int[] n = new int[4];
        for (int i = 0; i < 4; i++) {
            if (o[i].isEmpty() || o[i].length() > 3 || !o[i].chars().allMatch(c -> c >= '0' && c <= '9')) {
                return false;
            }
            n[i] = Integer.parseInt(o[i]);
            if (n[i] > 255) return false;
        }
        return n[0] == 100 && n[1] >= 64 && n[1] <= 127;
    }

    /**
     * The address to bind among {interface, address} pairs from the phone's VPN networks:
     * the first tailnet IPv4, preferring a tun interface. "" = not on the mesh.
     */
    static String pickAddress(List<String[]> addrs) {
        String any = "";
        for (String[] a : addrs) {
            if (a == null || a.length < 2 || !tailnet(a[1])) continue;
            if (a[0] != null && a[0].startsWith("tun")) return a[1];
            if (any.isEmpty()) any = a[1];
        }
        return any;
    }

    /**
     * Is a VPN network with these DNS servers and search domains Tailscale's? Its resolver
     * is 100.100.100.100 (MagicDNS) and its domains end in .ts.net; any other VPN app that
     * hands out a 100.64/10 address (or a carrier CGNAT) is not the owner's mesh.
     */
    static boolean tailscale(List<String> dns, String domains) {
        if (dns != null && dns.contains("100.100.100.100")) return true;
        if (domains == null) return false;
        for (String d : domains.split("[ ,]+")) {
            String x = d.trim().toLowerCase(java.util.Locale.ROOT);
            if (x.endsWith(".ts.net") || x.equals("ts.net")) return true;
        }
        return false;
    }

    /** Why {@code gb} cannot be lent with {@code availMb} free, or "" when it can. Never cut silently. */
    static String lendRefusal(int gb, long availMb) {
        long want = gb * 1024L;
        long room = availMb - RESERVE_MB;
        if (want < MIN_LEND_MB) return "the minimum offer is " + MIN_LEND_MB + " MB";
        if (want > room) {
            return gb + " GB would leave " + (availMb - want) + " MB free; at most "
                    + Math.max(0, room) / 1024 + " GB can be lent now (" + availMb + " MB free, "
                    + RESERVE_MB + " MB kept for the phone)";
        }
        return "";
    }

    /** The server's argv: the flags adk.mesh_lend uses. Refuses any host off the tailnet. */
    static List<String> argv(String exe, String host, int threads) {
        if (!tailnet(host)) throw new IllegalArgumentException("not a tailnet address: " + host);
        return new ArrayList<>(Arrays.asList(exe, "-H", host, "-p", String.valueOf(PORT),
                "-d", "CPU", "-t", String.valueOf(Math.max(1, threads))));
    }

    /** Did {@code exe -h} print the RPC server's usage (it ran on this CPU)? */
    static boolean usage(String helpText) {
        return helpText != null && helpText.contains("--device");
    }

    /** Half the cores, at least one: the phone keeps the rest. */
    static int threads(int cores) {
        return Math.max(1, cores / 2);
    }
}
