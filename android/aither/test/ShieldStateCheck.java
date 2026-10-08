package com.aitherium.aither;

/** Exit 0 when ShieldState.decide reports, nudges and restarts exactly when it should. */
public class ShieldStateCheck {
    static int bad = 0;

    static void check(boolean ok, String what) {
        if (!ok) { System.out.println("FAIL " + what); bad++; }
    }

    static final long T = 1_000_000L;

    public static void main(String[] a) {
        ShieldState.Verdict v;

        // the household does not ask for a filter: nothing to say, nothing to do
        v = ShieldState.decide(false, false, false, true, true, "off", T, T);
        check(v.state.isEmpty() && !v.report && !v.nudge && !v.restart, "not wanted is silent");

        // up and running, already said "on": no news
        v = ShieldState.decide(true, true, true, false, false, "on", 0, T);
        check(ShieldState.ON.equals(v.state) && !v.report && !v.nudge, "on stays quiet");
        // back on after an "off": the household hears "on"
        v = ShieldState.decide(true, true, true, false, false, "off:revoked", 0, T);
        check(v.report && !v.nudge && !v.restart, "on after off is reported");
        // this install forgot what it said (cleared data): "on" again, the household dedups
        v = ShieldState.decide(true, true, true, false, false, "", 0, T);
        check(v.report && "on".equals(v.said()), "forgotten state re-sends on");

        // the child switched it off in Settings: reported at once, the child is nudged
        v = ShieldState.decide(true, false, false, false, true, "on", 0, T);
        check(ShieldState.OFF.equals(v.state) && "revoked".equals(v.reason), "revoked reason");
        check(v.report && v.nudge && !v.restart && v.recheckIn == 0,
                "revoked: report now, nudge, no self-restart");
        check("off:revoked".equals(v.said()), "said() carries the reason");
        // switched off while Android still holds consent: still the child's act, not a crash
        v = ShieldState.decide(true, false, true, false, true, "on", 0, T);
        check(v.report && v.nudge && !v.restart, "revoked with consent: report now, nudge");
        // ...and only once: the retry after the household heard it says nothing new
        v = ShieldState.decide(true, false, false, false, true, "off:revoked", T - 10, T);
        check(!v.report && v.nudge, "off already reported: nudge again, no second report");

        // another VPN took over (consent moved to that app)
        v = ShieldState.decide(true, false, false, true, false, "on", 0, T);
        check("other_vpn".equals(v.reason) && v.report && v.nudge && !v.restart, "other vpn");
        // the takeover reaches us as onRevoke too: the other VPN is the cause
        v = ShieldState.decide(true, false, false, true, true, "on", 0, T);
        check("other_vpn".equals(v.reason), "other vpn wins over revoked");
        // a "revoked" already said, then the other VPN is seen: the reason is corrected
        v = ShieldState.decide(true, false, false, true, true, "off:revoked", T, T + 1);
        check(v.report && "other_vpn".equals(v.reason), "a better reason is re-reported");

        // consent gone without onRevoke (e.g. after an app update or a reboot)
        v = ShieldState.decide(true, false, false, false, false, "on", 0, T);
        check("no_permission".equals(v.reason) && v.report && v.nudge, "no permission");
        // never accepted on this phone: not "removed", and stays so across retries
        v = ShieldState.decide(true, false, false, false, false, "", 0, T);
        check("not_accepted".equals(v.reason) && v.report && v.nudge, "not accepted yet");
        v = ShieldState.decide(true, false, false, false, false, "off:not_accepted", T, T + 1);
        check("not_accepted".equals(v.reason) && !v.report, "not accepted: no second report");

        // down with consent held: restart quietly, report only past the grace
        v = ShieldState.decide(true, false, true, false, false, "on", 0, T);
        check("not_running".equals(v.reason) && v.restart && !v.nudge && !v.report
                && v.recheckIn == ShieldState.GRACE_MS,
                "first sight of a down filter: restart, look again at the grace");
        v = ShieldState.decide(true, false, true, false, false, "on", T, T + ShieldState.GRACE_MS - 1);
        check(!v.report && !v.nudge && v.restart && v.recheckIn == 1, "inside the grace: no report");
        v = ShieldState.decide(true, false, true, false, false, "on", T, T + ShieldState.GRACE_MS);
        check(v.report && v.restart && v.nudge && v.recheckIn == 0,
                "past the grace: report, keep restarting, and ask the child");

        if (bad == 0) System.out.println("ShieldStateCheck: ok");
        System.exit(bad == 0 ? 0 : 1);
    }
}
