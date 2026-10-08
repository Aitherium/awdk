package com.aitherium.aither;

import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.security.KeyFactory;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.MessageDigest;
import java.security.PrivateKey;
import java.security.PublicKey;
import java.security.SecureRandom;
import java.security.interfaces.ECPublicKey;
import java.security.spec.ECFieldFp;
import java.security.spec.ECGenParameterSpec;
import java.security.spec.ECParameterSpec;
import java.security.spec.ECPoint;
import java.security.spec.ECPublicKeySpec;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;

import javax.crypto.Cipher;
import javax.crypto.KeyAgreement;
import javax.crypto.Mac;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;

/**
 * Adding a device that is in Bluetooth range ("Nearby devices"), as pure Java so
 * test/BlePairCheck.java runs every rule on a desktop JVM. Android's radio code is
 * BleCandidate (the device being added: a watch, or a new phone) and NearbyDevicesActivity
 * (the member's phone that approves it); neither decides anything this class does not.
 *
 * <p><b>The advert</b> (legacy, 31 bytes): flags plus one service-data field under
 * {@link #SERVICE}, ten bytes: version, device class, and an 8-byte request id. No name, no
 * account, no code, no SAS. The request id is the first 8 bytes of SHA-256 over the
 * candidate's ephemeral P-256 public key, so it doubles as a commitment to that key; the key
 * (and so the id) is replaced every {@link #ROTATE_MS}, and the whole thing ends after
 * {@link #LIFETIME_MS} or when the "add device" screen closes.
 *
 * <p><b>The handshake</b> over one GATT write characteristic ({@link #RX}) and one read
 * characteristic ({@link #TX}), commit-then-reveal like Bluetooth's numeric comparison:
 * <ol>
 * <li>phone writes HELLO = rid, its key P, commit = H(P, Np)</li>
 * <li>device answers its key C (checked against rid) and a fresh nonce Nc</li>
 * <li>phone writes REVEAL = Np; device checks the commit</li>
 * <li>both screens show the same 6-digit SAS = H(transcript); the person checks them</li>
 * <li>phone (a grown-up's account, refused for a child by Identity) mints the single-use
 *     workspace pairing code and writes CODE = AES-GCM(code) under ECDH(C, P) bound to the
 *     transcript; the device decrypts it and, once its owner also tapped "Matches", confirms
 *     it with Identity itself (/v1/nodes/pairing/confirm) over its own connection.</li>
 * </ol>
 * A man in the middle has to fix its own nonce or key before it learns the other side's, so
 * it matches both screens' SAS with probability 1e-6 per try, and a device takes at most
 * {@link #MAX_HANDSHAKES} key exchanges per opening. The code never crosses the air in clear.
 */
final class BlePair {
    private BlePair() {}

    /** The Aither pairing service (random 128-bit UUID) and its two characteristics. */
    static final String SERVICE = "a17e5001-6b7d-4c3e-9f2a-1d0e8b4c7a51";
    /** The phone writes here (HELLO, REVEAL, CODE). */
    static final String RX = "a17e5002-6b7d-4c3e-9f2a-1d0e8b4c7a51";
    /** The phone reads the device's answer to its last write here. */
    static final String TX = "a17e5003-6b7d-4c3e-9f2a-1d0e8b4c7a51";

    static final int VERSION = 1;
    static final int CLASS_WATCH = 1;
    static final int CLASS_PHONE = 2;

    static final int RID_LEN = 8;
    static final int PUB_LEN = 65; // uncompressed P-256: 04 || X || Y
    static final int NONCE_LEN = 16;
    static final int HASH_LEN = 32;
    static final int IV_LEN = 12;
    static final int TAG_LEN = 16;
    static final int CODE_LEN = 8;
    static final int ADVERT_LEN = 2 + RID_LEN;

    /** How long one "add device" opening advertises at most (also the advertiser's own box). */
    static final long LIFETIME_MS = 180_000;
    /** A new key, so a new request id, this often. */
    static final long ROTATE_MS = 60_000;
    /** The id just replaced is still answered this long (a phone that scanned it is connecting). */
    static final long GRACE_MS = 20_000;
    /** One phone's handshake, from HELLO to CODE. Past it, the device is free for another. */
    static final long HANDSHAKE_MS = 60_000;
    /** Key exchanges one opening allows: a man in the middle gets this many SAS guesses. */
    static final int MAX_HANDSHAKES = 5;
    /** No message is longer (the GATT attribute limit). */
    static final int MAX_MESSAGE = 512;
    /** Peers a device keeps an answer for (any one nearby device may write; this is bounded). */
    static final int MAX_PEERS = 8;

    static final byte HELLO = 0x01;
    static final byte REVEAL = 0x02;
    static final byte CODE = 0x03;
    static final byte R_KEY = (byte) 0x81;
    static final byte R_SAS = (byte) 0x82;
    static final byte R_GOT = (byte) 0x83;
    static final byte E_BAD = (byte) 0xE0;
    static final byte E_STALE = (byte) 0xE1;
    static final byte E_BUSY = (byte) 0xE2;
    static final byte E_CLOSED = (byte) 0xE3;
    static final byte E_ORDER = (byte) 0xE4;

    static final int HELLO_LEN = 1 + RID_LEN + PUB_LEN + HASH_LEN;
    static final int KEY_LEN = 1 + PUB_LEN + NONCE_LEN;
    static final int REVEAL_LEN = 1 + NONCE_LEN;
    static final int CODE_MSG_LEN = 1 + IV_LEN + CODE_LEN + TAG_LEN;

    /** Identity's pairing alphabet (identity_nodes._PAIRING_ALPHABET): no 0/O/1/I/L. */
    static final String ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789";

    // ------------------------------------------------------------------ the advert

    /** What one scanned advert says: a device class and a request id, nothing else. */
    static final class Advert {
        final int deviceClass;
        final byte[] rid;

        Advert(int deviceClass, byte[] rid) {
            this.deviceClass = deviceClass;
            this.rid = rid.clone();
        }

        /** The ten service-data bytes, or null for a class or id this version does not send. */
        static byte[] encode(int deviceClass, byte[] rid) {
            if (!knownClass(deviceClass) || rid == null || rid.length != RID_LEN) return null;
            byte[] out = new byte[ADVERT_LEN];
            out[0] = (byte) VERSION;
            out[1] = (byte) deviceClass;
            System.arraycopy(rid, 0, out, 2, RID_LEN);
            return out;
        }

        /** A well-formed advert, or null (wrong length, version or class: never guessed at). */
        static Advert decode(byte[] data) {
            if (data == null || data.length != ADVERT_LEN) return null;
            if ((data[0] & 0xFF) != VERSION) return null;
            int cls = data[1] & 0xFF;
            if (!knownClass(cls)) return null;
            return new Advert(cls, Arrays.copyOfRange(data, 2, ADVERT_LEN));
        }

        String ridHex() { return hex(rid); }
    }

    static boolean knownClass(int c) {
        return c == CLASS_WATCH || c == CLASS_PHONE;
    }

    /** Identity's node_class for a device class. */
    static String nodeClass(int c) {
        return c == CLASS_WATCH ? "watch" : c == CLASS_PHONE ? "phone" : "";
    }

    /** How a nearby device is named on the phone: its kind, never anything it chose. */
    static String label(int c) {
        return c == CLASS_WATCH ? "A watch" : c == CLASS_PHONE ? "A phone" : "A device";
    }

    // ------------------------------------------------------------------ crypto

    private static volatile ECParameterSpec p256;

    static KeyPair newKey(SecureRandom rnd) {
        try {
            KeyPairGenerator g = KeyPairGenerator.getInstance("EC");
            g.initialize(new ECGenParameterSpec("secp256r1"), rnd);
            KeyPair kp = g.generateKeyPair();
            if (p256 == null) p256 = ((ECPublicKey) kp.getPublic()).getParams();
            return kp;
        } catch (Exception e) {
            throw new IllegalStateException("no P-256 on this device", e);
        }
    }

    private static ECParameterSpec params() {
        if (p256 == null) newKey(new SecureRandom());
        return p256;
    }

    /** 04 || X || Y, each coordinate 32 bytes big-endian. */
    static byte[] encodePub(PublicKey k) {
        ECPoint w = ((ECPublicKey) k).getW();
        byte[] out = new byte[PUB_LEN];
        out[0] = 0x04;
        fixed(w.getAffineX(), out, 1);
        fixed(w.getAffineY(), out, 33);
        return out;
    }

    private static void fixed(BigInteger v, byte[] out, int at) {
        byte[] b = v.toByteArray();
        int n = Math.min(b.length, 32);
        System.arraycopy(b, b.length - n, out, at + 32 - n, n);
    }

    /** A peer's key, or null unless it is an uncompressed point ON P-256 (no invalid-curve keys). */
    static PublicKey decodePub(byte[] b) {
        if (b == null || b.length != PUB_LEN || b[0] != 0x04) return null;
        try {
            ECParameterSpec ps = params();
            BigInteger p = ((ECFieldFp) ps.getCurve().getField()).getP();
            BigInteger x = new BigInteger(1, Arrays.copyOfRange(b, 1, 33));
            BigInteger y = new BigInteger(1, Arrays.copyOfRange(b, 33, 65));
            if (x.signum() == 0 && y.signum() == 0) return null;
            if (x.compareTo(p) >= 0 || y.compareTo(p) >= 0) return null;
            BigInteger lhs = y.modPow(BigInteger.valueOf(2), p);
            BigInteger rhs = x.modPow(BigInteger.valueOf(3), p)
                    .add(ps.getCurve().getA().multiply(x)).add(ps.getCurve().getB()).mod(p);
            if (!lhs.equals(rhs)) return null;
            return KeyFactory.getInstance("EC").generatePublic(new ECPublicKeySpec(new ECPoint(x, y), ps));
        } catch (Exception e) {
            return null;
        }
    }

    static byte[] sha256(String label, byte[]... parts) {
        try {
            MessageDigest d = MessageDigest.getInstance("SHA-256");
            d.update(label.getBytes(StandardCharsets.US_ASCII));
            for (byte[] p : parts) {
                d.update((byte) (p.length >>> 8));
                d.update((byte) p.length);
                d.update(p);
            }
            return d.digest();
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /** The request id a key advertises: its commitment. */
    static byte[] rid(byte[] pub) {
        return Arrays.copyOf(sha256("aither-ble-rid-v1", pub), RID_LEN);
    }

    static byte[] commit(byte[] pubP, byte[] np) {
        return sha256("aither-ble-commit-v1", pubP, np);
    }

    static byte[] transcript(int cls, byte[] rid, byte[] pubC, byte[] pubP, byte[] nc, byte[] np) {
        return sha256("aither-ble-transcript-v1", new byte[] {(byte) cls}, rid, pubC, pubP, nc, np);
    }

    /** Six digits both screens show, as "123 456". Never sent over the air. */
    static String sas(byte[] transcript) {
        byte[] h = sha256("aither-ble-sas-v1", transcript);
        long v = ((h[0] & 0xFFL) << 24) | ((h[1] & 0xFFL) << 16) | ((h[2] & 0xFFL) << 8) | (h[3] & 0xFFL);
        String s = String.format(Locale.ROOT, "%06d", v % 1_000_000L);
        return s.substring(0, 3) + " " + s.substring(3);
    }

    /** HKDF-SHA256(ikm = ECDH secret, salt = transcript, info = label): the code's key. */
    static byte[] sessionKey(PrivateKey mine, PublicKey theirs, byte[] transcript) {
        try {
            KeyAgreement ka = KeyAgreement.getInstance("ECDH");
            ka.init(mine);
            ka.doPhase(theirs, true);
            byte[] z = ka.generateSecret();
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(transcript, "HmacSHA256"));
            byte[] prk = mac.doFinal(z);
            Arrays.fill(z, (byte) 0);
            mac.init(new SecretKeySpec(prk, "HmacSHA256"));
            mac.update("aither-ble-code-v1".getBytes(StandardCharsets.US_ASCII));
            mac.update((byte) 1);
            return mac.doFinal();
        } catch (Exception e) {
            return null;
        }
    }

    /** CODE || iv || AES-256-GCM(code) with the transcript as associated data. */
    static byte[] seal(byte[] key, byte[] transcript, String code, SecureRandom rnd) {
        if (key == null || !validCode(code)) return null;
        try {
            byte[] iv = new byte[IV_LEN];
            rnd.nextBytes(iv);
            Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
            c.init(Cipher.ENCRYPT_MODE, new SecretKeySpec(key, "AES"), new GCMParameterSpec(TAG_LEN * 8, iv));
            c.updateAAD(transcript);
            byte[] ct = c.doFinal(code.getBytes(StandardCharsets.US_ASCII));
            byte[] out = new byte[1 + IV_LEN + ct.length];
            out[0] = CODE;
            System.arraycopy(iv, 0, out, 1, IV_LEN);
            System.arraycopy(ct, 0, out, 1 + IV_LEN, ct.length);
            return out;
        } catch (Exception e) {
            return null;
        }
    }

    /** The code a CODE message carries, or null (wrong key, tampered, malformed). */
    static String open(byte[] key, byte[] transcript, byte[] msg) {
        if (key == null || msg == null || msg.length != CODE_MSG_LEN || msg[0] != CODE) return null;
        try {
            Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
            c.init(Cipher.DECRYPT_MODE, new SecretKeySpec(key, "AES"),
                    new GCMParameterSpec(TAG_LEN * 8, Arrays.copyOfRange(msg, 1, 1 + IV_LEN)));
            c.updateAAD(transcript);
            String code = new String(c.doFinal(msg, 1 + IV_LEN, msg.length - 1 - IV_LEN), StandardCharsets.US_ASCII);
            return validCode(code) ? code : null;
        } catch (Exception e) {
            return null;
        }
    }

    /** Eight characters of Identity's pairing alphabet, nothing else. */
    static boolean validCode(String code) {
        if (code == null || code.length() != CODE_LEN) return false;
        for (int i = 0; i < code.length(); i++) {
            if (ALPHABET.indexOf(code.charAt(i)) < 0) return false;
        }
        return true;
    }

    /** A DNS-safe hostname for Identity (NodeLink.hostname's rule), or the fallback. */
    static String hostname(String model, String fallback) {
        String h = (model == null ? "" : model).toLowerCase(Locale.ROOT)
                .replaceAll("[^a-z0-9-]+", "-").replaceAll("^-+|-+$", "");
        if (h.length() > 63) h = h.substring(0, 63).replaceAll("-+$", "");
        return h.isEmpty() ? fallback : h;
    }

    static String hex(byte[] b) {
        StringBuilder s = new StringBuilder(b.length * 2);
        for (byte x : b) s.append(String.format(Locale.ROOT, "%02x", x & 0xFF));
        return s.toString();
    }

    private static byte[] cat(byte first, byte[]... parts) {
        int n = 1;
        for (byte[] p : parts) n += p.length;
        byte[] out = new byte[n];
        out[0] = first;
        int at = 1;
        for (byte[] p : parts) {
            System.arraycopy(p, 0, out, at, p.length);
            at += p.length;
        }
        return out;
    }

    private static byte[] one(byte b) {
        return new byte[] {b};
    }

    interface Clock {
        long now();
    }

    // ------------------------------------------------------------------ the device being added

    /**
     * The candidate's side: what it advertises, what it answers each nearby writer, and when
     * it may hand the received code to Identity. One phone at a time; everyone else is told
     * BUSY. The code is released only when the owner of THIS device tapped "Matches" on the
     * SAS this device is showing AND a code arrived under that same handshake.
     */
    static final class Candidate {
        enum Phase { OPEN, KEYED, SAS, CODE, DONE, CLOSED }

        private final int cls;
        private final Clock clock;
        private final SecureRandom rnd;
        private final long started;

        private KeyPair cur;
        private byte[] curPub;
        private byte[] curRid;
        private long rotatedAt;
        private KeyPair prev;
        private byte[] prevPub;
        private byte[] prevRid;
        private long prevUntil;

        private Phase phase = Phase.OPEN;
        private String peer;
        private long peerSince;
        private KeyPair epoch;
        private byte[] epochPub;
        private byte[] epochRid;
        private byte[] pubP;
        private byte[] commitP;
        private byte[] nc;
        private byte[] t;
        private byte[] key;
        private String sas;
        private String code;
        private boolean localOk;
        private int handshakes;
        private String why = "";

        /** Each writer's last answer, for its read; bounded, oldest dropped. */
        private final Map<String, byte[]> replies = new LinkedHashMap<String, byte[]>() {
            @Override
            protected boolean removeEldestEntry(Map.Entry<String, byte[]> e) {
                return size() > MAX_PEERS;
            }
        };

        Candidate(int deviceClass, Clock clock, SecureRandom rnd) {
            if (!knownClass(deviceClass)) throw new IllegalArgumentException("device class");
            this.cls = deviceClass;
            this.clock = clock;
            this.rnd = rnd;
            this.started = clock.now();
            rotate(started);
        }

        private void rotate(long now) {
            prev = cur;
            prevPub = curPub;
            prevRid = curRid;
            prevUntil = now + GRACE_MS;
            cur = newKey(rnd);
            curPub = encodePub(cur.getPublic());
            curRid = rid(curPub);
            rotatedAt = now;
        }

        /** Still taking phones: inside its lifetime, not cancelled, not done. */
        synchronized boolean open() {
            long now = clock.now();
            if (phase != Phase.CLOSED && phase != Phase.DONE && now - started >= LIFETIME_MS) {
                close("Time's up. Start again on the device.");
            }
            if (peer != null && (phase == Phase.KEYED || phase == Phase.SAS)
                    && now - peerSince > HANDSHAKE_MS) {
                reset(); // that phone went quiet: free the device for another
            }
            return phase != Phase.CLOSED && phase != Phase.DONE;
        }

        /** What to advertise now (a new id once ROTATE_MS passed), or null once it should stop. */
        synchronized byte[] advert() {
            if (!open() || phase == Phase.CODE) return null;
            long now = clock.now();
            if (now - rotatedAt >= ROTATE_MS) rotate(now);
            return Advert.encode(cls, curRid);
        }

        /** Milliseconds until the advert should be refreshed. */
        synchronized long nextChangeIn() {
            return Math.max(0, ROTATE_MS - (clock.now() - rotatedAt));
        }

        /** One GATT write from {@code who} (its connection's address): the answer it may read. */
        synchronized byte[] onWrite(String who, byte[] msg) {
            byte[] r = handle(who == null ? "" : who, msg);
            replies.put(who == null ? "" : who, r);
            return r;
        }

        /** The answer to {@code who}'s last write (one byte E_ORDER when it wrote nothing). */
        synchronized byte[] reply(String who) {
            byte[] r = replies.get(who == null ? "" : who);
            return r == null ? one(E_ORDER) : r.clone();
        }

        private byte[] handle(String who, byte[] msg) {
            if (!open()) return one(E_CLOSED);
            if (msg == null || msg.length == 0 || msg.length > MAX_MESSAGE) return one(E_BAD);
            if (peer != null && !peer.equals(who)) return one(E_BUSY);
            switch (msg[0]) {
                case HELLO: return hello(who, msg);
                case REVEAL: return reveal(msg);
                case CODE: return code(msg);
                default: return one(E_BAD);
            }
        }

        private byte[] hello(String who, byte[] msg) {
            if (peer != null || phase != Phase.OPEN) return one(E_ORDER);
            if (msg.length != HELLO_LEN) return one(E_BAD);
            long now = clock.now();
            byte[] r = Arrays.copyOfRange(msg, 1, 1 + RID_LEN);
            KeyPair kp;
            byte[] pub;
            if (MessageDigest.isEqual(r, curRid)) {
                kp = cur;
                pub = curPub;
            } else if (prevRid != null && now < prevUntil && MessageDigest.isEqual(r, prevRid)) {
                kp = prev;
                pub = prevPub;
            } else {
                return one(E_STALE);
            }
            byte[] p = Arrays.copyOfRange(msg, 1 + RID_LEN, 1 + RID_LEN + PUB_LEN);
            if (decodePub(p) == null) return one(E_BAD);
            if (++handshakes > MAX_HANDSHAKES) {
                close("Too many tries. Start again on the device.");
                return one(E_CLOSED);
            }
            peer = who;
            peerSince = now;
            epoch = kp;
            epochPub = pub;
            epochRid = r;
            pubP = p;
            commitP = Arrays.copyOfRange(msg, 1 + RID_LEN + PUB_LEN, HELLO_LEN);
            nc = new byte[NONCE_LEN];
            rnd.nextBytes(nc);
            phase = Phase.KEYED;
            return cat(R_KEY, pub, nc);
        }

        private byte[] reveal(byte[] msg) {
            if (phase != Phase.KEYED) return one(E_ORDER);
            if (msg.length != REVEAL_LEN) {
                reset();
                return one(E_BAD);
            }
            byte[] np = Arrays.copyOfRange(msg, 1, REVEAL_LEN);
            if (!MessageDigest.isEqual(commit(pubP, np), commitP)) {
                reset(); // a phone that changed its nonce after seeing ours: never shown a SAS
                return one(E_BAD);
            }
            t = transcript(cls, epochRid, epochPub, pubP, nc, np);
            key = sessionKey(epoch.getPrivate(), decodePub(pubP), t);
            if (key == null) {
                reset();
                return one(E_BAD);
            }
            sas = BlePair.sas(t);
            phase = Phase.SAS;
            return one(R_SAS);
        }

        private byte[] code(byte[] msg) {
            if (phase != Phase.SAS) return one(E_ORDER);
            String c = BlePair.open(key, t, msg);
            if (c == null) {
                reset();
                return one(E_BAD);
            }
            code = c;
            phase = Phase.CODE;
            return one(R_GOT);
        }

        /** Forget the current phone (its handshake failed, timed out or it left). */
        private void reset() {
            peer = null;
            epoch = null;
            epochPub = null;
            epochRid = null;
            pubP = null;
            commitP = null;
            nc = null;
            t = null;
            if (key != null) Arrays.fill(key, (byte) 0);
            key = null;
            sas = null;
            code = null;
            localOk = false;
            if (phase != Phase.CLOSED && phase != Phase.DONE) phase = Phase.OPEN;
        }

        private void close(String reason) {
            reset();
            phase = Phase.CLOSED;
            why = reason;
        }

        /** The phone {@code who} disconnected: mid-handshake it frees the device. */
        synchronized void peerGone(String who) {
            replies.remove(who == null ? "" : who);
            if (peer != null && peer.equals(who) && (phase == Phase.KEYED || phase == Phase.SAS)) reset();
        }

        /** The SAS to show, or null while no phone has finished the key exchange. */
        synchronized String sas() {
            open();
            return sas;
        }

        /** The owner tapped "Matches" on {@code shown}; true only if it is still this SAS. */
        synchronized boolean confirmLocal(String shown) {
            if (!open() || sas == null || shown == null || !sas.equals(shown)) return false;
            if (phase != Phase.SAS && phase != Phase.CODE) return false;
            localOk = true;
            return true;
        }

        /** The code, once: only when it arrived AND the owner confirmed the same SAS. */
        synchronized String takeCode() {
            if (!open() || phase != Phase.CODE || !localOk || code == null) return null;
            String c = code;
            reset();
            phase = Phase.DONE;
            return c;
        }

        synchronized void cancel() {
            if (phase != Phase.DONE) close("Cancelled.");
        }

        synchronized Phase phase() {
            open();
            return phase;
        }

        synchronized String why() { return why; }

        synchronized long startedAt() { return started; }
    }

    // ------------------------------------------------------------------ the approving phone

    /** The member phone's side of one handshake with one scanned advert. */
    static final class Approver {
        private final Advert ad;
        private final SecureRandom rnd;
        private final KeyPair mine;
        private final byte[] pub;
        private final byte[] np;
        private byte[] t;
        private byte[] key;
        private String sas;
        String why = "";

        Approver(Advert ad, SecureRandom rnd) {
            this.ad = ad;
            this.rnd = rnd;
            this.mine = newKey(rnd);
            this.pub = encodePub(mine.getPublic());
            this.np = new byte[NONCE_LEN];
            rnd.nextBytes(np);
        }

        byte[] hello() {
            return cat(HELLO, ad.rid, pub, commit(pub, np));
        }

        /** The device's key answer: the REVEAL to write next, or null with {@link #why}. */
        byte[] onKey(byte[] reply) {
            if (reply == null || reply.length == 0) { why = "No answer."; return null; }
            if (reply.length == 1) { why = refusal(reply[0]); return null; }
            if (reply.length != KEY_LEN || reply[0] != R_KEY) { why = "That device answered oddly."; return null; }
            byte[] pubC = Arrays.copyOfRange(reply, 1, 1 + PUB_LEN);
            if (!MessageDigest.isEqual(rid(pubC), ad.rid)) {
                why = "That isn't the device you picked.";
                return null;
            }
            PublicKey theirs = decodePub(pubC);
            if (theirs == null) { why = "That device answered oddly."; return null; }
            byte[] nc = Arrays.copyOfRange(reply, 1 + PUB_LEN, KEY_LEN);
            t = transcript(ad.deviceClass, ad.rid, pubC, pub, nc, np);
            key = sessionKey(mine.getPrivate(), theirs, t);
            if (key == null) { why = "That device answered oddly."; return null; }
            sas = BlePair.sas(t);
            return cat(REVEAL, np);
        }

        /** The device accepted our reveal and is showing its SAS. */
        boolean onSasShown(byte[] reply) {
            if (reply != null && reply.length == 1 && reply[0] == R_SAS) return true;
            why = reply != null && reply.length == 1 ? refusal(reply[0]) : "That device answered oddly.";
            sas = null;
            return false;
        }

        String sas() { return sas; }

        /** The sealed CODE message, or null (no SAS yet, or not a pairing code). */
        byte[] codeMessage(String code) {
            if (sas == null) return null;
            return seal(key, t, code, rnd);
        }

        static boolean delivered(byte[] reply) {
            return reply != null && reply.length == 1 && reply[0] == R_GOT;
        }

        static String refusal(byte e) {
            switch (e) {
                case E_BUSY: return "Another phone is adding that device right now.";
                case E_STALE: return "That device moved on. Pick it again from the list.";
                case E_CLOSED: return "That device stopped looking. Start again on it.";
                case E_ORDER: return "Out of step with that device. Pick it again.";
                default: return "That device refused.";
            }
        }
    }
}
