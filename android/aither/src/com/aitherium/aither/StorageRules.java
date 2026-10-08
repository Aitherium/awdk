package com.aitherium.aither;

/**
 * Whether this phone lends disk to the family's mesh storage pool right now, and how much.
 * Pure Java (no Android types), so test/StorageRulesCheck.java runs it on a desktop JVM.
 *
 * Off unless the household says so: the flag is this device's row (storage_share), set by
 * the owner for their own phone or by a guardian for a child's. A quota is required, and
 * the phone lends only on unmetered Wi-Fi while charging; a child's phone always keeps
 * both conditions, whatever its limits say. It never lends more than the quota, 30% of the
 * free space, or anything that would leave less than 5 GiB free.
 */
final class StorageRules {
    static final long GIB = 1L << 30;
    static final long RESERVE_BYTES = 5 * GIB;
    static final double SHARE = 0.30;
    static final long MIN_BYTES = GIB;

    static final class Verdict {
        boolean lent;
        /** Why an opted-in phone is resting ("" when it is not). */
        String paused = "";
        /** Why it is not lending at all ("" when it is, or when it is only paused). */
        String reason = "";
        long contributedBytes;
        long quotaBytes;
    }

    private StorageRules() {}

    static Verdict decide(boolean child, boolean householdOn, int quotaGb, boolean wifiOnly,
                          boolean chargingOnly, boolean unmeteredWifi, boolean charging,
                          boolean powerSave, long freeBytes) {
        Verdict v = new Verdict();
        v.quotaBytes = Math.max(0, quotaGb) * GIB;
        if (!householdOn) {
            v.reason = child ? "a guardian has not turned this on" : "storage sharing is off";
            return v;
        }
        if (quotaGb <= 0) {
            v.reason = "no quota set";
            return v;
        }
        if (powerSave) {
            v.paused = "battery saver is on";
            return v;
        }
        if ((child || wifiOnly) && !unmeteredWifi) {
            v.paused = "not on Wi-Fi";
            return v;
        }
        if ((child || chargingOnly) && !charging) {
            v.paused = "not charging";
            return v;
        }
        long free = Math.max(0, freeBytes);
        long amount = Math.min(v.quotaBytes, Math.min((long) (free * SHARE), free - RESERVE_BYTES));
        if (amount < MIN_BYTES) {
            v.reason = "too little free space";
            return v;
        }
        v.contributedBytes = amount;
        v.lent = true;
        return v;
    }

    /** GiB with two decimals, as the platform's capability detail carries it. */
    static double gib(long bytes) {
        return Math.round(bytes * 100.0 / GIB) / 100.0;
    }
}
