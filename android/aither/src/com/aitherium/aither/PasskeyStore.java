package com.aitherium.aither;

import android.content.Context;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.GeneralSecurityException;
import java.security.KeyStore;
import java.util.ArrayList;
import java.util.List;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/**
 * Where the phone keeps its passkeys (S4). Each passkey is a vault item
 * ({@link Passkeys.Credential#toVaultItem()}); on this device it is sealed with AES-GCM
 * under an Android KeyStore key that needs a STRONG biometric for EVERY use (timeout 0),
 * so reading a private key to sign is gated by the fingerprint on the same sheet that
 * shows the site, and UV=1 in the assertion is true.
 *
 * The listing index (site, account name, credential id) is NOT secret and is kept in the
 * clear so the Credential Manager sheet can offer the right entries before unlock; the
 * private key is only in the sealed record.
 *
 * Sync to a second enrolled device is the vault's job (slice S2: the item is encrypted
 * end to end to the vault members and travels as ciphertext). {@link #exportForSync}
 * hands S2 the plaintext item only after the same biometric unseal, and
 * {@link #importSynced} seals a synced item under THIS device's key.
 */
final class PasskeyStore {
    static final String KEY_ALIAS = "aither-passkeys-v1";
    private static final String DIR = "passkeys";
    private final File dir;

    PasskeyStore(Context ctx) {
        dir = new File(ctx.getNoBackupFilesDir(), DIR);
        //noinspection ResultOfMethodCallIgnored
        dir.mkdirs();
    }

    /** One entry of the clear index. */
    static final class Entry {
        final String rpId, credentialId, userName, displayName;

        Entry(JSONObject o) {
            rpId = o.optString("rp_id");
            credentialId = o.optString("credential_id");
            userName = o.optString("user_name");
            displayName = o.optString("display_name");
        }
    }

    List<Entry> index() {
        List<Entry> out = new ArrayList<>();
        try {
            JSONArray a = new JSONArray(read(new File(dir, "index.json"), "[]"));
            for (int i = 0; i < a.length(); i++) out.add(new Entry(a.getJSONObject(i)));
        } catch (JSONException | java.io.IOException e) {
            return out;
        }
        return out;
    }

    List<Entry> forRp(String rpId) {
        List<Entry> out = new ArrayList<>();
        for (Entry e : index()) if (e.rpId.equals(rpId)) out.add(e);
        return out;
    }

    // ── the auth-per-use key ──────────────────────────────────────────────────

    private static SecretKey key() throws GeneralSecurityException, java.io.IOException {
        KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
        ks.load(null);
        if (ks.containsAlias(KEY_ALIAS)) return (SecretKey) ks.getKey(KEY_ALIAS, null);
        KeyGenerator g = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        g.init(new KeyGenParameterSpec.Builder(KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .setUserAuthenticationRequired(true)
                .setUserAuthenticationParameters(0, KeyProperties.AUTH_BIOMETRIC_STRONG)
                .setInvalidatedByBiometricEnrollment(true)
                .build());
        return g.generateKey();
    }

    /** A cipher to hand BiometricPrompt as its CryptoObject (seal a new passkey). */
    static Cipher sealCipher() throws GeneralSecurityException, java.io.IOException {
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.ENCRYPT_MODE, key());
        return c;
    }

    /** A cipher to hand BiometricPrompt to unseal {@code credentialId}. */
    Cipher unsealCipher(String credentialId) throws GeneralSecurityException, java.io.IOException {
        byte[] blob = Files.readAllBytes(record(credentialId).toPath());
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128, blob, 1, 12));
        return c;
    }

    // ── seal / unseal (the cipher came back authenticated from BiometricPrompt) ──

    void seal(Cipher authed, Passkeys.Credential cred)
            throws GeneralSecurityException, JSONException, java.io.IOException {
        byte[] ct = authed.doFinal(cred.toVaultItem().toString().getBytes(StandardCharsets.UTF_8));
        byte[] iv = authed.getIV();
        byte[] blob = new byte[1 + iv.length + ct.length];
        blob[0] = 1;
        System.arraycopy(iv, 0, blob, 1, iv.length);
        System.arraycopy(ct, 0, blob, 1 + iv.length, ct.length);
        String id = Passkeys.b64u(cred.credentialId);
        Files.write(record(id).toPath(), blob);
        JSONArray a = new JSONArray(read(new File(dir, "index.json"), "[]"));
        a.put(new JSONObject().put("rp_id", cred.rpId).put("credential_id", id)
                .put("user_name", cred.userName).put("display_name", cred.displayName));
        Files.write(new File(dir, "index.json").toPath(),
                a.toString().getBytes(StandardCharsets.UTF_8));
    }

    Passkeys.Credential unseal(Cipher authed, String credentialId)
            throws GeneralSecurityException, JSONException, java.io.IOException {
        byte[] blob = Files.readAllBytes(record(credentialId).toPath());
        if (blob.length < 1 + 12 + 16 || blob[0] != 1) throw new GeneralSecurityException("bad record");
        byte[] pt = authed.doFinal(blob, 13, blob.length - 13);
        Passkeys.Credential c = Passkeys.Credential.fromVaultItem(
                new JSONObject(new String(pt, StandardCharsets.UTF_8)));
        if (!Passkeys.b64u(c.credentialId).equals(credentialId)) {
            throw new GeneralSecurityException("record does not hold the credential asked for");
        }
        return c;
    }

    /** The plaintext vault item for S2 to encrypt to the vault members (after unseal). */
    JSONObject exportForSync(Cipher authed, String credentialId)
            throws GeneralSecurityException, JSONException, java.io.IOException {
        return unseal(authed, credentialId).toVaultItem();
    }

    /** Seal a passkey that arrived through vault sync under this device's key. */
    void importSynced(Cipher authedSeal, JSONObject vaultItem)
            throws GeneralSecurityException, JSONException, java.io.IOException {
        seal(authedSeal, Passkeys.Credential.fromVaultItem(vaultItem));
    }

    private File record(String credentialId) throws GeneralSecurityException {
        if (!credentialId.matches("[A-Za-z0-9_-]{16,128}")) {
            throw new GeneralSecurityException("bad credential id");
        }
        return new File(dir, "pk-" + credentialId + ".bin");
    }

    private static String read(File f, String dflt) throws java.io.IOException {
        return f.exists() ? new String(Files.readAllBytes(f.toPath()), StandardCharsets.UTF_8) : dflt;
    }
}
