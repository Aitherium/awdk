package com.aitherium.aither;

/** Exit 0 when InviteShare shares only aitherium.com / app.aitherium.com https links. */
public class InviteShareCheck {
    public static void main(String[] a) {
        String[][] cases = {
            {"https://app.aitherium.com/family/join#t=abc", "true"},
            {"https://aitherium.com/family/join#c=ABCDE-FGHIJ", "true"},
            {"http://app.aitherium.com/family/join", "false"},
            {"https://evil.com/family/join", "false"},
            {"https://app.aitherium.com.evil.com/x", "false"},
            {"https://evilaitherium.com/x", "false"},
            {"https://api.aitherium.com/x", "false"},
            {"https://user@app.aitherium.com/x", "false"},
            {"https://app.aitherium.com:8443/x", "false"},
            {"javascript:alert(1)", "false"},
            {"", "false"},
        };
        int bad = 0;
        for (String[] c : cases) {
            if (InviteShare.shareable(c[0]) != Boolean.parseBoolean(c[1])) { System.out.println("FAIL " + c[0]); bad++; }
        }
        if (InviteShare.shareable(null)) { System.out.println("FAIL null"); bad++; }
        String m = InviteShare.message("May invited you.\n Tap to join:", "https://app.aitherium.com/family/join#t=x");
        if (!"May invited you. Tap to join: https://app.aitherium.com/family/join#t=x".equals(m)) { System.out.println("FAIL message " + m); bad++; }
        if (InviteShare.message("hi", "https://evil.com") != null) { System.out.println("FAIL refused message"); bad++; }
        if (InviteShare.message("x".repeat(500), "https://aitherium.com/").length() != InviteShare.MAX_TEXT + 1 + "https://aitherium.com/".length()) {
            System.out.println("FAIL clip"); bad++;
        }
        System.out.println(bad == 0 ? "OK" : bad + " FAILED");
        System.exit(bad == 0 ? 0 : 1);
    }
}
