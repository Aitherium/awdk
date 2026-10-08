package com.aitherium.aither;

/** Desktop-JVM check of StorageRules: opt-in, quota, Wi-Fi + charging, a child's limits. */
public final class StorageRulesCheck {
    private static int failed;

    private static void check(boolean ok, String what) {
        if (!ok) {
            failed++;
            System.out.println("FAIL " + what);
        }
    }

    public static void main(String[] args) {
        long free = 200 * StorageRules.GIB;
        // off until the household turns it on; a child's phone says who decides
        StorageRules.Verdict v = StorageRules.decide(false, false, 32, true, true, true, true, false, free);
        check(!v.lent && v.reason.contains("off"), "adult default off");
        v = StorageRules.decide(true, false, 32, true, true, true, true, false, free);
        check(!v.lent && v.reason.contains("guardian"), "child waits for the guardian");
        // a quota is required
        v = StorageRules.decide(false, true, 0, true, true, true, true, false, free);
        check(!v.lent && v.reason.equals("no quota set"), "no quota, no lending");
        // on Wi-Fi while charging, capped by the quota
        v = StorageRules.decide(false, true, 32, true, true, true, true, false, free);
        check(v.lent && v.contributedBytes == 32 * StorageRules.GIB, "lends the quota");
        // paused, not lost
        v = StorageRules.decide(false, true, 32, true, true, false, true, false, free);
        check(!v.lent && v.paused.equals("not on Wi-Fi"), "paused off Wi-Fi");
        v = StorageRules.decide(false, true, 32, true, true, true, false, false, free);
        check(!v.lent && v.paused.equals("not charging"), "paused off charge");
        v = StorageRules.decide(false, true, 32, true, true, true, true, true, free);
        check(!v.lent && v.paused.contains("battery saver"), "paused in battery saver");
        // an adult may lift the limits; a child's phone never does
        v = StorageRules.decide(false, true, 32, false, false, false, false, false, free);
        check(v.lent, "adult lifted both limits");
        v = StorageRules.decide(true, true, 32, false, false, false, true, false, free);
        check(!v.lent && v.paused.equals("not on Wi-Fi"), "child keeps Wi-Fi only");
        v = StorageRules.decide(true, true, 32, false, false, true, false, false, free);
        check(!v.lent && v.paused.equals("not charging"), "child keeps charging only");
        // never past 30% of free space or into the 5 GiB reserve
        v = StorageRules.decide(false, true, 500, true, true, true, true, false, free);
        check(v.contributedBytes == (long) (free * 0.30), "30% of free");
        // 5.5 GiB free: half a GiB above the 5 GiB reserve, under the 1 GiB minimum
        v = StorageRules.decide(false, true, 32, true, true, true, true, false, 11 * StorageRules.GIB / 2);
        check(!v.lent && v.reason.contains("too little"), "keeps the reserve");
        v = StorageRules.decide(false, true, 32, true, true, true, true, false, 6 * StorageRules.GIB);
        check(v.lent && v.contributedBytes == StorageRules.GIB, "lends down to the reserve");
        check(StorageRules.gib(3 * StorageRules.GIB / 2) == 1.5, "gib rounding");
        if (failed > 0) {
            System.out.println(failed + " failed");
            System.exit(1);
        }
        System.out.println("StorageRulesCheck OK");
    }
}
