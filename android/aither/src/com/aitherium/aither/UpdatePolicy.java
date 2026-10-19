package com.aitherium.aither;

/**
 * When Android lets this app update itself without a tap, and what to do when it does not.
 * Pure Java (no android.* imports) so test/UpdatePolicyCheck.java runs it on a desktop JVM.
 *
 * Android (PackageInstallerSession.computeUserActionRequirement, 12+) installs a session
 * that asked for USER_ACTION_NOT_REQUIRED with no confirmation when the caller holds
 * UPDATE_PACKAGES_WITHOUT_USER_ACTION and is updating ITSELF, or is the installer of record,
 * or (14+) the update owner; the new APK must target a recent SDK; and an update owner that
 * is some OTHER app always forces the confirmation. Two silent installs of one package
 * within ~30 s are also turned into a confirmation (SilentUpdatePolicy). The
 * "install unknown apps" switch is NOT on that path: it only matters when Android does ask.
 *
 * Measured before 0.3.32: the updater skipped the silent attempt whenever that switch was
 * off (every phone whose first install came from a browser), and treated every
 * STATUS_PENDING_USER_ACTION as final: a notification whose tap built a SECOND session, so
 * each update cost a notification tap plus Android's own Update tap.
 */
final class UpdatePolicy {
    // android.content.pm.PackageInstaller status codes, copied so this runs without Android
    static final int STATUS_PENDING_USER_ACTION = -1;
    static final int STATUS_SUCCESS = 0;
    static final int STATUS_FAILURE = 1;
    static final int STATUS_FAILURE_BLOCKED = 2;
    static final int STATUS_FAILURE_ABORTED = 3;
    static final int STATUS_FAILURE_INVALID = 4;
    static final int STATUS_FAILURE_CONFLICT = 5;
    static final int STATUS_FAILURE_STORAGE = 6;
    static final int STATUS_FAILURE_INCOMPATIBLE = 7;
    static final int STATUS_FAILURE_TIMEOUT = 8;

    /** Quiet retries of one version before Android's confirmation is offered. */
    static final int QUIET_TRIES = 2;
    /** Past Android's ~30 s silent-update throttle, with margin. */
    static final long RETRY_AFTER_MS = 90_000L;
    /** A committed session younger than this is still installing: never start a second one. */
    static final long IN_FLIGHT_MS = 10 * 60_000L;

    /** This app abandoned the session itself (a newer attempt replaced it): nothing to say. */
    static boolean superseded(int status, String msg) {
        return status == STATUS_FAILURE_ABORTED && clean(msg).contains("Session was abandoned");
    }

    /** What the phone knows about this app's install (PackageManager.getInstallSourceInfo). */
    static final class Facts {
        String me = "";
        String installer = "";   // installer of record (installingPackageName)
        String initiator = "";   // initiatingPackageName
        String updateOwner = ""; // API 34+; "" = none
        int sdk;                 // Build.VERSION.SDK_INT
        int targetSdk;           // this app's (and the update's) targetSdkVersion
        boolean updatePermission; // UPDATE_PACKAGES_WITHOUT_USER_ACTION granted
        boolean unknownSources;  // canRequestPackageInstalls(): the "install unknown apps" switch
    }

    enum Next { DONE, RETRY_QUIET, ASK, BLOCKED }

    /**
     * Android's package verifier (Play Protect; on a supervised child account the parent's
     * Family Link) refused the APK: INSTALL_FAILED_VERIFICATION_FAILURE "Install not allowed",
     * public status FAILURE_BLOCKED. Nothing on the phone itself can approve it, so asking the
     * person there again only nags; the old version keeps running and the 6-hourly check
     * tries again after a grown-up allows it.
     */
    static boolean verifierRefused(int status, String msg) {
        String m = clean(msg);
        return m.contains("INSTALL_FAILED_VERIFICATION_FAILURE")
                || (status == STATUS_FAILURE_BLOCKED && m.contains("Install not allowed"));
    }

    /** The state line when the verifier refused. */
    static String blockedText(boolean child) {
        return child
                ? "a parent must allow this update: Android's app check (Play Protect / Family Link) refused it; the current version keeps working"
                : "Android's app check (Play Protect) refused this update; the current version keeps working";
    }

    /** "" when Android should install this app's own update with no tap; else why not. */
    static String whyNotSilent(Facts f) {
        if (f.sdk < 31) return "Android " + f.sdk + " always asks (silent updates start at Android 12)";
        if (!f.updatePermission) return "UPDATE_PACKAGES_WITHOUT_USER_ACTION is not granted";
        if (f.sdk >= 34 && !f.updateOwner.isEmpty() && !f.updateOwner.equals(f.me))
            return "updates belong to " + f.updateOwner + "; Android asks once to move them to Aither";
        if (f.targetSdk > 0 && f.targetSdk < minTargetSdk(f.sdk))
            return "targets SDK " + f.targetSdk + "; Android " + f.sdk + " needs " + minTargetSdk(f.sdk);
        return "";
    }

    /** The oldest targetSdk Android accepts for a silent update (rolling, roughly n-3). */
    static int minTargetSdk(int sdk) {
        if (sdk <= 33) return 29;
        return sdk - 4; // 34 -> 30, 35 -> 31, 36 -> 32, 37 -> 33 (at least as strict as Android)
    }

    /** True when this app is already the installer of record and no other app owns updates. */
    static boolean ownsUpdates(Facts f) {
        return f.me.equals(f.installer) && (f.updateOwner.isEmpty() || f.updateOwner.equals(f.me));
    }

    /**
     * The quiet attempt is always made: the "install unknown apps" switch does not gate it.
     * (Before 0.3.32 a switch that was off skipped straight to the notification.)
     */
    static boolean tryQuiet(Facts f) {
        return f.sdk >= 31 && f.updatePermission;
    }

    /** After a session's status: done, retry quietly later, or ask the person once. */
    static Next after(int status, String msg, Facts f, int quietTriesSoFar) {
        if (superseded(status, msg)) return Next.DONE;
        if (verifierRefused(status, msg)) return Next.BLOCKED;
        return after(status, f, quietTriesSoFar);
    }

    static Next after(int status, Facts f, int quietTriesSoFar) {
        if (status == STATUS_SUCCESS) return Next.DONE;
        if (status == STATUS_FAILURE_BLOCKED) return Next.BLOCKED;
        if (status == STATUS_FAILURE_ABORTED) {
            // the person (or Android) dismissed a confirmation: ask again, never loop quietly
            return Next.ASK;
        }
        boolean transient_ = status == STATUS_PENDING_USER_ACTION || status == STATUS_FAILURE
                || status == STATUS_FAILURE_STORAGE || status == STATUS_FAILURE_TIMEOUT;
        if (transient_ && whyNotSilent(f).isEmpty() && quietTriesSoFar < QUIET_TRIES) return Next.RETRY_QUIET;
        return Next.ASK;
    }

    /** The notification's line when Android does have to ask. */
    static String askText(Facts f) {
        if (!ownsUpdates(f) || !whyNotSilent(f).isEmpty())
            return "One last tap: after this, Aither updates itself.";
        return "Tap to install. It is checked and signed by Aitherium.";
    }

    static String statusName(int st) {
        switch (st) {
            case STATUS_PENDING_USER_ACTION: return "PENDING_USER_ACTION";
            case STATUS_SUCCESS: return "SUCCESS";
            case STATUS_FAILURE: return "FAILURE";
            case STATUS_FAILURE_BLOCKED: return "FAILURE_BLOCKED";
            case STATUS_FAILURE_ABORTED: return "FAILURE_ABORTED";
            case STATUS_FAILURE_INVALID: return "FAILURE_INVALID";
            case STATUS_FAILURE_CONFLICT: return "FAILURE_CONFLICT";
            case STATUS_FAILURE_STORAGE: return "FAILURE_STORAGE";
            case STATUS_FAILURE_INCOMPATIBLE: return "FAILURE_INCOMPATIBLE";
            case STATUS_FAILURE_TIMEOUT: return "FAILURE_TIMEOUT";
            default: return "STATUS_" + st;
        }
    }

    static String clean(String s) {
        return s == null ? "" : s;
    }
}
