package com.aitherium.aither;

import org.json.JSONArray;
import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * The command channel's canonical bytes, exactly as Identity writes them:
 * {@code json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=True)}.
 * Only strings, whole numbers, booleans, null, objects and arrays: a fraction has no
 * byte-identical form across Python and Java, so it is refused, never guessed at.
 */
final class Canon {
    private Canon() {}

    static final class NotCanonical extends Exception {
        NotCanonical(String why) { super(why); }
    }

    /** The named fields of {@code o} (absent = null), canonicalised. */
    static String fields(JSONObject o, String... names) throws NotCanonical {
        Map<String, Object> m = new TreeMap<>();
        for (String n : names) m.put(n, o.has(n) ? o.opt(n) : null);
        StringBuilder sb = new StringBuilder();
        write(sb, m);
        return sb.toString();
    }

    static String hmac(String keyHex, String payload) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(keyHex.getBytes(StandardCharsets.US_ASCII), "HmacSHA256"));
            byte[] d = mac.doFinal(payload.getBytes(StandardCharsets.US_ASCII));
            StringBuilder sb = new StringBuilder();
            for (byte b : d) sb.append(String.format("%02x", b & 0xff));
            return sb.toString();
        } catch (Exception e) {
            return "";
        }
    }

    /** Constant time, so a wrong signature takes as long as a nearly right one. */
    static boolean same(String a, String b) {
        if (a == null || b == null || a.length() != b.length() || a.isEmpty()) return false;
        int r = 0;
        for (int i = 0; i < a.length(); i++) r |= a.charAt(i) ^ b.charAt(i);
        return r == 0;
    }

    @SuppressWarnings("unchecked")
    static void write(StringBuilder sb, Object v) throws NotCanonical {
        if (v == null || v == JSONObject.NULL) {
            sb.append("null");
        } else if (v instanceof Boolean) {
            sb.append(((Boolean) v) ? "true" : "false");
        } else if (v instanceof Integer || v instanceof Long || v instanceof Short) {
            sb.append(((Number) v).longValue());
        } else if (v instanceof Number) {
            throw new NotCanonical("a fraction has no canonical form");
        } else if (v instanceof String) {
            str(sb, (String) v);
        } else if (v instanceof JSONObject) {
            JSONObject o = (JSONObject) v;
            Map<String, Object> m = new TreeMap<>();
            for (Iterator<String> it = o.keys(); it.hasNext(); ) {
                String k = it.next();
                m.put(k, o.opt(k));
            }
            write(sb, m);
        } else if (v instanceof Map) {
            Map<String, Object> m = new TreeMap<>((Map<String, Object>) v);
            sb.append('{');
            boolean first = true;
            for (Map.Entry<String, Object> e : m.entrySet()) {
                if (!first) sb.append(',');
                first = false;
                str(sb, e.getKey());
                sb.append(':');
                write(sb, e.getValue());
            }
            sb.append('}');
        } else if (v instanceof JSONArray) {
            JSONArray a = (JSONArray) v;
            List<Object> l = new ArrayList<>();
            for (int i = 0; i < a.length(); i++) l.add(a.opt(i));
            write(sb, l);
        } else if (v instanceof List) {
            sb.append('[');
            boolean first = true;
            for (Object x : (List<Object>) v) {
                if (!first) sb.append(',');
                first = false;
                write(sb, x);
            }
            sb.append(']');
        } else {
            throw new NotCanonical("unsupported value " + v.getClass().getSimpleName());
        }
    }

    private static void str(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                case '\b': sb.append("\\b"); break;
                case '\f': sb.append("\\f"); break;
                default:
                    if (c < 0x20 || c > 0x7e) sb.append(String.format("\\u%04x", (int) c));
                    else sb.append(c);
            }
        }
        sb.append('"');
    }
}
