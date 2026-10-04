package com.aitherium.aither;

/** Exit 0 when Purchases refuses exactly the payment hosts, and only in the store build. */
public class PurchasesCheck {
    public static void main(String[] a) {
        String[][] cases = {
            {"checkout.stripe.com", "true"}, {"buy.stripe.com", "true"}, {"billing.stripe.com", "true"},
            {"STRIPE.COM.", "true"}, {"m.stripe.network", "true"}, {"www.paypal.com", "true"},
            {"app.aitherium.com", "false"}, {"notstripe.com", "false"}, {"stripe.com.evil.io", "false"},
            {"", "false"},
        };
        int bad = 0;
        for (String[] c : cases) {
            boolean want = Boolean.parseBoolean(c[1]);
            if (Purchases.refuse(true, c[0]) != want) { System.out.println("FAIL store " + c[0]); bad++; }
            if (Purchases.refuse(false, c[0])) { System.out.println("FAIL github " + c[0]); bad++; }
        }
        if (Purchases.paymentHost(null)) { System.out.println("FAIL null"); bad++; }
        System.out.println(bad == 0 ? "OK" : bad + " FAILED");
        System.exit(bad == 0 ? 0 : 1);
    }
}
