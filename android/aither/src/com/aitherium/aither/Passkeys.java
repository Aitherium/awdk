package com.aitherium.aither;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.KeyFactory;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.MessageDigest;
import java.security.PrivateKey;
import java.security.SecureRandom;
import java.security.Signature;
import java.security.interfaces.ECPublicKey;
import java.security.spec.ECGenParameterSpec;
import java.security.spec.PKCS8EncodedKeySpec;
import java.util.Base64;
import java.util.List;

/**
 * The WebAuthn authenticator the Aither passkey provider runs (S4, #16698). Pure Java
 * (java.security + org.json), so the same code is checked on a desktop JVM against
 * python-fido2 (test/PasskeysCheck.java, check_passkeys.py) and runs on the phone.
 *
 * A passkey here is a SYNCED credential: its P-256 private key is a vault item (S2),
 * end-to-end encrypted to the vault's members, so it can sign from a second enrolled
 * device. That is why the flags say BE=1 (backup eligible) and BS=1 (backed up), why
 * the key is software P-256 rather than a non-exportable KeyStore key, and why
 * signCount stays 0 (WebAuthn §6.1.1: a synced credential cannot keep a monotonic
 * counter, and 0 tells the RP not to check it). Attestation is "none" with the Aither
 * AAGUID, which is how a relying party names the provider ("Aither") in its UI.
 *
 * The provider signs {@code authenticatorData || clientDataHash}: the caller (a browser
 * through Credential Manager, or our own native flow) hands the clientDataHash, so the
 * origin binding lives in the hash the browser computed, never in a string we build.
 */
final class Passkeys {
    private Passkeys() {}

    /** Aither's AAGUID (registered once, 2026-11-13). Never reuse for another provider. */
    static final String AAGUID_STR = "3d2e3505-9a19-4709-9cd2-516ba4ae1085";
    static final byte[] AAGUID = uuidBytes(AAGUID_STR);

    static final int UP = 0x01, UV = 0x04, BE = 0x08, BS = 0x10, AT = 0x40;
    static final int COSE_ES256 = -7;

    /** One stored passkey: the vault item, exactly the fields S2 encrypts and syncs. */
    static final class Credential {
        final String rpId;
        final byte[] credentialId;
        final byte[] userHandle;
        final String userName;
        final String displayName;
        final byte[] privateKeyPkcs8;
        final long createdAt;

        Credential(String rpId, byte[] credentialId, byte[] userHandle, String userName,
                   String displayName, byte[] privateKeyPkcs8, long createdAt) {
            this.rpId = rpId;
            this.credentialId = credentialId;
            this.userHandle = userHandle;
            this.userName = userName;
            this.displayName = displayName;
            this.privateKeyPkcs8 = privateKeyPkcs8;
            this.createdAt = createdAt;
        }

        /** The vault item (type "passkey"). The private key never leaves this object
         *  except inside the vault's encrypted envelope. */
        JSONObject toVaultItem() throws JSONException {
            return new JSONObject()
                    .put("type", "passkey").put("v", 1)
                    .put("rp_id", rpId)
                    .put("credential_id", b64u(credentialId))
                    .put("user_handle", b64u(userHandle))
                    .put("user_name", userName)
                    .put("display_name", displayName)
                    .put("private_key_pkcs8", b64u(privateKeyPkcs8))
                    .put("aaguid", AAGUID_STR)
                    .put("sign_count", 0)
                    .put("created_at", createdAt);
        }

        static Credential fromVaultItem(JSONObject o) throws JSONException {
            if (!"passkey".equals(o.optString("type"))) {
                throw new JSONException("not a passkey item");
            }
            return new Credential(o.getString("rp_id"), unb64u(o.getString("credential_id")),
                    unb64u(o.getString("user_handle")), o.optString("user_name"),
                    o.optString("display_name"), unb64u(o.getString("private_key_pkcs8")),
                    o.optLong("created_at"));
        }
    }

    /** What a registration returns: the stored credential and the JSON the RP reads. */
    static final class Created {
        final Credential credential;
        final JSONObject response;

        Created(Credential credential, JSONObject response) {
            this.credential = credential;
            this.response = response;
        }
    }

    // ── registration (navigator.credentials.create) ───────────────────────────

    /**
     * Make a passkey for {@code rpId}. {@code options} is the PublicKeyCredentialCreationOptions
     * JSON the RP sent (Credential Manager hands it verbatim). Refuses an RP that does not
     * accept ES256, an rpId the caller's origin may not claim (checked by the caller), and
     * an excluded credential this vault already holds.
     */
    static Created create(JSONObject options, String rpId, byte[] clientDataHash,
                          Iterable<Credential> existing, SecureRandom rng, long now)
            throws GeneralSecurityException, JSONException {
        need(clientDataHash != null && clientDataHash.length == 32, "clientDataHash must be 32 bytes");
        need(rpId != null && !rpId.isEmpty(), "rpId missing");
        boolean es256 = false;
        JSONArray params = options.optJSONArray("pubKeyCredParams");
        for (int i = 0; params != null && i < params.length(); i++) {
            JSONObject p = params.getJSONObject(i);
            if ("public-key".equals(p.optString("type")) && p.optInt("alg") == COSE_ES256) es256 = true;
        }
        need(es256, "the site does not accept ES256 keys");
        JSONArray exclude = options.optJSONArray("excludeCredentials");
        for (int i = 0; exclude != null && i < exclude.length(); i++) {
            byte[] id = unb64u(exclude.getJSONObject(i).getString("id"));
            for (Credential c : existing) {
                if (c.rpId.equals(rpId) && MessageDigest.isEqual(c.credentialId, id)) {
                    throw new GeneralSecurityException("InvalidStateError: already registered");
                }
            }
        }
        JSONObject user = options.getJSONObject("user");
        byte[] userHandle = unb64u(user.getString("id"));
        need(userHandle.length > 0 && userHandle.length <= 64, "user.id must be 1..64 bytes");

        KeyPairGenerator g = KeyPairGenerator.getInstance("EC");
        g.initialize(new ECGenParameterSpec("secp256r1"), rng);
        KeyPair kp = g.generateKeyPair();
        byte[] credId = new byte[32];
        rng.nextBytes(credId);

        byte[] authData = authData(rpId, UP | UV | BE | BS | AT,
                attestedCredentialData(credId, (ECPublicKey) kp.getPublic()));
        byte[] attObj = new Cbor().map(3)
                .text("fmt").text("none")
                .text("attStmt").map(0)
                .text("authData").bytes(authData).toByteArray();
        Credential cred = new Credential(rpId, credId, userHandle, user.optString("name"),
                user.optString("displayName"), kp.getPrivate().getEncoded(), now);

        JSONObject response = new JSONObject()
                .put("attestationObject", b64u(attObj))
                .put("authenticatorData", b64u(authData))
                .put("publicKey", b64u(kp.getPublic().getEncoded()))
                .put("publicKeyAlgorithm", COSE_ES256)
                .put("transports", new JSONArray().put("internal").put("hybrid"));
        JSONObject json = new JSONObject()
                .put("id", b64u(credId)).put("rawId", b64u(credId)).put("type", "public-key")
                .put("authenticatorAttachment", "platform")
                .put("response", response)
                .put("clientExtensionResults", new JSONObject()
                        .put("credProps", new JSONObject().put("rk", true)));
        return new Created(cred, json);
    }

    // ── authentication (navigator.credentials.get) ────────────────────────────

    /** The credentials in {@code vault} the RP's request may use (rpId + allowCredentials). */
    static java.util.List<Credential> candidates(JSONObject options, String rpId,
                                                 Iterable<Credential> vault) throws JSONException {
        java.util.List<Credential> out = new java.util.ArrayList<>();
        JSONArray allow = options.optJSONArray("allowCredentials");
        for (Credential c : vault) {
            if (!c.rpId.equals(rpId)) continue;
            if (allow == null || allow.length() == 0) { out.add(c); continue; }
            for (int i = 0; i < allow.length(); i++) {
                if (MessageDigest.isEqual(c.credentialId,
                        unb64u(allow.getJSONObject(i).getString("id")))) {
                    out.add(c);
                    break;
                }
            }
        }
        return out;
    }

    /** Sign an assertion: ES256 over authenticatorData || clientDataHash, signCount 0. */
    static JSONObject get(Credential c, byte[] clientDataHash)
            throws GeneralSecurityException, JSONException {
        need(clientDataHash != null && clientDataHash.length == 32, "clientDataHash must be 32 bytes");
        byte[] authData = authData(c.rpId, UP | UV | BE | BS, null);
        PrivateKey sk = KeyFactory.getInstance("EC")
                .generatePrivate(new PKCS8EncodedKeySpec(c.privateKeyPkcs8));
        Signature s = Signature.getInstance("SHA256withECDSA");
        s.initSign(sk);
        s.update(authData);
        s.update(clientDataHash);
        JSONObject response = new JSONObject()
                .put("authenticatorData", b64u(authData))
                .put("signature", b64u(s.sign()))
                .put("userHandle", b64u(c.userHandle));
        return new JSONObject()
                .put("id", b64u(c.credentialId)).put("rawId", b64u(c.credentialId))
                .put("type", "public-key").put("authenticatorAttachment", "platform")
                .put("response", response).put("clientExtensionResults", new JSONObject());
    }

    // ── authenticator data ────────────────────────────────────────────────────

    static byte[] authData(String rpId, int flags, byte[] attested) throws GeneralSecurityException {
        ByteArrayOutputStream o = new ByteArrayOutputStream();
        o.write(sha256(rpId.getBytes(StandardCharsets.UTF_8)), 0, 32);
        o.write(flags);
        o.write(new byte[4], 0, 4); // signCount 0: synced credential (see class doc)
        if (attested != null) o.write(attested, 0, attested.length);
        return o.toByteArray();
    }

    static byte[] attestedCredentialData(byte[] credId, ECPublicKey pub) {
        ByteArrayOutputStream o = new ByteArrayOutputStream();
        o.write(AAGUID, 0, 16);
        o.write((credId.length >> 8) & 0xff);
        o.write(credId.length & 0xff);
        o.write(credId, 0, credId.length);
        byte[] cose = new Cbor().map(5)
                .integer(1).integer(2)            // kty: EC2
                .integer(3).integer(COSE_ES256)   // alg: ES256
                .integer(-1).integer(1)           // crv: P-256
                .integer(-2).bytes(fixed32(pub.getW().getAffineX()))
                .integer(-3).bytes(fixed32(pub.getW().getAffineY()))
                .toByteArray();
        o.write(cose, 0, cose.length);
        return o.toByteArray();
    }

    // ── origin binding ────────────────────────────────────────────────────────

    /**
     * May a web {@code origin} (from a privileged browser) use {@code rpId}? WebAuthn §5.1.3:
     * the origin must be https and its host equal to the rpId or a subdomain of it. A bare
     * single-label rpId (a TLD, "localhost" aside) is refused. A public suffix beyond one
     * label (co.uk) is the browser's job: it hands us an origin it already checked.
     */
    static boolean rpIdAllowed(String origin, String rpId) {
        if (origin == null || rpId == null || rpId.isEmpty()) return false;
        String id = rpId.toLowerCase(java.util.Locale.ROOT);
        if (!id.equals(rpId) || id.startsWith(".") || id.endsWith(".")) return false;
        String host;
        if (origin.startsWith("https://")) {
            host = origin.substring(8);
        } else if (origin.startsWith("http://localhost")) {
            host = origin.substring(7);  // WebAuthn's one secure-context http exception
        } else {
            return false;
        }
        int cut = host.indexOf('/');
        if (cut >= 0) host = host.substring(0, cut);
        cut = host.lastIndexOf(':');
        if (cut >= 0) host = host.substring(0, cut);
        host = host.toLowerCase(java.util.Locale.ROOT);
        if (!id.contains(".") && !id.equals("localhost")) return false;
        return host.equals(id) || host.endsWith("." + id);
    }

    /** Who is asking: a privileged browser (origin it vouches for) or a native app. */
    static final class Caller {
        final String origin;
        final boolean browser;

        Caller(String origin, boolean browser) {
            this.origin = origin;
            this.browser = browser;
        }
    }

    /**
     * Decide the origin. A browser's claimed origin is believed ONLY when the calling
     * package AND one of its signing certificates are in the privileged-browser allowlist
     * (Google's published list, shipped in assets); any other caller is a native app whose
     * origin is {@code android:apk-key-hash:<b64url sha256(cert)>} and whose rpId must be
     * vouched for by the site's assetlinks.json ({@link #assetLinksAllow}).
     */
    static Caller caller(String pkg, List<byte[]> certs, String claimedOrigin,
                         String allowlistJson) throws GeneralSecurityException, JSONException {
        need(pkg != null && certs != null && !certs.isEmpty(), "caller has no signing certificate");
        List<String> fps = new java.util.ArrayList<>();
        for (byte[] c : certs) fps.add(colonHex(sha256(c)));
        if (claimedOrigin != null && !claimedOrigin.isEmpty()) {
            JSONArray apps = new JSONObject(allowlistJson).getJSONArray("apps");
            for (int i = 0; i < apps.length(); i++) {
                JSONObject info = apps.getJSONObject(i).optJSONObject("info");
                if (info == null || !pkg.equals(info.optString("package_name"))) continue;
                JSONArray sigs = info.optJSONArray("signatures");
                for (int j = 0; sigs != null && j < sigs.length(); j++) {
                    String fp = sigs.getJSONObject(j).optString("cert_fingerprint_sha256");
                    if (fps.contains(fp.toUpperCase(java.util.Locale.ROOT))) {
                        return new Caller(claimedOrigin, true);
                    }
                }
            }
            throw new GeneralSecurityException("an app that is not a known browser claimed a web origin");
        }
        return new Caller("android:apk-key-hash:" + b64u(sha256(certs.get(0))), false);
    }

    /** Does the site's assetlinks.json let {@code pkg} (signed by one of {@code certs}) use
     *  its passkeys? (delegate_permission/common.get_login_creds) */
    static boolean assetLinksAllow(String assetlinksJson, String pkg, List<byte[]> certs)
            throws GeneralSecurityException {
        List<String> fps = new java.util.ArrayList<>();
        for (byte[] c : certs) fps.add(colonHex(sha256(c)));
        try {
            JSONArray a = new JSONArray(assetlinksJson);
            for (int i = 0; i < a.length(); i++) {
                JSONObject st = a.getJSONObject(i);
                JSONArray rel = st.optJSONArray("relation");
                boolean creds = false;
                for (int j = 0; rel != null && j < rel.length(); j++) {
                    if ("delegate_permission/common.get_login_creds".equals(rel.optString(j))) creds = true;
                }
                JSONObject t = st.optJSONObject("target");
                if (!creds || t == null || !"android_app".equals(t.optString("namespace"))
                        || !pkg.equals(t.optString("package_name"))) continue;
                JSONArray want = t.optJSONArray("sha256_cert_fingerprints");
                for (int j = 0; want != null && j < want.length(); j++) {
                    if (fps.contains(want.optString(j).toUpperCase(java.util.Locale.ROOT))) return true;
                }
            }
        } catch (JSONException e) {
            return false;
        }
        return false;
    }

    static String colonHex(byte[] b) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < b.length; i++) {
            if (i > 0) sb.append(':');
            sb.append(String.format(java.util.Locale.ROOT, "%02X", b[i] & 0xff));
        }
        return sb.toString();
    }

    /** clientDataJSON for a NATIVE caller (no browser hash): origin android:apk-key-hash. */
    static byte[] clientDataJson(String type, String challengeB64u, String origin)
            throws JSONException {
        return new JSONObject().put("type", type).put("challenge", challengeB64u)
                .put("origin", origin).put("crossOrigin", false).toString()
                .replace("\\/", "/").getBytes(StandardCharsets.UTF_8);
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    /** Minimal canonical CBOR writer: unsigned/negative ints, byte/text strings, maps. */
    static final class Cbor {
        private final ByteArrayOutputStream o = new ByteArrayOutputStream();

        private Cbor head(int major, long n) {
            int m = major << 5;
            if (n < 24) o.write(m | (int) n);
            else if (n < 0x100) { o.write(m | 24); o.write((int) n); }
            else if (n < 0x10000) { o.write(m | 25); o.write((int) (n >> 8)); o.write((int) n & 0xff); }
            else { o.write(m | 26); for (int s = 24; s >= 0; s -= 8) o.write((int) (n >> s) & 0xff); }
            return this;
        }

        Cbor integer(long v) { return v >= 0 ? head(0, v) : head(1, -1 - v); }
        Cbor bytes(byte[] b) { head(2, b.length); o.write(b, 0, b.length); return this; }
        Cbor text(String s) {
            byte[] b = s.getBytes(StandardCharsets.UTF_8);
            head(3, b.length);
            o.write(b, 0, b.length);
            return this;
        }
        Cbor map(int pairs) { return head(5, pairs); }
        byte[] toByteArray() { return o.toByteArray(); }
    }

    static byte[] fixed32(BigInteger v) {
        byte[] raw = v.toByteArray();
        byte[] out = new byte[32];
        int n = Math.min(raw.length, 32);
        System.arraycopy(raw, raw.length - n, out, 32 - n, n);
        return out;
    }

    static byte[] sha256(byte[] b) throws GeneralSecurityException {
        return MessageDigest.getInstance("SHA-256").digest(b);
    }

    static String b64u(byte[] b) {
        return Base64.getUrlEncoder().withoutPadding().encodeToString(b);
    }

    static byte[] unb64u(String s) {
        return Base64.getUrlDecoder().decode(s.trim());
    }

    private static byte[] uuidBytes(String u) {
        String hex = u.replace("-", "");
        byte[] out = new byte[16];
        for (int i = 0; i < 16; i++) out[i] = (byte) Integer.parseInt(hex.substring(2 * i, 2 * i + 2), 16);
        return out;
    }

    private static void need(boolean ok, String why) throws GeneralSecurityException {
        if (!ok) throw new GeneralSecurityException(why);
    }
}
