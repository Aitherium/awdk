package com.aitherium.aither;

import android.app.Activity;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.Signature;
import android.content.pm.SigningInfo;
import android.credentials.CreateCredentialException;
import android.credentials.CreateCredentialResponse;
import android.credentials.Credential;
import android.credentials.CredentialOption;
import android.credentials.GetCredentialException;
import android.credentials.GetCredentialResponse;
import android.hardware.biometrics.BiometricManager;
import android.hardware.biometrics.BiometricPrompt;
import android.os.Bundle;
import android.os.CancellationSignal;
import android.service.credentials.CallingAppInfo;
import android.service.credentials.CreateCredentialRequest;
import android.service.credentials.CredentialProviderService;
import android.service.credentials.GetCredentialRequest;

import org.json.JSONObject;

import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Executors;

import javax.crypto.Cipher;

/**
 * The one sheet a passkey use shows (S4): the site, then a STRONG fingerprint that unlocks
 * the passkey store's key for this single operation. Launched by the system from the
 * entry {@link PasskeyProviderService} offered; the framework puts the full request in the
 * intent. Every refusal returns a typed Credential Manager error, never a silent finish.
 */
public class PasskeyActivity extends Activity {
    static final String MODE_CREATE = "create", MODE_GET = "get";
    private static final String X_MODE = "aither.passkey.mode", X_CRED = "aither.passkey.cred";

    static PendingIntent pending(Context ctx, String mode, String credentialId, int code) {
        Intent i = new Intent(ctx, PasskeyActivity.class).setPackage(ctx.getPackageName())
                .putExtra(X_MODE, mode).putExtra(X_CRED, credentialId);
        return PendingIntent.getActivity(ctx, code, i,
                PendingIntent.FLAG_MUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        String mode = getIntent().getStringExtra(X_MODE);
        Executors.newSingleThreadExecutor().execute(() -> {
            try {
                if (MODE_CREATE.equals(mode)) create();
                else if (MODE_GET.equals(mode)) get();
                else fail(mode, "unknown request");
            } catch (Exception e) {
                fail(mode, e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage());
            }
        });
    }

    // ── create ────────────────────────────────────────────────────────────────

    private void create() throws Exception {
        CreateCredentialRequest req = getIntent().getParcelableExtra(
                CredentialProviderService.EXTRA_CREATE_CREDENTIAL_REQUEST, CreateCredentialRequest.class);
        if (req == null) throw new SecurityException("no create request");
        Bundle data = req.getData();
        JSONObject options = new JSONObject(data.getString(PasskeyProviderService.KEY_REQUEST_JSON, "{}"));
        JSONObject rp = options.getJSONObject("rp");
        String rpId = rp.optString("id");
        Bound bound = bind(req.getCallingAppInfo(), rpId,
                data.getByteArray(PasskeyProviderService.KEY_CLIENT_DATA_HASH),
                "webauthn.create", options.getString("challenge"));
        PasskeyStore store = new PasskeyStore(this);
        List<Passkeys.Credential> held = new ArrayList<>();  // matched against excludeCredentials
        for (PasskeyStore.Entry e : store.forRp(rpId)) {
            held.add(new Passkeys.Credential(e.rpId, Passkeys.unb64u(e.credentialId), new byte[0],
                    e.userName, e.displayName, new byte[0], 0));
        }
        String who = options.getJSONObject("user").optString("name");
        Cipher authed = biometric("Save a passkey for " + rpId, who, PasskeyStore.sealCipher());
        Passkeys.Created made = Passkeys.create(options, rpId, bound.hash, held, new SecureRandom(),
                System.currentTimeMillis() / 1000);
        store.seal(authed, made.credential);
        if (bound.clientDataJson != null) {
            made.response.getJSONObject("response").put("clientDataJSON",
                    Passkeys.b64u(bound.clientDataJson));
        }
        Bundle out = new Bundle();
        out.putString(PasskeyProviderService.KEY_REGISTRATION_JSON, made.response.toString());
        Intent result = new Intent().putExtra(CredentialProviderService.EXTRA_CREATE_CREDENTIAL_RESPONSE,
                new CreateCredentialResponse(out));
        runOnUiThread(() -> { setResult(RESULT_OK, result); finish(); });
    }

    // ── get ───────────────────────────────────────────────────────────────────

    private void get() throws Exception {
        GetCredentialRequest req = getIntent().getParcelableExtra(
                CredentialProviderService.EXTRA_GET_CREDENTIAL_REQUEST, GetCredentialRequest.class);
        if (req == null) throw new SecurityException("no get request");
        CredentialOption opt = null;
        for (CredentialOption o : req.getCredentialOptions()) {
            if (PasskeyProviderService.TYPE_PUBLIC_KEY.equals(o.getType())) opt = o;
        }
        if (opt == null) throw new SecurityException("no passkey option");
        Bundle data = opt.getCredentialRetrievalData();
        JSONObject options = new JSONObject(data.getString(PasskeyProviderService.KEY_REQUEST_JSON, "{}"));
        String rpId = options.optString("rpId");
        Bound bound = bind(req.getCallingAppInfo(), rpId,
                data.getByteArray(PasskeyProviderService.KEY_CLIENT_DATA_HASH),
                "webauthn.get", options.getString("challenge"));
        String credId = getIntent().getStringExtra(X_CRED);
        PasskeyStore store = new PasskeyStore(this);
        String who = null;
        for (PasskeyStore.Entry e : store.forRp(rpId)) if (e.credentialId.equals(credId)) who = e.userName;
        if (who == null) throw new SecurityException("that passkey is not for " + rpId);
        Cipher authed = biometric("Sign in to " + rpId, who, store.unsealCipher(credId));
        Passkeys.Credential cred = store.unseal(authed, credId);
        if (!cred.rpId.equals(rpId)) throw new SecurityException("passkey belongs to another site");
        JSONObject assertion = Passkeys.get(cred, bound.hash);
        if (bound.clientDataJson != null) {
            assertion.getJSONObject("response").put("clientDataJSON",
                    Passkeys.b64u(bound.clientDataJson));
        }
        Bundle out = new Bundle();
        out.putString(PasskeyProviderService.KEY_AUTHENTICATION_JSON, assertion.toString());
        Intent result = new Intent().putExtra(CredentialProviderService.EXTRA_GET_CREDENTIAL_RESPONSE,
                new GetCredentialResponse(new Credential(PasskeyProviderService.TYPE_PUBLIC_KEY, out)));
        runOnUiThread(() -> { setResult(RESULT_OK, result); finish(); });
    }

    // ── origin binding ────────────────────────────────────────────────────────

    private static final class Bound {
        final byte[] hash;
        final byte[] clientDataJson;  // non-null only for a native caller

        Bound(byte[] hash, byte[] clientDataJson) {
            this.hash = hash;
            this.clientDataJson = clientDataJson;
        }
    }

    /** Check the caller may use {@code rpId}; return the clientDataHash to sign over. */
    private Bound bind(CallingAppInfo app, String rpId, byte[] browserHash, String type,
                       String challenge) throws Exception {
        List<byte[]> certs = new ArrayList<>();
        SigningInfo si = app.getSigningInfo();
        if (si != null) for (Signature s : si.getApkContentsSigners()) certs.add(s.toByteArray());
        String allow;
        try (InputStream in = getAssets().open("privileged_browsers.json")) {
            allow = new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
        Passkeys.Caller c = Passkeys.caller(app.getPackageName(), certs, app.getOrigin(), allow);
        if (c.browser) {
            if (!Passkeys.rpIdAllowed(c.origin, rpId)) {
                throw new SecurityException(c.origin + " may not use passkeys for " + rpId);
            }
            if (browserHash == null || browserHash.length != 32) {
                throw new SecurityException("the browser sent no clientDataHash");
            }
            return new Bound(browserHash, null);
        }
        if (!Passkeys.assetLinksAllow(fetchAssetLinks(rpId), app.getPackageName(), certs)) {
            throw new SecurityException(rpId + " does not vouch for this app (assetlinks.json)");
        }
        byte[] cdj = Passkeys.clientDataJson(type, challenge, c.origin);
        return new Bound(Passkeys.sha256(cdj), cdj);
    }

    private static String fetchAssetLinks(String rpId) throws java.io.IOException {
        HttpURLConnection h = (HttpURLConnection) new URL(
                "https://" + rpId + "/.well-known/assetlinks.json").openConnection();
        h.setConnectTimeout(8000);
        h.setReadTimeout(8000);
        h.setInstanceFollowRedirects(false);
        int status = h.getResponseCode();
        if (status != 200) {
            h.disconnect();
            throw new SecurityException(rpId + "/.well-known/assetlinks.json answered " + status
                    + "; the site has not vouched for this app");
        }
        try (InputStream in = h.getInputStream()) {
            byte[] body = in.readNBytes(256 * 1024);
            return new String(body, StandardCharsets.UTF_8);
        } finally {
            h.disconnect();
        }
    }

    // ── biometric (STRONG only, one sheet per operation) ──────────────────────

    private Cipher biometric(String title, String subtitle, Cipher cipher) throws Exception {
        final Object lock = new Object();
        final Cipher[] got = new Cipher[1];
        final String[] err = new String[1];
        BiometricPrompt p = new BiometricPrompt.Builder(this)
                .setTitle(title).setSubtitle(subtitle.isEmpty() ? "Aither vault" : subtitle)
                .setDescription("Your fingerprint unlocks this one passkey use.")
                .setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_STRONG)
                .setNegativeButton("Cancel", getMainExecutor(), (d, w) -> {
                    synchronized (lock) { err[0] = "canceled"; lock.notifyAll(); }
                })
                .build();
        runOnUiThread(() -> p.authenticate(new BiometricPrompt.CryptoObject(cipher),
                new CancellationSignal(), getMainExecutor(),
                new BiometricPrompt.AuthenticationCallback() {
                    @Override
                    public void onAuthenticationSucceeded(BiometricPrompt.AuthenticationResult r) {
                        synchronized (lock) { got[0] = r.getCryptoObject().getCipher(); lock.notifyAll(); }
                    }

                    @Override
                    public void onAuthenticationError(int code, CharSequence msg) {
                        synchronized (lock) { err[0] = "canceled"; lock.notifyAll(); }
                    }
                }));
        synchronized (lock) {
            while (got[0] == null && err[0] == null) lock.wait();
        }
        if (got[0] == null) throw new InterruptedException(err[0]);
        return got[0];
    }

    private void fail(String mode, String why) {
        Intent result = new Intent();
        if (MODE_CREATE.equals(mode)) {
            result.putExtra(CredentialProviderService.EXTRA_CREATE_CREDENTIAL_EXCEPTION,
                    new CreateCredentialException("canceled".equals(why)
                            ? CreateCredentialException.TYPE_USER_CANCELED
                            : CreateCredentialException.TYPE_UNKNOWN, why));
        } else {
            result.putExtra(CredentialProviderService.EXTRA_GET_CREDENTIAL_EXCEPTION,
                    new GetCredentialException("canceled".equals(why)
                            ? GetCredentialException.TYPE_USER_CANCELED
                            : GetCredentialException.TYPE_NO_CREDENTIAL, why));
        }
        runOnUiThread(() -> { setResult(RESULT_OK, result); finish(); });
    }
}
