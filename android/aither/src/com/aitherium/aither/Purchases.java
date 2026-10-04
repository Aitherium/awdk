package com.aitherium.aither;

import java.util.Locale;

/**
 * Google Play's payments policy: an app from Play sells digital goods only through Play
 * Billing, and may not send the user to another way to pay. AitherOS checks out on Stripe's
 * hosted pages, so the Play build (Flavor.STORE) never opens, loads or hands to the browser
 * a payment host. Plain Java (no android.*) so test/PurchasesCheck.java can run it on a JVM.
 */
final class Purchases {
    static final String REFUSED = "Purchases aren't available in the Google Play version of Aither.";
    private static final String[] HOSTS = {"stripe.com", "stripe.network", "link.com", "paypal.com"};

    private Purchases() {}

    /** Is this host a payment page (or a subdomain of one)? */
    static boolean paymentHost(String host) {
        if (host == null) return false;
        String h = host.toLowerCase(Locale.ROOT);
        while (h.endsWith(".")) h = h.substring(0, h.length() - 1);
        for (String p : HOSTS) {
            if (h.equals(p) || h.endsWith("." + p)) return true;
        }
        return false;
    }

    /** Should the app refuse to load or open this host? Only the store build refuses. */
    static boolean refuse(boolean store, String host) {
        return store && paymentHost(host);
    }
}
