package com.aitherium.aither;

import java.util.ArrayList;
import java.util.List;

/**
 * "What's in this picture?": which engine looks at it, and the words on the wire. Pure Java
 * so test/VisionCheck.java runs it on a desktop JVM; VisionEngine (on this phone) and
 * VisionHosted (Aither online) do the work, DescribeActivity asks.
 *
 * The model on this phone is SmolVLM-500M-Instruct (Hugging Face TB, Apache-2.0), as the
 * ggml-org GGUF conversion: the Q8_0 language model (437 MB) and its Q8_0 vision projector
 * (109 MB), 546 MB in all. It is downloaded only after the owner taps Download, from a
 * pinned revision, and each file is kept only if its SHA-256 is the pinned one; nothing of
 * it is in the APK. It runs in the same llama-server LlmService uses (build_llama.py builds
 * it multimodal), as its own process on its own loopback port, and unloads when idle.
 *
 * Gates, the same kinds LlmService applies to Bonsai: never on a phone the workspace has not
 * vouched for or a child's phone (Config.localAiBlocked); offered only on a phone with the
 * RAM to run it and the storage to keep it; loaded only with enough free memory right now
 * and while the phone is not hot. Everything else goes to Aither online, which is the same
 * chat the app's Aither tab uses: the owner's account, the hosted vision model.
 */
final class Vision {
    enum Route { LOCAL, OFFER, HOSTED, NONE }

    static final String MODEL_ID = "smolvlm-500m";
    static final String LICENSE = "Apache-2.0";
    static final String SOURCE = "huggingface.co/ggml-org/SmolVLM-500M-Instruct-GGUF";
    static final String REVISION = "72e986006ef53e37cdd3f6d4241c90b0f01df376";
    static final String BASE = "https://huggingface.co/ggml-org/SmolVLM-500M-Instruct-GGUF/resolve/"
            + REVISION + "/";

    static final String MODEL_FILE = "SmolVLM-500M-Instruct-Q8_0.gguf";
    static final String MODEL_SHA256 = "9d4612de6a42214499e301494a3ecc2be0abdd9de44e663bda63f1152fad1bf4";
    static final long MODEL_BYTES = 436806912L;
    static final String PROJ_FILE = "mmproj-SmolVLM-500M-Instruct-Q8_0.gguf";
    static final String PROJ_SHA256 = "d1eb8b6b23979205fdf63703ed10f788131a3f812c7b1f72e0119d5d81295150";
    static final long PROJ_BYTES = 108783360L;
    static final long DOWNLOAD_BYTES = MODEL_BYTES + PROJ_BYTES;

    /** Free memory it needs beyond the files to load now: the image encoder's buffers, a
     *  4K-token KV cache and room left for the phone. Measured on a Pixel 8 (2026-10-07,
     *  this engine, -t 4 -c 4096): RSS 1.03 GB, 1.5 s load, 7.8 s for the first 800x600
     *  picture (218 prompt tokens with the image), then 45-62 tok/s answering. */
    static final long HEADROOM = 600L << 20;
    /** A phone with less RAM than this in all is never offered the download. */
    static final long MIN_TOTAL_RAM = 6L << 30;
    /** Storage left free after the download. */
    static final long STORAGE_MARGIN = 500L << 20;
    /** The longest side a picture is sent at (SmolVLM tiles at 512; hosted wants no more). */
    static final int MAX_SIDE = 1024;

    static final String DEFAULT_QUESTION = "What is in this picture? Describe it briefly.";

    private Vision() {}

    static String sizeMb() {
        return Math.round(DOWNLOAD_BYTES / 1e6) + " MB"; // decimal, as Bonsai's "248 MB"
    }

    /**
     * @param blocked   Config.localAiBlocked(): "" or why this phone runs no local model
     * @param engine    this build carries libllamaserver.so
     * @param installed both files are here at their pinned sizes
     * @param declined  the owner said "Not now" to the download (Settings undoes it)
     * @param availMem  ActivityManager.MemoryInfo.availMem
     * @param totalMem  ActivityManager.MemoryInfo.totalMem
     * @param freeBytes usable space where the model lives
     * @param hot       thermal status SEVERE or worse
     * @param signedIn  the app has an Aither session (Session.hasSession)
     */
    static Route pick(String blocked, boolean engine, boolean installed, boolean declined,
                      long availMem, long totalMem, long freeBytes, boolean hot, boolean signedIn) {
        boolean local = blocked != null && blocked.isEmpty() && engine;
        if (local && installed && !hot && canRunNow(availMem)) return Route.LOCAL;
        if (local && !installed && !declined && fitsPhone(totalMem) && roomFor(freeBytes)) {
            return Route.OFFER;
        }
        return signedIn ? Route.HOSTED : Route.NONE;
    }

    static boolean canRunNow(long availMem) {
        return availMem >= DOWNLOAD_BYTES + HEADROOM;
    }

    static boolean fitsPhone(long totalMem) {
        return totalMem >= MIN_TOTAL_RAM;
    }

    static boolean roomFor(long freeBytes) {
        return freeBytes >= DOWNLOAD_BYTES + STORAGE_MARGIN;
    }

    /** Why the picture is not looked at on this phone (shown under the Ask button). */
    static String whyNotLocal(String blocked, boolean engine, boolean installed, long availMem,
                              long totalMem, long freeBytes, boolean hot) {
        if (blocked == null) return "this phone is not set up for AI yet";
        if (!blocked.isEmpty()) return blocked;
        if (!engine) return "this build has no on-device model engine";
        if (!installed) {
            if (!fitsPhone(totalMem)) return "this phone has too little memory for the picture model";
            if (!roomFor(freeBytes)) return "not enough storage for the picture model (" + sizeMb() + ")";
            return "the picture model is not downloaded";
        }
        if (hot) return "the phone is hot";
        if (!canRunNow(availMem)) return "not enough free memory right now (" + (availMem >> 20) + " MB free)";
        return "";
    }

    /** The question asked: the owner's words, or the default for a picture sent bare. */
    static String question(String q) {
        String t = q == null ? "" : q.trim();
        return t.isEmpty() ? DEFAULT_QUESTION : t;
    }

    /** Width and height to send a w x h picture at: the longest side at most max. */
    static int[] scaled(int w, int h, int max) {
        if (w <= 0 || h <= 0) return new int[] {0, 0};
        int longest = Math.max(w, h);
        if (longest <= max) return new int[] {w, h};
        double f = (double) max / longest;
        return new int[] {Math.max(1, (int) Math.round(w * f)), Math.max(1, (int) Math.round(h * f))};
    }

    /** OpenAI chat body for llama-server: one user turn, the question then the picture. */
    static String chatBody(String question, String jpegBase64, int maxTokens) {
        return "{\"model\":\"" + MODEL_ID + "\",\"temperature\":0.2,\"max_tokens\":" + maxTokens
                + ",\"stream\":false,\"messages\":[{\"role\":\"user\",\"content\":["
                + "{\"type\":\"text\",\"text\":" + quote(question(question)) + "},"
                + "{\"type\":\"image_url\",\"image_url\":{\"url\":\"data:image/jpeg;base64,"
                + jpegBase64 + "\"}}]}]}";
    }

    /** A JSON string literal. */
    static String quote(String s) {
        StringBuilder b = new StringBuilder(s.length() + 2).append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': b.append("\\\""); break;
                case '\\': b.append("\\\\"); break;
                case '\n': b.append("\\n"); break;
                case '\r': b.append("\\r"); break;
                case '\t': b.append("\\t"); break;
                default:
                    if (c < 0x20) b.append(String.format("\\u%04x", (int) c));
                    else b.append(c);
            }
        }
        return b.append('"').toString();
    }

    /** Server-sent events as {event, data} pairs (multi-line data joined with \n). */
    static List<String[]> sse(String body) {
        List<String[]> out = new ArrayList<>();
        String event = "message";
        StringBuilder data = null;
        for (String raw : (body == null ? "" : body).split("\n", -1)) {
            String line = raw.endsWith("\r") ? raw.substring(0, raw.length() - 1) : raw;
            if (line.isEmpty()) {
                if (data != null) out.add(new String[] {event, data.toString()});
                event = "message";
                data = null;
            } else if (line.startsWith("event:")) {
                event = line.substring(6).trim();
            } else if (line.startsWith("data:")) {
                String d = line.substring(5);
                if (d.startsWith(" ")) d = d.substring(1);
                data = data == null ? new StringBuilder(d) : data.append('\n').append(d);
            }
        }
        if (data != null) out.add(new String[] {event, data.toString()});
        return out;
    }
}
