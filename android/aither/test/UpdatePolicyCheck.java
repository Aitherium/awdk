package com.aitherium.aither;

/**
 * Exit 0 when the self-update policy matches Android's rules for a silent self-update:
 * the quiet attempt is made whatever the "install unknown apps" switch says, a refusal that
 * a silent install could still pass is retried quietly (Android throttles repeat silent
 * installs) before anyone is asked, an update owner that is another app is named as the
 * blocker, and the one notification says it is the last tap.
 */
public class UpdatePolicyCheck {
    static int bad = 0;

    static void eq(String what, Object got, Object want) {
        if (!String.valueOf(got).equals(String.valueOf(want))) {
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
            bad++;
        }
    }

    static UpdatePolicy.Facts phone(String installer, String owner, boolean unknownSources) {
        UpdatePolicy.Facts f = new UpdatePolicy.Facts();
        f.me = "com.aitherium.aither";
        f.installer = installer;
        f.initiator = installer;
        f.updateOwner = owner;
        f.sdk = 37;
        f.targetSdk = 36;
        f.updatePermission = true;
        f.unknownSources = unknownSources;
        return f;
    }

    public static void main(String[] a) {
        // installed from a browser, switch never turned on for Aither: still tries quietly
        UpdatePolicy.Facts browser = phone("com.android.chrome", "", false);
        eq("browser install tries quietly", UpdatePolicy.tryQuiet(browser), true);
        eq("browser install can be silent (self-update)", UpdatePolicy.whyNotSilent(browser), "");
        eq("browser install does not own updates yet", UpdatePolicy.ownsUpdates(browser), false);

        // adb install -r: installer of record is the shell; a self-update is still silent
        UpdatePolicy.Facts adb = phone("com.android.shell", "", true);
        eq("adb install can be silent", UpdatePolicy.whyNotSilent(adb), "");

        // after one self-install Aither is the installer of record
        UpdatePolicy.Facts mine = phone("com.aitherium.aither", "", true);
        eq("own install owns updates", UpdatePolicy.ownsUpdates(mine), true);
        eq("own install with own update owner", UpdatePolicy.ownsUpdates(
                phone("com.aitherium.aither", "com.aitherium.aither", true)), true);

        // PENDING_USER_ACTION on a phone where silent should work: retry quietly, twice
        int pending = UpdatePolicy.STATUS_PENDING_USER_ACTION;
        eq("pending, first try -> retry", UpdatePolicy.after(pending, mine, 1), UpdatePolicy.Next.RETRY_QUIET);
        eq("pending, tries used -> ask", UpdatePolicy.after(pending, mine, UpdatePolicy.QUIET_TRIES),
                UpdatePolicy.Next.ASK);
        eq("success -> done", UpdatePolicy.after(UpdatePolicy.STATUS_SUCCESS, mine, 1), UpdatePolicy.Next.DONE);
        eq("dismissed -> ask, never a quiet loop",
                UpdatePolicy.after(UpdatePolicy.STATUS_FAILURE_ABORTED, mine, 0), UpdatePolicy.Next.ASK);
        eq("invalid apk -> ask", UpdatePolicy.after(UpdatePolicy.STATUS_FAILURE_INVALID, mine, 0),
                UpdatePolicy.Next.ASK);

        // another app owns updates (Android 14+): name it, ask at once, say it is the last tap
        UpdatePolicy.Facts owned = phone("com.android.vending", "com.android.vending", true);
        eq("foreign update owner blocks",
                UpdatePolicy.whyNotSilent(owned).startsWith("updates belong to com.android.vending"), true);
        eq("foreign owner -> ask now", UpdatePolicy.after(pending, owned, 0), UpdatePolicy.Next.ASK);
        eq("foreign owner text", UpdatePolicy.askText(owned), "One last tap: after this, Aither updates itself.");
        eq("browser text", UpdatePolicy.askText(browser), "One last tap: after this, Aither updates itself.");
        eq("own install text", UpdatePolicy.askText(mine), "Tap to install. It is checked and signed by Aitherium.");
        // an update owner on Android 13 is not enforced
        UpdatePolicy.Facts t = phone("com.android.vending", "com.android.vending", true);
        t.sdk = 33;
        eq("owner unenforced before 14", UpdatePolicy.whyNotSilent(t), "");

        // the permission and the Android version still decide
        UpdatePolicy.Facts noPerm = phone("com.aitherium.aither", "", true);
        noPerm.updatePermission = false;
        eq("no permission -> no quiet try", UpdatePolicy.tryQuiet(noPerm), false);
        eq("no permission named", UpdatePolicy.whyNotSilent(noPerm), "UPDATE_PACKAGES_WITHOUT_USER_ACTION is not granted");
        UpdatePolicy.Facts old = phone("com.aitherium.aither", "", true);
        old.sdk = 30;
        eq("android 11 -> no quiet try", UpdatePolicy.tryQuiet(old), false);
        UpdatePolicy.Facts lowTarget = phone("com.aitherium.aither", "", true);
        lowTarget.targetSdk = 30;
        eq("old target named", UpdatePolicy.whyNotSilent(lowTarget).startsWith("targets SDK 30"), true);

        // the package verifier refused it (a supervised child phone): blocked, never a quiet loop
        String vf = "INSTALL_FAILED_VERIFICATION_FAILURE: Install not allowed for file:///data/app/vmdl1.tmp";
        eq("verifier refusal recognised", UpdatePolicy.verifierRefused(UpdatePolicy.STATUS_FAILURE_BLOCKED, vf), true);
        eq("verifier refusal under FAILURE too", UpdatePolicy.verifierRefused(UpdatePolicy.STATUS_FAILURE, vf), true);
        eq("verifier -> blocked", UpdatePolicy.after(UpdatePolicy.STATUS_FAILURE, vf, mine, 0), UpdatePolicy.Next.BLOCKED);
        eq("blocked status -> blocked", UpdatePolicy.after(UpdatePolicy.STATUS_FAILURE_BLOCKED, "", mine, 0),
                UpdatePolicy.Next.BLOCKED);
        eq("other failure unaffected", UpdatePolicy.after(UpdatePolicy.STATUS_FAILURE, "INSTALL_FAILED_INTERNAL_ERROR", mine, 0),
                UpdatePolicy.Next.RETRY_QUIET);
        eq("child text names a parent", UpdatePolicy.blockedText(true).startsWith("a parent must allow"), true);

        // a session this app abandoned itself is never a reason to ask (Fold, 0.3.33)
        String ab = "INSTALL_FAILED_ABORTED: Session was abandoned";
        eq("own abandon -> done", UpdatePolicy.after(UpdatePolicy.STATUS_FAILURE_ABORTED, ab, mine, 1), UpdatePolicy.Next.DONE);
        eq("person dismissed -> ask", UpdatePolicy.after(UpdatePolicy.STATUS_FAILURE_ABORTED, "INSTALL_FAILED_ABORTED: User rejected permissions", mine, 1),
                UpdatePolicy.Next.ASK);

        eq("status name", UpdatePolicy.statusName(-1), "PENDING_USER_ACTION");
        eq("status name unknown", UpdatePolicy.statusName(42), "STATUS_42");

        if (bad > 0) System.exit(1);
        System.out.println("UpdatePolicyCheck ok");
    }
}
