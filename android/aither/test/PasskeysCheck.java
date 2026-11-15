package com.aitherium.aither;

import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * Driven by check_passkeys.py (python-fido2 is the relying party):
 *   create OPTIONS_FILE RP_ID HASH_HEX VAULT_OUT   -> prints the registration JSON
 *   get    OPTIONS_FILE RP_ID HASH_HEX VAULT_IN    -> prints the assertion JSON
 *   origin                                         -> prints one verdict per origin case
 * A "get" runs in a FRESH JVM that holds nothing but the vault item, the way a second
 * enrolled device does after the vault synced it.
 */
public class PasskeysCheck {
    public static void main(String[] a) throws Exception {
        if ("origin".equals(a[0])) { origin(a[1]); return; }
        JSONObject options = new JSONObject(new String(Files.readAllBytes(Paths.get(a[1])),
                StandardCharsets.UTF_8));
        String rpId = a[2];
        byte[] hash = hex(a[3]);
        if ("create".equals(a[0])) {
            Passkeys.Created c = Passkeys.create(options, rpId, hash,
                    Collections.<Passkeys.Credential>emptyList(), new SecureRandom(), 1_763_000_000L);
            Files.write(Paths.get(a[4]), c.credential.toVaultItem().toString()
                    .getBytes(StandardCharsets.UTF_8));
            System.out.println(c.response.toString());
        } else {
            Passkeys.Credential cred = Passkeys.Credential.fromVaultItem(new JSONObject(
                    new String(Files.readAllBytes(Paths.get(a[4])), StandardCharsets.UTF_8)));
            List<Passkeys.Credential> mine = Passkeys.candidates(options, rpId,
                    Collections.singletonList(cred));
            if (mine.isEmpty()) { System.out.println("NO-CANDIDATE"); return; }
            System.out.println(Passkeys.get(mine.get(0), hash).toString());
        }
    }

    private static void origin(String allowFile) throws Exception {
        String allow = new String(Files.readAllBytes(Paths.get(allowFile)), StandardCharsets.UTF_8);
        String[][] rp = {
                {"https://aitherium.com", "aitherium.com"},
                {"https://app.aitherium.com", "aitherium.com"},
                {"https://app.aitherium.com:8443/x", "app.aitherium.com"},
                {"https://evilaitherium.com", "aitherium.com"},
                {"http://aitherium.com", "aitherium.com"},
                {"https://aitherium.com", "com"},
                {"https://aitherium.com", "Aitherium.com"},
                {"http://localhost:5000", "localhost"},
        };
        for (String[] r : rp) {
            System.out.println("RP " + (Passkeys.rpIdAllowed(r[0], r[1]) ? "ALLOW" : "REFUSE"));
        }
        List<byte[]> fake = new ArrayList<>();
        fake.add("not-chrome-cert".getBytes(StandardCharsets.UTF_8));
        try {
            Passkeys.caller("com.android.chrome", fake, "https://aitherium.com", allow);
            System.out.println("CALLER ALLOW");
        } catch (java.security.GeneralSecurityException e) {
            System.out.println("CALLER REFUSE");
        }
        Passkeys.Caller n = Passkeys.caller("com.example.app", fake, null, allow);
        System.out.println("NATIVE " + n.origin + " " + n.browser);
        String links = "[{\"relation\":[\"delegate_permission/common.get_login_creds\"],"
                + "\"target\":{\"namespace\":\"android_app\",\"package_name\":\"com.example.app\","
                + "\"sha256_cert_fingerprints\":[\"" + Passkeys.colonHex(Passkeys.sha256(fake.get(0)))
                + "\"]}}]";
        System.out.println("LINKS " + Passkeys.assetLinksAllow(links, "com.example.app", fake)
                + " " + Passkeys.assetLinksAllow(links, "com.example.other", fake));
    }

    private static byte[] hex(String s) {
        byte[] out = new byte[s.length() / 2];
        for (int i = 0; i < out.length; i++) out[i] = (byte) Integer.parseInt(s.substring(2 * i, 2 * i + 2), 16);
        return out;
    }
}
