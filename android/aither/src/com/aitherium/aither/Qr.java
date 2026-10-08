package com.aitherium.aither;

import java.nio.charset.StandardCharsets;

/**
 * A small QR Code encoder (ISO/IEC 18004): byte mode, error correction level M, versions
 * 1-10 (up to 213 bytes, plenty for a sign-in link). Pure Java with no dependencies, so the
 * watch draws the device-link QR without a library and test/DeviceLinkCheck.java encodes on a
 * desktop JVM (dev/tests decode the result with OpenCV to prove it scans).
 *
 * {@code Qr.encode(text)} returns the module grid, true = dark, without the quiet zone
 * (draw 4 light modules around it).
 */
final class Qr {
    private Qr() {}

    // Level M, versions 1..10 (index 0 unused).
    private static final int[] ECC_PER_BLOCK = {0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26};
    private static final int[] BLOCKS = {0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5};
    private static final int FORMAT_M = 0; // level M's two format bits

    static boolean[][] encode(String text) {
        byte[] data = text.getBytes(StandardCharsets.UTF_8);
        int ver = 1;
        for (; ver <= 10; ver++) {
            int lenBits = ver <= 9 ? 8 : 16;
            if (4 + lenBits + data.length * 8 <= dataCodewords(ver) * 8) break;
        }
        if (ver > 10) throw new IllegalArgumentException("too long for a version-10 QR: " + data.length + " bytes");
        int cap = dataCodewords(ver);
        Bits bb = new Bits();
        bb.put(0b0100, 4);
        bb.put(data.length, ver <= 9 ? 8 : 16);
        for (byte b : data) bb.put(b & 0xff, 8);
        bb.put(0, Math.min(4, cap * 8 - bb.len));
        bb.put(0, (8 - bb.len % 8) % 8);
        for (int pad = 0xEC; bb.len < cap * 8; pad ^= 0xEC ^ 0x11) bb.put(pad, 8);
        byte[] words = interleave(ver, bb.bytes(cap));

        int size = ver * 4 + 17;
        boolean[][] m = new boolean[size][size];
        boolean[][] fn = new boolean[size][size];
        drawFunctions(ver, m, fn);
        place(words, m, fn);
        int best = 0;
        long bestScore = Long.MAX_VALUE;
        for (int mask = 0; mask < 8; mask++) {
            applyMask(mask, m, fn);
            drawFormat(mask, m, fn);
            long s = penalty(m);
            if (s < bestScore) { bestScore = s; best = mask; }
            applyMask(mask, m, fn); // undo (XOR)
        }
        applyMask(best, m, fn);
        drawFormat(best, m, fn);
        return m;
    }

    // ------------------------------------------------------------------ capacity

    private static int rawModules(int ver) {
        int r = (16 * ver + 128) * ver + 64;
        if (ver >= 2) {
            int n = ver / 7 + 2;
            r -= (25 * n - 10) * n - 55;
            if (ver >= 7) r -= 36;
        }
        return r;
    }

    static int dataCodewords(int ver) {
        return rawModules(ver) / 8 - ECC_PER_BLOCK[ver] * BLOCKS[ver];
    }

    // ------------------------------------------------------------------ error correction

    private static byte[] interleave(int ver, byte[] data) {
        int nb = BLOCKS[ver], ecc = ECC_PER_BLOCK[ver];
        int raw = rawModules(ver) / 8;
        int shortBlocks = nb - raw % nb;
        int shortLen = raw / nb;
        byte[] gen = rsGenerator(ecc);
        byte[][] blocks = new byte[nb][];
        for (int i = 0, k = 0; i < nb; i++) {
            int dLen = shortLen - ecc + (i < shortBlocks ? 0 : 1);
            byte[] d = new byte[dLen];
            System.arraycopy(data, k, d, 0, dLen);
            k += dLen;
            byte[] e = rsRemainder(d, gen);
            byte[] blk = new byte[shortLen + 1];
            System.arraycopy(d, 0, blk, 0, dLen);
            System.arraycopy(e, 0, blk, blk.length - ecc, ecc);
            blocks[i] = blk; // short blocks carry one unused byte before their ECC
        }
        byte[] out = new byte[raw];
        int o = 0;
        for (int i = 0; i < blocks[0].length; i++) {
            for (int j = 0; j < nb; j++) {
                if (i != shortLen - ecc || j >= shortBlocks) out[o++] = blocks[j][i];
            }
        }
        return out;
    }

    private static int gfMul(int x, int y) {
        int z = 0;
        for (int i = 7; i >= 0; i--) {
            z = (z << 1) ^ ((z >>> 7) * 0x11D);
            z ^= ((y >>> i) & 1) * x;
        }
        return z & 0xff;
    }

    private static byte[] rsGenerator(int degree) {
        byte[] r = new byte[degree];
        r[degree - 1] = 1;
        int root = 1;
        for (int i = 0; i < degree; i++) {
            for (int j = 0; j < degree; j++) {
                r[j] = (byte) gfMul(r[j] & 0xff, root);
                if (j + 1 < degree) r[j] ^= r[j + 1];
            }
            root = gfMul(root, 0x02);
        }
        return r;
    }

    private static byte[] rsRemainder(byte[] data, byte[] gen) {
        byte[] r = new byte[gen.length];
        for (byte b : data) {
            int f = (b ^ r[0]) & 0xff;
            System.arraycopy(r, 1, r, 0, r.length - 1);
            r[r.length - 1] = 0;
            for (int i = 0; i < r.length; i++) r[i] ^= (byte) gfMul(gen[i] & 0xff, f);
        }
        return r;
    }

    // ------------------------------------------------------------------ function patterns

    private static void set(boolean[][] m, boolean[][] fn, int x, int y, boolean dark) {
        m[y][x] = dark;
        fn[y][x] = true;
    }

    private static void drawFunctions(int ver, boolean[][] m, boolean[][] fn) {
        int size = m.length;
        for (int i = 0; i < size; i++) {
            set(m, fn, 6, i, i % 2 == 0);
            set(m, fn, i, 6, i % 2 == 0);
        }
        finder(m, fn, 3, 3);
        finder(m, fn, size - 4, 3);
        finder(m, fn, 3, size - 4);
        int[] pos = alignmentPositions(ver);
        for (int i = 0; i < pos.length; i++) {
            for (int j = 0; j < pos.length; j++) {
                boolean corner = (i == 0 && j == 0) || (i == 0 && j == pos.length - 1) || (i == pos.length - 1 && j == 0);
                if (!corner) alignment(m, fn, pos[i], pos[j]);
            }
        }
        drawFormat(0, m, fn); // reserve; rewritten per mask
        if (ver >= 7) {
            int rem = ver;
            for (int i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1F25);
            long bits = ((long) ver << 12) | rem;
            for (int i = 0; i < 18; i++) {
                boolean b = ((bits >>> i) & 1) != 0;
                int a = size - 11 + i % 3, c = i / 3;
                set(m, fn, a, c, b);
                set(m, fn, c, a, b);
            }
        }
    }

    private static void finder(boolean[][] m, boolean[][] fn, int cx, int cy) {
        int size = m.length;
        for (int dy = -4; dy <= 4; dy++) {
            for (int dx = -4; dx <= 4; dx++) {
                int d = Math.max(Math.abs(dx), Math.abs(dy));
                int x = cx + dx, y = cy + dy;
                if (x >= 0 && x < size && y >= 0 && y < size) set(m, fn, x, y, d != 2 && d != 4);
            }
        }
    }

    private static void alignment(boolean[][] m, boolean[][] fn, int cx, int cy) {
        for (int dy = -2; dy <= 2; dy++) {
            for (int dx = -2; dx <= 2; dx++) {
                set(m, fn, cx + dx, cy + dy, Math.max(Math.abs(dx), Math.abs(dy)) != 1);
            }
        }
    }

    private static int[] alignmentPositions(int ver) {
        if (ver == 1) return new int[0];
        int n = ver / 7 + 2;
        int step = (ver == 32) ? 26 : (ver * 4 + n * 2 + 1) / (n * 2 - 2) * 2;
        int[] r = new int[n];
        r[0] = 6;
        for (int i = n - 1, p = ver * 4 + 10; i >= 1; i--, p -= step) r[i] = p;
        return r;
    }

    private static void drawFormat(int mask, boolean[][] m, boolean[][] fn) {
        int size = m.length;
        int data = FORMAT_M << 3 | mask;
        int rem = data;
        for (int i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
        int bits = (data << 10 | rem) ^ 0x5412;
        for (int i = 0; i <= 5; i++) set(m, fn, 8, i, bit(bits, i));
        set(m, fn, 8, 7, bit(bits, 6));
        set(m, fn, 8, 8, bit(bits, 7));
        set(m, fn, 7, 8, bit(bits, 8));
        for (int i = 9; i < 15; i++) set(m, fn, 14 - i, 8, bit(bits, i));
        for (int i = 0; i < 8; i++) set(m, fn, size - 1 - i, 8, bit(bits, i));
        for (int i = 8; i < 15; i++) set(m, fn, 8, size - 15 + i, bit(bits, i));
        set(m, fn, 8, size - 8, true); // the dark module
    }

    private static boolean bit(int x, int i) {
        return ((x >>> i) & 1) != 0;
    }

    // ------------------------------------------------------------------ data and masks

    private static void place(byte[] words, boolean[][] m, boolean[][] fn) {
        int size = m.length;
        int i = 0;
        for (int right = size - 1; right >= 1; right -= 2) {
            if (right == 6) right = 5;
            for (int vert = 0; vert < size; vert++) {
                for (int j = 0; j < 2; j++) {
                    int x = right - j;
                    boolean upward = ((right + 1) & 2) == 0;
                    int y = upward ? size - 1 - vert : vert;
                    if (!fn[y][x] && i < words.length * 8) {
                        m[y][x] = bit(words[i >>> 3] & 0xff, 7 - (i & 7));
                        i++;
                    }
                }
            }
        }
    }

    private static void applyMask(int mask, boolean[][] m, boolean[][] fn) {
        int size = m.length;
        for (int y = 0; y < size; y++) {
            for (int x = 0; x < size; x++) {
                boolean inv;
                switch (mask) {
                    case 0: inv = (x + y) % 2 == 0; break;
                    case 1: inv = y % 2 == 0; break;
                    case 2: inv = x % 3 == 0; break;
                    case 3: inv = (x + y) % 3 == 0; break;
                    case 4: inv = (x / 3 + y / 2) % 2 == 0; break;
                    case 5: inv = x * y % 2 + x * y % 3 == 0; break;
                    case 6: inv = (x * y % 2 + x * y % 3) % 2 == 0; break;
                    default: inv = ((x + y) % 2 + x * y % 3) % 2 == 0; break;
                }
                if (inv && !fn[y][x]) m[y][x] = !m[y][x];
            }
        }
    }

    /** The standard's four penalty rules, close enough to pick a well-scanning mask. */
    private static long penalty(boolean[][] m) {
        int size = m.length;
        long score = 0;
        for (int pass = 0; pass < 2; pass++) {
            for (int a = 0; a < size; a++) {
                int run = 1;
                for (int b = 1; b <= size; b++) {
                    boolean same = b < size && cell(m, pass, a, b) == cell(m, pass, a, b - 1);
                    if (same) {
                        run++;
                    } else {
                        if (run >= 5) score += 3 + (run - 5);
                        run = 1;
                    }
                }
                for (int b = 0; b + 10 < size + 4; b++) { // 1:1:3:1:1 with 4 light either side
                    if (finderLike(m, pass, a, b)) score += 40;
                }
            }
        }
        int dark = 0;
        for (int y = 0; y < size; y++) {
            for (int x = 0; x < size; x++) {
                if (m[y][x]) dark++;
                if (x + 1 < size && y + 1 < size) {
                    boolean c = m[y][x];
                    if (c == m[y][x + 1] && c == m[y + 1][x] && c == m[y + 1][x + 1]) score += 3;
                }
            }
        }
        int total = size * size;
        int k = (Math.abs(dark * 20 - total * 10) + total - 1) / total - 1;
        score += Math.max(0, k) * 10L;
        return score;
    }

    private static boolean cell(boolean[][] m, int pass, int a, int b) {
        return pass == 0 ? m[a][b] : m[b][a];
    }

    private static boolean finderLike(boolean[][] m, int pass, int a, int start) {
        int size = m.length;
        boolean[] pat = {true, false, true, true, true, false, true};
        boolean before = true, after = true;
        for (int i = 0; i < 7; i++) {
            int b = start + i;
            if (b >= size || cell(m, pass, a, b) != pat[i]) return false;
        }
        for (int i = 1; i <= 4; i++) {
            int bb = start - i, ba = start + 6 + i;
            if (bb >= 0 && cell(m, pass, a, bb)) before = false;
            if (ba < size && cell(m, pass, a, ba)) after = false;
        }
        return before || after;
    }

    // ------------------------------------------------------------------ bit buffer

    private static final class Bits {
        final java.util.BitSet set = new java.util.BitSet();
        int len;

        void put(int v, int n) {
            for (int i = n - 1; i >= 0; i--) set.set(len++, ((v >>> i) & 1) != 0);
        }

        byte[] bytes(int n) {
            byte[] r = new byte[n];
            for (int i = 0; i < n * 8; i++) if (set.get(i)) r[i >>> 3] |= (byte) (0x80 >>> (i & 7));
            return r;
        }
    }
}
