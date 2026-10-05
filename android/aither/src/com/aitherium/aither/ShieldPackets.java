package com.aitherium.aither;

/**
 * Family Shield's packet arithmetic, with no Android types so a plain JVM can check it
 * (test/ShieldPacketsCheck.java). ShieldVpnService routes ONE address (the fake DNS server)
 * through its tun, so every packet it reads should be IPv4 UDP to port 53 at that address;
 * anything else is ignored.
 */
final class ShieldPackets {
    private ShieldPackets() {}

    /** A DNS query lifted out of an IPv4/UDP packet: who asked, from which port, what. */
    static final class Query {
        final byte[] clientIp;
        final int clientPort;
        final byte[] serverIp;
        final byte[] dns;

        Query(byte[] clientIp, int clientPort, byte[] serverIp, byte[] dns) {
            this.clientIp = clientIp;
            this.clientPort = clientPort;
            this.serverIp = serverIp;
            this.dns = dns;
        }
    }

    static int u16(byte[] b, int off) {
        return ((b[off] & 0xff) << 8) | (b[off + 1] & 0xff);
    }

    /** The DNS query in {@code pkt[0..len)} if it is IPv4 UDP to {@code dnsIp}:53, else null. */
    static Query parse(byte[] pkt, int len, byte[] dnsIp) {
        if (len < 28 || ((pkt[0] >> 4) & 0xf) != 4) return null;
        int ihl = (pkt[0] & 0xf) * 4;
        if (ihl < 20 || len < ihl + 8 || (pkt[9] & 0xff) != 17) return null;
        int total = u16(pkt, 2);
        if (total > len || total < ihl + 8) return null;
        if ((u16(pkt, 6) & 0x3fff) != 0) return null; // a fragment: DNS queries never are
        for (int i = 0; i < 4; i++) if (pkt[16 + i] != dnsIp[i]) return null;
        if (u16(pkt, ihl + 2) != 53) return null;
        int udpLen = u16(pkt, ihl + 4);
        if (udpLen < 8 + 12 || ihl + udpLen > total) return null; // 12 = a DNS header
        byte[] client = new byte[4];
        byte[] server = new byte[4];
        System.arraycopy(pkt, 12, client, 0, 4);
        System.arraycopy(pkt, 16, server, 0, 4);
        byte[] dns = new byte[udpLen - 8];
        System.arraycopy(pkt, ihl + 8, dns, 0, dns.length);
        return new Query(client, u16(pkt, ihl), server, dns);
    }

    /** The IPv4/UDP packet carrying {@code dns} back from the fake server to the asker. */
    static byte[] reply(Query q, byte[] dns) {
        int total = 20 + 8 + dns.length;
        byte[] p = new byte[total];
        p[0] = 0x45;
        p[2] = (byte) (total >> 8);
        p[3] = (byte) total;
        p[6] = 0x40; // don't fragment
        p[8] = 64; // TTL
        p[9] = 17; // UDP
        System.arraycopy(q.serverIp, 0, p, 12, 4);
        System.arraycopy(q.clientIp, 0, p, 16, 4);
        int sum = checksum(p, 0, 20);
        p[10] = (byte) (sum >> 8);
        p[11] = (byte) sum;
        p[20] = 0;
        p[21] = 53;
        p[22] = (byte) (q.clientPort >> 8);
        p[23] = (byte) q.clientPort;
        int udpLen = 8 + dns.length;
        p[24] = (byte) (udpLen >> 8);
        p[25] = (byte) udpLen;
        // UDP checksum 0 = none, which IPv4 allows
        System.arraycopy(dns, 0, p, 28, dns.length);
        return p;
    }

    /** RFC 1071 ones'-complement checksum. */
    static int checksum(byte[] b, int off, int len) {
        long sum = 0;
        for (int i = 0; i + 1 < len; i += 2) sum += u16(b, off + i);
        if ((len & 1) == 1) sum += (b[off + len - 1] & 0xff) << 8;
        while ((sum >> 16) != 0) sum = (sum & 0xffff) + (sum >> 16);
        return (int) (~sum & 0xffff);
    }

    /** SERVFAIL for {@code query}: same id and question; the filter could not answer. */
    static byte[] servfail(byte[] query) {
        byte[] r = query.clone();
        r[2] = (byte) ((r[2] & 0x79) | 0x80); // QR=1, keep opcode and RD
        r[3] = (byte) 0x82; // RA=1, RCODE=2
        return r;
    }

    /** The cache key: the query without its 2-byte id. */
    static String key(byte[] query) {
        return new String(query, 2, query.length - 2, java.nio.charset.StandardCharsets.ISO_8859_1);
    }

    /** {@code answer} with its id set to {@code query}'s. */
    static byte[] withId(byte[] answer, byte[] query) {
        byte[] r = answer.clone();
        r[0] = query[0];
        r[1] = query[1];
        return r;
    }
}
