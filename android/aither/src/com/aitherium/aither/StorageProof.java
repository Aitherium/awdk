package com.aitherium.aither;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.io.RandomAccessFile;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.KeyFactory;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.MessageDigest;
import java.security.PrivateKey;
import java.security.Signature;
import java.security.spec.PKCS8EncodedKeySpec;

/**
 * Signed storage receipts and proof of custody on this phone (mesh storage B12).
 *
 * Every job the storage worker finishes is reported with an Ed25519 signature over a short
 * statement (job, kind, block hash, ok, answer); Genesis checks it against the key this phone
 * registered on its first poll and countersigns it into the family's receipt chain. The
 * statement names the block by its hash only, never a file name.
 *
 * A "prove" job asks for sha256(nonce || bytes[offset, offset + length]) of a kept copy: the
 * phone can only answer it while it really holds the bytes.
 *
 * Pure Java (no Android classes), so test/StorageProofCheck runs it on a desktop JVM and the
 * Python side verifies the same signature. Ed25519 needs Android 13 (API 33); an older phone
 * has no signer and keeps working unsigned (Genesis then never challenges it).
 */
final class StorageProof {
    static final String TAG = "aither-storage-receipt/1";
    static final int MAX_RANGE = 1 << 20;

    private StorageProof() {}

    /** Must match lib/hearth/storage_receipts.statement byte for byte. */
    static byte[] statement(String job, String kind, String oid, boolean ok, String answer) {
        String s = TAG + "\n" + job + "\n" + kind + "\n" + oid + "\n" + (ok ? "1" : "0") + "\n"
                + (answer == null ? "" : answer);
        return s.getBytes(StandardCharsets.UTF_8);
    }

    /** sha256(nonce || chunk), hex. */
    static String answer(String nonceHex, byte[] chunk) throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        md.update(unhex(nonceHex));
        md.update(chunk);
        return hex(md.digest());
    }

    /** The answer for a byte range of a kept file; null when the range is not in the file. */
    static String answer(String nonceHex, File kept, long offset, int length) throws Exception {
        if (offset < 0 || length < 0 || length > MAX_RANGE || !kept.isFile()
                || offset + length > kept.length()) return null;
        byte[] chunk = new byte[length];
        try (RandomAccessFile f = new RandomAccessFile(kept, "r")) {
            f.seek(offset);
            f.readFully(chunk);
        }
        return answer(nonceHex, chunk);
    }

    static String hex(byte[] b) {
        StringBuilder sb = new StringBuilder();
        for (byte x : b) sb.append(String.format("%02x", x & 0xff));
        return sb.toString();
    }

    static byte[] unhex(String s) {
        if (s == null || s.length() % 2 != 0 || !s.matches("[0-9a-f]*")) {
            throw new IllegalArgumentException("not hex");
        }
        byte[] out = new byte[s.length() / 2];
        for (int i = 0; i < out.length; i++) {
            out[i] = (byte) Integer.parseInt(s.substring(2 * i, 2 * i + 2), 16);
        }
        return out;
    }

    /** This device's receipt key, kept in app-private storage beside (not in) the pool folder. */
    static final class Signer {
        final String pubHex;
        private final PrivateKey key;

        private Signer(PrivateKey key, String pubHex) {
            this.key = key;
            this.pubHex = pubHex;
        }

        /** Load or make the key in {@code dir}; null when this device cannot sign (API < 33). */
        static Signer load(File dir) {
            File priv = new File(dir, "storage-receipt.key");
            File pub = new File(dir, "storage-receipt.pub");
            try {
                if (priv.isFile() && pub.isFile()) {
                    PrivateKey k = KeyFactory.getInstance("Ed25519")
                            .generatePrivate(new PKCS8EncodedKeySpec(Files.readAllBytes(priv.toPath())));
                    String p = new String(Files.readAllBytes(pub.toPath()), StandardCharsets.US_ASCII).trim();
                    if (p.matches("[0-9a-f]{64}")) return new Signer(k, p);
                }
                KeyPair kp = KeyPairGenerator.getInstance("Ed25519").generateKeyPair();
                byte[] x509 = kp.getPublic().getEncoded(); // 12-byte header + the raw 32-byte key
                String p = hex(java.util.Arrays.copyOfRange(x509, x509.length - 32, x509.length));
                dir.mkdirs();
                write(priv, kp.getPrivate().getEncoded());
                write(pub, p.getBytes(StandardCharsets.US_ASCII));
                return new Signer(kp.getPrivate(), p);
            } catch (Exception e) {
                return null;
            }
        }

        String sign(byte[] message) throws Exception {
            Signature s = Signature.getInstance("Ed25519");
            s.initSign(key);
            s.update(message);
            return hex(s.sign());
        }

        private static void write(File f, byte[] data) throws IOException {
            File part = new File(f.getPath() + ".part");
            try (OutputStream o = new FileOutputStream(part)) {
                o.write(data);
            }
            if (!part.renameTo(f)) {
                f.delete();
                if (!part.renameTo(f)) throw new IOException("could not keep the receipt key");
            }
        }
    }
}
