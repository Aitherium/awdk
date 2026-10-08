package com.aitherium.aither;

import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStream;

/**
 * Desktop-JVM check of StorageProof: the receipt key is made once and reloaded, the range
 * answer reads the right bytes, and out-of-file ranges are refused. Prints the values the
 * Python test verifies (signature over the shared statement, the custody answer).
 */
public final class StorageProofCheck {
    private static int failed;

    private static void check(boolean ok, String what) {
        if (!ok) {
            failed++;
            System.out.println("FAIL " + what);
        }
    }

    public static void main(String[] args) throws Exception {
        File dir = new File(args[0]);
        StorageProof.Signer s = StorageProof.Signer.load(dir);
        check(s != null, "this JVM can sign");
        if (s == null) {
            System.out.println("FAILED");
            System.exit(1);
        }
        StorageProof.Signer again = StorageProof.Signer.load(dir);
        check(again != null && again.pubHex.equals(s.pubHex), "the key is kept, not remade");

        byte[] data = new byte[10000];
        for (int i = 0; i < data.length; i++) data[i] = (byte) (i * 7 + 3);
        File kept = new File(dir, "block");
        try (OutputStream o = new FileOutputStream(kept)) {
            o.write(data);
        }
        String nonce = "00112233445566778899aabbccddeeff";
        String ans = StorageProof.answer(nonce, kept, 1234, 4096);
        byte[] chunk = java.util.Arrays.copyOfRange(data, 1234, 1234 + 4096);
        check(ans != null && ans.equals(StorageProof.answer(nonce, chunk)), "range answer");
        check(StorageProof.answer(nonce, kept, 9000, 4096) == null, "range past the end refused");
        check(StorageProof.answer(nonce, kept, -1, 10) == null, "negative offset refused");
        check(StorageProof.answer(nonce, new File(dir, "missing"), 0, 10) == null, "missing copy");

        String job = "sj_abcdefghijklmnop";
        String oid = "ab".repeat(32);
        String sig = s.sign(StorageProof.statement(job, "prove", oid, true, ans));
        check(sig.matches("[0-9a-f]{128}"), "signature shape");
        System.out.println("pub=" + s.pubHex);
        System.out.println("sig=" + sig);
        System.out.println("answer=" + ans);
        System.out.println("statement=" + StorageProof.hex(
                StorageProof.statement(job, "prove", oid, true, ans)));
        System.out.println(failed == 0 ? "OK" : "FAILED");
        System.exit(failed == 0 ? 0 : 1);
    }
}
