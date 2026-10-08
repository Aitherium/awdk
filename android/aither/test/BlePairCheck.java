package com.aitherium.aither;

import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.Arrays;
import java.util.List;

/** Exit 0 when the nearby-device pairing rules (BlePair, BleNearby) hold. */
public class BlePairCheck {
    static int bad = 0;

    static void eq(String what, Object got, Object want) {
        if (!String.valueOf(got).equals(String.valueOf(want))) {
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
            bad++;
        }
    }

    static void ok(String what, boolean cond) {
        if (!cond) {
            System.out.println("FAIL " + what);
            bad++;
        }
    }

    static final class FakeClock implements BlePair.Clock {
        long t = 1_000_000;
        public long now() { return t; }
    }

    static final String CODE = "ABCD2345";
    static final SecureRandom RND = new SecureRandom();

    static BlePair.Advert scan(BlePair.Candidate c) {
        return BlePair.Advert.decode(c.advert());
    }

    /** Phone and device through the SAS; returns the approver (SAS shown on both). */
    static BlePair.Approver toSas(BlePair.Candidate c, String who) {
        BlePair.Approver a = new BlePair.Approver(scan(c), RND);
        byte[] reveal = a.onKey(c.onWrite(who, a.hello()));
        ok("key answer accepted (" + a.why + ")", reveal != null);
        ok("sas shown", a.onSasShown(c.onWrite(who, reveal)));
        return a;
    }

    public static void main(String[] args) {
        advert();
        handshake();
        refusals();
        rotationAndExpiry();
        limits();
        nearby();
        misc();
        if (bad == 0) System.out.println("BlePairCheck: all passed");
        System.exit(bad == 0 ? 0 : 1);
    }

    static void advert() {
        byte[] rid = new byte[BlePair.RID_LEN];
        Arrays.fill(rid, (byte) 7);
        byte[] ad = BlePair.Advert.encode(BlePair.CLASS_WATCH, rid);
        eq("advert is ten bytes (fits a legacy 31-byte advert with a 128-bit uuid)", ad.length, 10);
        eq("ten bytes, plus 18 for the field header and uuid, plus 3 flags", ad.length + 18 + 3 <= 31, true);
        BlePair.Advert d = BlePair.Advert.decode(ad);
        eq("round trip class", d.deviceClass, BlePair.CLASS_WATCH);
        eq("round trip rid", d.ridHex(), BlePair.hex(rid));
        eq("unknown class not encoded", BlePair.Advert.encode(9, rid) == null, true);
        eq("short rid not encoded", BlePair.Advert.encode(1, new byte[4]) == null, true);
        byte[] v2 = ad.clone();
        v2[0] = 2;
        eq("other version ignored", BlePair.Advert.decode(v2) == null, true);
        byte[] cls = ad.clone();
        cls[1] = 0;
        eq("unknown class ignored", BlePair.Advert.decode(cls) == null, true);
        eq("long data ignored", BlePair.Advert.decode(Arrays.copyOf(ad, 11)) == null, true);
        eq("null ignored", BlePair.Advert.decode(null) == null, true);

        BlePair.Candidate c = new BlePair.Candidate(BlePair.CLASS_WATCH, new FakeClock(), RND);
        byte[] live = c.advert();
        String text = new String(live, StandardCharsets.ISO_8859_1);
        ok("advert carries no SAS or code", c.sas() == null && !text.contains(CODE));
    }

    static void handshake() {
        FakeClock clk = new FakeClock();
        BlePair.Candidate c = new BlePair.Candidate(BlePair.CLASS_WATCH, clk, RND);
        BlePair.Approver a = toSas(c, "phone-1");
        ok("same SAS on both screens", a.sas() != null && a.sas().equals(c.sas()));
        ok("SAS looks like 123 456", a.sas().matches("[0-9]{3} [0-9]{3}"));
        eq("device is showing its SAS", c.phase(), BlePair.Candidate.Phase.SAS);
        ok("the SAS reply carries no SAS", Arrays.equals(c.reply("phone-1"), new byte[] {BlePair.R_SAS}));

        byte[] msg = a.codeMessage(CODE);
        ok("code is sealed", msg != null && msg.length == BlePair.CODE_MSG_LEN);
        ok("code never in clear over the air",
                !new String(msg, StandardCharsets.ISO_8859_1).contains(CODE));
        ok("a non-code is never sealed", a.codeMessage("abcd2345") == null && a.codeMessage("ABCD0345") == null);
        ok("device got it", BlePair.Approver.delivered(c.onWrite("phone-1", msg)));
        eq("device holds a code", c.phase(), BlePair.Candidate.Phase.CODE);
        eq("but releases nothing before its owner taps Matches", c.takeCode(), null);
        eq("Matches on a different SAS is refused", c.confirmLocal("000 000".equals(c.sas()) ? "111 111" : "000 000"), false);
        eq("still nothing", c.takeCode(), null);
        eq("Matches on the SAS shown", c.confirmLocal(c.sas()), true);
        eq("now the code", c.takeCode(), CODE);
        eq("once", c.takeCode(), null);
        eq("done", c.phase(), BlePair.Candidate.Phase.DONE);
        eq("done device stops advertising", c.advert() == null, true);

        // owner confirms first, code arrives after: same result
        BlePair.Candidate c2 = new BlePair.Candidate(BlePair.CLASS_PHONE, clk, RND);
        BlePair.Approver a2 = toSas(c2, "p");
        eq("confirm before the code", c2.confirmLocal(c2.sas()), true);
        eq("no code yet", c2.takeCode(), null);
        c2.onWrite("p", a2.codeMessage(CODE));
        eq("code after confirm", c2.takeCode(), CODE);
    }

    static void refusals() {
        FakeClock clk = new FakeClock();
        // a second phone while one is mid-handshake
        BlePair.Candidate c = new BlePair.Candidate(BlePair.CLASS_WATCH, clk, RND);
        BlePair.Approver a = new BlePair.Approver(scan(c), RND);
        c.onWrite("phone-1", a.hello());
        BlePair.Approver other = new BlePair.Approver(scan(c), RND);
        byte[] r = c.onWrite("phone-2", other.hello());
        ok("second phone told busy", r.length == 1 && r[0] == BlePair.E_BUSY);
        ok("second phone cannot send a code", c.onWrite("phone-2", new byte[BlePair.CODE_MSG_LEN])[0] == BlePair.E_BUSY);

        // a phone that changes its nonce after seeing ours (commit mismatch)
        BlePair.Candidate c3 = new BlePair.Candidate(BlePair.CLASS_WATCH, clk, RND);
        BlePair.Approver a3 = new BlePair.Approver(scan(c3), RND);
        byte[] reveal = a3.onKey(c3.onWrite("x", a3.hello()));
        reveal[5] ^= 1;
        ok("changed nonce refused", c3.onWrite("x", reveal)[0] == BlePair.E_BAD);
        ok("and no SAS shown", c3.sas() == null);
        eq("device free again", c3.phase(), BlePair.Candidate.Phase.OPEN);

        // the phone picked device A but device B answers (a stand-in): caught by the rid commitment
        BlePair.Candidate realA = new BlePair.Candidate(BlePair.CLASS_WATCH, clk, RND);
        BlePair.Candidate fakeB = new BlePair.Candidate(BlePair.CLASS_WATCH, clk, RND);
        BlePair.Approver toA = new BlePair.Approver(scan(realA), RND);
        byte[] hello = toA.hello();
        // B accepts only its own rid; an attacker relaying A's key would need A's nonce flow,
        // so model B answering with its OWN key to A's hello (rid swapped to B's)
        byte[] forB = hello.clone();
        System.arraycopy(scan(fakeB).rid, 0, forB, 1, BlePair.RID_LEN);
        eq("a key that is not the advertised one is refused", toA.onKey(fakeB.onWrite("p", forB)) == null, true);
        eq("in words", toA.why, "That isn't the device you picked.");

        // a tampered sealed code
        BlePair.Candidate c4 = new BlePair.Candidate(BlePair.CLASS_WATCH, clk, RND);
        BlePair.Approver a4 = toSas(c4, "p");
        byte[] m = a4.codeMessage(CODE);
        m[m.length - 1] ^= 1;
        ok("tampered code refused", c4.onWrite("p", m)[0] == BlePair.E_BAD);
        ok("and the session dropped", c4.sas() == null && c4.takeCode() == null);

        // a code sealed for a different handshake
        BlePair.Candidate c5 = new BlePair.Candidate(BlePair.CLASS_WATCH, clk, RND);
        BlePair.Approver a5 = toSas(c5, "p");
        BlePair.Candidate c6 = new BlePair.Candidate(BlePair.CLASS_WATCH, clk, RND);
        toSas(c6, "p");
        ok("a code from another handshake refused", c6.onWrite("p", a5.codeMessage(CODE))[0] == BlePair.E_BAD);

        // a key that is not on the curve
        BlePair.Candidate c7 = new BlePair.Candidate(BlePair.CLASS_WATCH, clk, RND);
        byte[] h = new BlePair.Approver(scan(c7), RND).hello();
        h[1 + BlePair.RID_LEN + 40] ^= 1;
        ok("off-curve key refused", c7.onWrite("p", h)[0] == BlePair.E_BAD);
        ok("off-curve decode is null", BlePair.decodePub(Arrays.copyOfRange(h, 9, 74)) == null);

        // out of order, garbage, oversize
        BlePair.Candidate c8 = new BlePair.Candidate(BlePair.CLASS_WATCH, clk, RND);
        byte[] early = new byte[BlePair.REVEAL_LEN];
        early[0] = BlePair.REVEAL;
        ok("reveal before hello", c8.onWrite("p", early)[0] == BlePair.E_ORDER);
        ok("code before SAS", c8.onWrite("p", new byte[] {BlePair.CODE})[0] == BlePair.E_ORDER);
        ok("empty write", c8.onWrite("p", new byte[0])[0] == BlePair.E_BAD);
        ok("oversize write", c8.onWrite("p", new byte[BlePair.MAX_MESSAGE + 1])[0] == BlePair.E_BAD);
        ok("unknown type", c8.onWrite("p", new byte[] {0x7F})[0] == BlePair.E_BAD);
        ok("a read with no write", c8.reply("nobody")[0] == BlePair.E_ORDER);
    }

    static void rotationAndExpiry() {
        FakeClock clk = new FakeClock();
        BlePair.Candidate c = new BlePair.Candidate(BlePair.CLASS_WATCH, clk, RND);
        BlePair.Advert first = scan(c);
        clk.t += BlePair.ROTATE_MS - 1;
        eq("same id before the rotation", scan(c).ridHex(), first.ridHex());
        clk.t += 1;
        BlePair.Advert second = scan(c);
        ok("a new id after ROTATE_MS", !second.ridHex().equals(first.ridHex()));
        ok("rotation waits for the next change", c.nextChangeIn() == BlePair.ROTATE_MS);

        // the old id still answers in its grace window
        BlePair.Approver late = new BlePair.Approver(first, RND);
        byte[] r = c.onWrite("p", late.hello());
        ok("old id answered in grace", r[0] == BlePair.R_KEY && late.onKey(r) != null);
        c.peerGone("p");

        clk.t += BlePair.GRACE_MS;
        BlePair.Approver stale = new BlePair.Approver(first, RND);
        ok("old id refused after grace", c.onWrite("p", stale.hello())[0] == BlePair.E_STALE);

        // the lifetime
        BlePair.Candidate box = new BlePair.Candidate(BlePair.CLASS_WATCH, clk, RND);
        clk.t += BlePair.LIFETIME_MS - 1;
        ok("advertising inside the box", box.advert() != null);
        clk.t += 1;
        eq("no advert after the box", box.advert() == null, true);
        eq("closed", box.phase(), BlePair.Candidate.Phase.CLOSED);
        ok("writes refused after the box", box.onWrite("p", new byte[] {1})[0] == BlePair.E_CLOSED);

        // a code waiting for Matches does not outlive the box
        BlePair.Candidate wait = new BlePair.Candidate(BlePair.CLASS_WATCH, clk, RND);
        BlePair.Approver a = toSas(wait, "p");
        wait.onWrite("p", a.codeMessage(CODE));
        eq("advert stops once a code is in", wait.advert() == null, true);
        String sas = wait.sas();
        clk.t += BlePair.LIFETIME_MS;
        eq("Matches after the box is refused", wait.confirmLocal(sas), false);
        eq("and nothing is released", wait.takeCode(), null);

        // cancel
        BlePair.Candidate cc = new BlePair.Candidate(BlePair.CLASS_WATCH, clk, RND);
        cc.cancel();
        eq("cancel closes", cc.advert() == null && cc.phase() == BlePair.Candidate.Phase.CLOSED, true);
    }

    static void limits() {
        FakeClock clk = new FakeClock();
        // a phone that goes quiet frees the device after HANDSHAKE_MS
        BlePair.Candidate c = new BlePair.Candidate(BlePair.CLASS_WATCH, clk, RND);
        c.onWrite("quiet", new BlePair.Approver(scan(c), RND).hello());
        BlePair.Approver next = new BlePair.Approver(scan(c), RND);
        ok("busy while it is fresh", c.onWrite("next", next.hello())[0] == BlePair.E_BUSY);
        clk.t += BlePair.HANDSHAKE_MS + 1;
        next = new BlePair.Approver(scan(c), RND);
        ok("free after the handshake window", c.onWrite("next", next.hello())[0] == BlePair.R_KEY);

        // disconnect mid-handshake frees it; after the code it does not drop the code
        BlePair.Candidate g = new BlePair.Candidate(BlePair.CLASS_WATCH, clk, RND);
        toSas(g, "p");
        eq("Matches is taken", g.confirmLocal(g.sas()), true);
        g.peerGone("p");
        ok("gone phone: SAS cleared", g.sas() == null);
        BlePair.Approver b = toSas(g, "q");
        g.onWrite("q", b.codeMessage(CODE));
        eq("an earlier Matches does not carry over to a new phone", g.takeCode(), null);
        g.peerGone("q");
        eq("a delivered code survives the disconnect", g.phase(), BlePair.Candidate.Phase.CODE);
        eq("and needs this SAS's Matches", g.confirmLocal(g.sas()) && CODE.equals(g.takeCode()), true);

        // MAX_HANDSHAKES key exchanges per opening, then closed
        BlePair.Candidate m = new BlePair.Candidate(BlePair.CLASS_WATCH, clk, RND);
        for (int i = 0; i < BlePair.MAX_HANDSHAKES; i++) {
            BlePair.Approver x = new BlePair.Approver(scan(m), RND);
            ok("try " + (i + 1) + " keyed", m.onWrite("p" + i, x.hello())[0] == BlePair.R_KEY);
            m.peerGone("p" + i);
        }
        BlePair.Approver x = new BlePair.Approver(scan(m), RND);
        ok("one more try closes it", m.onWrite("z", x.hello())[0] == BlePair.E_CLOSED);
        eq("closed after too many tries", m.phase(), BlePair.Candidate.Phase.CLOSED);
        eq("in words", m.why(), "Too many tries. Start again on the device.");

        // junk does not spend tries
        BlePair.Candidate j = new BlePair.Candidate(BlePair.CLASS_WATCH, clk, RND);
        byte[] junk = new BlePair.Approver(scan(j), RND).hello();
        junk[1] ^= 1; // wrong rid
        for (int i = 0; i < 50; i++) j.onWrite("j" + i, junk);
        ok("stale junk spends no tries", j.onWrite("real", new BlePair.Approver(scan(j), RND).hello())[0]
                == BlePair.R_KEY);

        // answers are kept for a bounded number of writers
        BlePair.Candidate k = new BlePair.Candidate(BlePair.CLASS_WATCH, clk, RND);
        for (int i = 0; i < 100; i++) k.onWrite("w" + i, new byte[] {0x7F});
        ok("oldest writer's answer dropped", k.reply("w0")[0] == BlePair.E_ORDER);
        ok("newest kept", k.reply("w99")[0] == BlePair.E_BAD);
    }

    static byte[] ad(int cls, int seed) {
        byte[] rid = new byte[BlePair.RID_LEN];
        rid[0] = (byte) seed;
        rid[1] = (byte) (seed >> 8);
        return BlePair.Advert.encode(cls, rid);
    }

    static void nearby() {
        BleNearby n = new BleNearby();
        long t = 5_000_000;
        ok("a real advert is listed", n.seen(ad(1, 1), -60, null, t));
        ok("the same id again only refreshes", !n.seen(ad(1, 1), -50, null, t + 10));
        eq("refreshed rssi", n.list(t + 10).get(0).rssi, -50);
        ok("junk is not listed", !n.seen(new byte[] {1, 1, 2}, -40, null, t));
        ok("too faint is not listed", !n.seen(ad(2, 2), BleNearby.MIN_RSSI - 1, null, t));
        n.seen(ad(2, 3), -40, null, t + 20);
        List<BleNearby.Device> l = n.list(t + 20);
        eq("closest first", l.get(0).label(), "A phone · right here");
        eq("then", l.get(1).label(), "A watch · right here");
        eq("stale entries drop", n.list(t + 20 + BleNearby.STALE_MS + 1).size(), 0);

        // capacity: a stronger newcomer replaces the weakest, a weaker one is not listed
        BleNearby cap = new BleNearby();
        for (int i = 0; i < BleNearby.MAX; i++) cap.seen(ad(1, 100 + i), -80 + i, null, t);
        eq("full", cap.list(t).size(), BleNearby.MAX);
        ok("weaker newcomer ignored", !cap.seen(ad(1, 500), -85, null, t));
        ok("stronger newcomer listed", cap.seen(ad(1, 501), -45, null, t));
        eq("still capped", cap.list(t).size(), BleNearby.MAX);
        ok("the weakest went", cap.get(BlePair.Advert.decode(ad(1, 100)).ridHex(), t) == null);

        // a flood of fresh ids
        BleNearby f = new BleNearby();
        ok("real device first", f.seen(ad(1, 9999), -50, null, t));
        int listed = 1;
        for (int i = 0; i < 200; i++) if (f.seen(ad(1, i), -30, null, t + i)) listed++;
        ok("flood bounded by the window", listed <= BleNearby.NEW_PER_WINDOW);
        ok("flood is flagged", f.flooded(t + 200));
        ok("never past MAX", f.list(t + 200).size() <= BleNearby.MAX);
        ok("the flag clears", !f.flooded(t + 200 + BleNearby.WINDOW_MS * 2));
        ok("no background scan: the scan is time-boxed", BleNearby.SCAN_MS <= 60_000);
    }

    static void misc() {
        eq("watch node class", BlePair.nodeClass(BlePair.CLASS_WATCH), "watch");
        eq("phone node class", BlePair.nodeClass(BlePair.CLASS_PHONE), "phone");
        eq("hostname", BlePair.hostname("Pixel Watch 4", "watch"), "pixel-watch-4");
        eq("hostname fallback", BlePair.hostname("__", "watch"), "watch");
        ok("pairing alphabet", BlePair.validCode("ABCDEFGH") && !BlePair.validCode("ABCDEFG1")
                && !BlePair.validCode("ABCDEFGHJ") && !BlePair.validCode(null));
        ok("rid is a commitment to the key", Arrays.equals(BlePair.rid(new byte[] {1}), BlePair.rid(new byte[] {1}))
                && !Arrays.equals(BlePair.rid(new byte[] {1}), BlePair.rid(new byte[] {2})));
        ok("time box is three minutes or less", BlePair.LIFETIME_MS <= 180_000);
        ok("a man in the middle gets few guesses", BlePair.MAX_HANDSHAKES <= 5);
    }
}
