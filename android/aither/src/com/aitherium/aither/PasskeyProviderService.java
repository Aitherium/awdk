package com.aitherium.aither;

import android.app.PendingIntent;
import android.app.slice.Slice;
import android.app.slice.SliceSpec;
import android.credentials.CreateCredentialException;
import android.credentials.GetCredentialException;
import android.net.Uri;
import android.os.CancellationSignal;
import android.os.OutcomeReceiver;
import android.service.credentials.BeginCreateCredentialRequest;
import android.service.credentials.BeginCreateCredentialResponse;
import android.service.credentials.BeginGetCredentialOption;
import android.service.credentials.BeginGetCredentialRequest;
import android.service.credentials.BeginGetCredentialResponse;
import android.service.credentials.ClearCredentialStateRequest;
import android.service.credentials.CreateEntry;
import android.service.credentials.CredentialEntry;
import android.service.credentials.CredentialProviderService;

import org.json.JSONObject;

import java.util.Collections;
import java.util.List;

/**
 * Aither as an Android passkey provider (API 34+ framework Credential Manager, S4 #16698).
 *
 * The Begin* calls answer from the clear index only (site + account name): nothing is
 * unsealed and no key is touched until the person picks an entry, which opens
 * {@link PasskeyActivity}; that activity shows the site, runs a STRONG biometric on the
 * passkey store's key, and only then creates or signs. Because the passkey lives on the
 * phone that asks, there is no Bluetooth/hybrid leg at all.
 *
 * Slices carry the androidx.credentials hint names: the system selector renders entries
 * by those strings, whether or not the provider links androidx (this app does not).
 */
public class PasskeyProviderService extends CredentialProviderService {
    static final String TYPE_PUBLIC_KEY = "androidx.credentials.TYPE_PUBLIC_KEY_CREDENTIAL";
    static final String KEY_REQUEST_JSON = "androidx.credentials.BUNDLE_KEY_REQUEST_JSON";
    static final String KEY_CLIENT_DATA_HASH = "androidx.credentials.BUNDLE_KEY_CLIENT_DATA_HASH";
    static final String KEY_REGISTRATION_JSON =
            "androidx.credentials.BUNDLE_KEY_REGISTRATION_RESPONSE_JSON";
    static final String KEY_AUTHENTICATION_JSON =
            "androidx.credentials.BUNDLE_KEY_AUTHENTICATION_RESPONSE_JSON";

    private static final String CE = "androidx.credentials.provider.credentialEntry.";
    private static final String CR = "androidx.credentials.provider.createEntry.";

    @Override
    public void onBeginCreateCredential(BeginCreateCredentialRequest request,
                                        CancellationSignal cancel,
                                        OutcomeReceiver<BeginCreateCredentialResponse,
                                                CreateCredentialException> callback) {
        if (!TYPE_PUBLIC_KEY.equals(request.getType())) {
            callback.onError(new CreateCredentialException(
                    CreateCredentialException.TYPE_NO_CREATE_OPTIONS, "Aither stores passkeys only"));
            return;
        }
        PendingIntent pi = PasskeyActivity.pending(this, PasskeyActivity.MODE_CREATE, "", 1);
        Slice.Builder b = new Slice.Builder(Uri.EMPTY, new SliceSpec("CreateEntry", 1));
        b.addText("Aither vault", null, Collections.singletonList(CR + "SLICE_HINT_USER_PROVIDER_ACCOUNT_NAME"));
        b.addText("Saved in your Aither vault, synced end-to-end encrypted", null,
                Collections.singletonList(CR + "SLICE_HINT_NOTE"));
        b.addAction(pi, new Slice.Builder(b).addHints(
                Collections.singletonList(CR + "SLICE_HINT_PENDING_INTENT")).build(), null);
        callback.onResult(new BeginCreateCredentialResponse.Builder()
                .addCreateEntry(new CreateEntry(b.build())).build());
    }

    @Override
    public void onBeginGetCredential(BeginGetCredentialRequest request, CancellationSignal cancel,
                                     OutcomeReceiver<BeginGetCredentialResponse,
                                             GetCredentialException> callback) {
        BeginGetCredentialResponse.Builder out = new BeginGetCredentialResponse.Builder();
        PasskeyStore store = new PasskeyStore(this);
        int code = 100;
        for (BeginGetCredentialOption opt : request.getBeginGetCredentialOptions()) {
            if (!TYPE_PUBLIC_KEY.equals(opt.getType())) continue;
            String rpId;
            try {
                rpId = new JSONObject(opt.getCandidateQueryData().getString(KEY_REQUEST_JSON, "{}"))
                        .optString("rpId");
            } catch (org.json.JSONException e) {
                continue;
            }
            List<PasskeyStore.Entry> mine = store.forRp(rpId);
            for (PasskeyStore.Entry e : mine) {
                PendingIntent pi = PasskeyActivity.pending(this, PasskeyActivity.MODE_GET,
                        e.credentialId, code++);
                out.addCredentialEntry(new CredentialEntry(opt, entrySlice(opt, e, pi)));
            }
        }
        callback.onResult(out.build());
    }

    private static Slice entrySlice(BeginGetCredentialOption opt, PasskeyStore.Entry e,
                                    PendingIntent pi) {
        Slice.Builder b = new Slice.Builder(Uri.EMPTY, new SliceSpec(TYPE_PUBLIC_KEY, 1));
        String title = e.userName.isEmpty() ? e.displayName : e.userName;
        b.addText("Passkey", null, Collections.singletonList(CE + "SLICE_HINT_TYPE_DISPLAY_NAME"));
        b.addText(title, null, Collections.singletonList(CE + "SLICE_HINT_USER_NAME"));
        b.addText(e.displayName, null,
                Collections.singletonList(CE + "SLICE_HINT_CREDENTIAL_TYPE_DISPLAY_NAME"));
        b.addText(opt.getId(), null, Collections.singletonList(CE + "SLICE_HINT_OPTION_ID"));
        b.addText("false", null, Collections.singletonList(CE + "SLICE_HINT_AUTO_ALLOWED"));
        b.addAction(pi, new Slice.Builder(b).addHints(
                Collections.singletonList(CE + "SLICE_HINT_PENDING_INTENT")).build(), null);
        return b.build();
    }

    @Override
    public void onClearCredentialState(ClearCredentialStateRequest request, CancellationSignal cancel,
                                       OutcomeReceiver<Void,
                                               android.credentials.ClearCredentialStateException> cb) {
        cb.onResult(null); // nothing session-like is cached: every use asks for the biometric
    }
}
