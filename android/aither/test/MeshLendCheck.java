package com.aitherium.aither;

import java.io.File;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * MeshLend on a desktop JVM: the gb rule, the sha pin check, the tailnet-only bind, the lend
 * size. Exit 0 = all hold. {@code self-test} breaks one rule on purpose and must exit 1.
 */
public class MeshLendCheck {
    static int bad;

    static void expect(boolean ok, String what) {
        if (!ok) {
            bad++;
            System.out.println("FAIL " + what);
        }
    }

    public static void main(String[] a) throws Exception {
        boolean selfTest = a.length > 0 && "self-test".equals(a[0]);
        // gb: decimal text 1..256, nothing else
        for (int i = 1; i <= 256; i++) expect(MeshLend.gbOk(String.valueOf(i)), "gb " + i);
        for (String s : new String[] {"0", "257", "300", "01", "08", "-1", "+8", "8.0", " 8", "8 ",
                "8\n", "", "1e2", "0x10", "٨", "999999999999"}) {
            expect(!MeshLend.gbOk(s), "gb refused " + s);
        }
        expect(!MeshLend.gbOk(8), "a JSON number is refused");
        expect(!MeshLend.gbOk(null), "null is refused");

        // tailnet: 100.64.0.0/10 only
        for (String ip : new String[] {"100.64.0.1", "100.127.255.254", "100.101.2.3"}) {
            expect(MeshLend.tailnet(ip), "tailnet " + ip);
        }
        for (String ip : new String[] {"0.0.0.0", "127.0.0.1", "192.168.1.5", "10.0.0.1",
                "100.63.255.255", "100.128.0.1", "100.64.0", "100.64.0.1.2", "100.64.0.256",
                "100.064.0.1x", "١٠٠.64.0.1", "", "::", "fd7a:115c:a1e0::1", " 100.64.0.1"}) {
            expect(!MeshLend.tailnet(ip), "not tailnet " + ip);
        }
        List<String[]> addrs = new ArrayList<>();
        addrs.add(new String[] {"wlan0", "192.168.1.9"});
        addrs.add(new String[] {"null", "100.80.0.2"});
        addrs.add(new String[] {"tun0", "100.90.1.2"});
        expect("100.90.1.2".equals(MeshLend.pickAddress(addrs)), "a tun address wins");
        addrs.remove(2);
        expect("100.80.0.2".equals(MeshLend.pickAddress(addrs)), "any tailnet address next");
        addrs.remove(1);
        expect(MeshLend.pickAddress(addrs).isEmpty(), "no tailnet = not on the mesh");

        // Tailscale's VPN, not any VPN: MagicDNS resolver or a ts.net domain
        expect(MeshLend.tailscale(Arrays.asList("100.100.100.100"), null), "MagicDNS resolver");
        expect(MeshLend.tailscale(new ArrayList<>(), "tail1234.ts.net"), "ts.net domain");
        expect(!MeshLend.tailscale(Arrays.asList("100.96.0.1", "1.1.1.1"), "example.com"), "other VPN");
        expect(!MeshLend.tailscale(new ArrayList<>(), "evil-ts.net.example.com"), "ts.net lookalike");
        expect(!MeshLend.tailscale(null, null), "nothing");

        // argv: never a host off the tailnet
        for (String h : new String[] {"0.0.0.0", "127.0.0.1", "192.168.1.9", ""}) {
            boolean threw = false;
            try {
                MeshLend.argv("/x/libggml_rpc_v82.so", h, 4);
            } catch (IllegalArgumentException e) {
                threw = true;
            }
            expect(selfTest ? !threw : threw, "argv refuses " + h);
        }
        expect(MeshLend.argv("/x/s", "100.90.1.2", 4).equals(Arrays.asList(
                "/x/s", "-H", "100.90.1.2", "-p", "50052", "-d", "CPU", "-t", "4")), "argv flags");

        // lend size: honoured exactly or refused, RESERVE_MB always kept
        expect(MeshLend.lendRefusal(2, 2048 + MeshLend.RESERVE_MB).isEmpty(), "2 GB fits");
        expect(!MeshLend.lendRefusal(2, 2047 + MeshLend.RESERVE_MB).isEmpty(), "2 GB does not fit");
        expect(MeshLend.lendRefusal(4, 3698).contains("at most 2 GB"), "says how much would fit");
        expect(MeshLend.threads(8) == 4 && MeshLend.threads(1) == 1, "threads");
        expect(MeshLend.usage("  -d, --device <dev1,dev2,...>") && !MeshLend.usage(""), "usage");

        // the pin check: right bytes pass, one changed byte or a wrong size fails
        File tmp = File.createTempFile("meshlend", ".so");
        tmp.deleteOnExit();
        byte[] body = "hello".getBytes("UTF-8");
        Files.write(tmp.toPath(), body);
        String sha = MeshLend.sha256(tmp);
        expect("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824".equals(sha), "sha256");
        String[] row = {"libx.so", "x", "5", sha};
        expect(MeshLend.verify(tmp, row).isEmpty(), "pinned bytes pass");
        Files.write(tmp.toPath(), "hellp".getBytes("UTF-8"));
        expect(MeshLend.verify(tmp, row).contains("sha256"), "a changed byte fails");
        Files.write(tmp.toPath(), "hello!".getBytes("UTF-8"));
        expect(MeshLend.verify(tmp, row).contains("size"), "a wrong size fails");
        expect(MeshLend.verify(new File(tmp.getPath() + ".gone"), row).contains("not in this build"),
                "a missing file fails");
        for (String[] b : MeshLend.BUILDS) {
            expect(b[0].matches("lib[a-z0-9_]+\\.so") && b[3].matches("[0-9a-f]{64}"), "build row " + b[0]);
        }

        System.out.println(bad == 0 ? "OK" : bad + " failure(s)");
        System.exit(bad == 0 ? 0 : 1);
    }
}
