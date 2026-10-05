package com.aitherium.aither;

/** Exit 0 when ShieldPackets lifts a DNS query out of IPv4/UDP and builds a valid reply. */
public class ShieldPacketsCheck {
    static int bad = 0;

    static void check(boolean ok, String what) {
        if (!ok) { System.out.println("FAIL " + what); bad++; }
    }

    static byte[] query() {
        // id 0x1234, RD, one question: example.org A IN
        byte[] name = {7, 'e', 'x', 'a', 'm', 'p', 'l', 'e', 3, 'o', 'r', 'g', 0};
        byte[] q = new byte[12 + name.length + 4];
        q[0] = 0x12; q[1] = 0x34; q[2] = 0x01; q[5] = 1;
        System.arraycopy(name, 0, q, 12, name.length);
        q[12 + name.length + 1] = 1; // QTYPE A
        q[12 + name.length + 3] = 1; // QCLASS IN
        return q;
    }

    static byte[] packet(byte[] dns, byte[] src, byte[] dst, int dport, int proto) {
        int total = 28 + dns.length;
        byte[] p = new byte[total];
        p[0] = 0x45; p[2] = (byte) (total >> 8); p[3] = (byte) total; p[8] = 64; p[9] = (byte) proto;
        System.arraycopy(src, 0, p, 12, 4);
        System.arraycopy(dst, 0, p, 16, 4);
        p[20] = (byte) 0xc3; p[21] = 0x50; // source port 50000
        p[22] = (byte) (dport >> 8); p[23] = (byte) dport;
        p[24] = (byte) ((8 + dns.length) >> 8); p[25] = (byte) (8 + dns.length);
        System.arraycopy(dns, 0, p, 28, dns.length);
        return p;
    }

    public static void main(String[] a) {
        byte[] client = {10, 111, (byte) 222, 1};
        byte[] server = {10, 111, (byte) 222, 2};
        byte[] q = query();
        byte[] pkt = packet(q, client, server, 53, 17);
        ShieldPackets.Query got = ShieldPackets.parse(pkt, pkt.length, server);
        check(got != null, "parse a DNS query");
        if (got != null) {
            check(got.clientPort == 50000, "client port");
            check(java.util.Arrays.equals(got.dns, q), "payload");
            byte[] r = ShieldPackets.reply(got, ShieldPackets.servfail(got.dns));
            check(ShieldPackets.checksum(r, 0, 20) == 0, "reply IPv4 header checksum verifies");
            check(r[12] == server[0] && r[15] == server[3] && r[16] == client[0], "reply addresses swapped");
            check(ShieldPackets.u16(r, 22) == 50000 && ShieldPackets.u16(r, 20) == 53, "reply ports");
            check(ShieldPackets.u16(r, 2) == r.length, "reply total length");
            check((r[28 + 2] & 0x80) != 0 && (r[28 + 3] & 0x0f) == 2, "servfail flags");
            check(r[28] == 0x12 && r[29] == 0x34, "servfail keeps the id");
        }
        check(ShieldPackets.parse(pkt, pkt.length, client) == null, "other destination ignored");
        byte[] tcp = packet(q, client, server, 53, 6);
        check(ShieldPackets.parse(tcp, tcp.length, server) == null, "TCP ignored");
        byte[] other = packet(q, client, server, 853, 17);
        check(ShieldPackets.parse(other, other.length, server) == null, "port 853 ignored");
        check(ShieldPackets.parse(pkt, 20, server) == null, "short packet ignored");
        byte[] ans = q.clone(); ans[0] = 0; ans[1] = 0;
        byte[] fixed = ShieldPackets.withId(ans, q);
        check(fixed[0] == 0x12 && fixed[1] == 0x34, "cached answer takes the new id");
        check(ShieldPackets.key(q).equals(ShieldPackets.key(fixed)), "cache key ignores the id");
        System.out.println(bad == 0 ? "OK" : bad + " FAILED");
        System.exit(bad == 0 ? 0 : 1);
    }
}
