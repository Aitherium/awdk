package com.aitherium.aither;

/**
 * Family Shield's watch, as plain logic (no Android types, so test/ShieldStateCheck.java runs
 * it on a desktop JDK): given what this phone sees, is the filter on or off, does the
 * household need telling, and can the phone fix it alone or must the child tap?
 *
 * Without device-owner mode the child can switch the VPN off in Settings, or another VPN app
 * can take it over; both end in VpnService.onRevoke and Android's consent being gone. The
 * filter can also just be down with consent still held (killed, never restarted): the phone
 * restarts it itself, and past GRACE_MS it reports "off" and asks the child too (a restart
 * from the background can be refused; opening Aither brings it to the front, where it works).
 */
final class ShieldState {
    static final String ON = "on";
    static final String OFF = "off";
    /** A filter that is down with consent held gets this long to come back on its own. */
    static final long GRACE_MS = 2 * 60_000L;

    /** What the watch should do now. */
    static final class Verdict {
        /** "on", "off", or "" when the household does not ask this phone to filter. */
        final String state;
        /** For "off": other_vpn | revoked | no_permission | not_accepted | not_running. */
        final String reason;
        /** Tell the household (state or reason differs from what it last heard). */
        final boolean report;
        /** Ask the child to turn it back on (a tap opens Aither, which can). */
        final boolean nudge;
        /** Start the filter again: consent is still held, nobody needs to tap. */
        final boolean restart;
        /** Look again in this many ms (the end of the grace), 0 = the periodic job will do. */
        final long recheckIn;

        Verdict(String state, String reason, boolean report, boolean nudge, boolean restart,
                long recheckIn) {
            this.state = state;
            this.reason = reason;
            this.report = report;
            this.nudge = nudge;
            this.restart = restart;
            this.recheckIn = recheckIn;
        }

        /** What to remember the household heard: "on" or "off:<reason>". */
        String said() {
            return OFF.equals(state) ? OFF + ":" + reason : state;
        }
    }

    private ShieldState() {}

    /**
     * @param wanted    the household's mode for this phone is not off
     * @param running   this app's filter tunnel is up
     * @param consent   Android's VPN consent is held (VpnService.prepare is null)
     * @param otherVpn  another VPN is up on this phone
     * @param revoked   onRevoke ran since the filter last came up
     * @param reported  what the household last heard from this phone ("on", "off:<reason>",
     *                  or "" when this install has never told it anything)
     * @param offSince  when this phone first saw the filter down (0 = not yet)
     * @param now       wall clock, ms
     */
    static Verdict decide(boolean wanted, boolean running, boolean consent, boolean otherVpn,
                          boolean revoked, String reported, long offSince, long now) {
        if (!wanted) return new Verdict("", "", false, false, false, 0);
        // "on" whenever the household may not have it: after an "off", and after this install
        // forgot (cleared data, reinstall) — the household ignores an "on" it has no "off" for
        if (running) return new Verdict(ON, "", !ON.equals(reported), false, false, 0);
        // never "on" on this install: the prompt was not accepted yet, nothing was removed
        boolean neverOn = reported.isEmpty() || (OFF + ":not_accepted").equals(reported);
        // another VPN up is the cause even though it reached us as onRevoke
        String reason = otherVpn ? "other_vpn" : revoked ? "revoked"
                : !consent ? (neverOn ? "not_accepted" : "no_permission")
                : "not_running";
        boolean fixable = consent && !otherVpn && !revoked;
        // the child's own act is reported at once; a filter we can restart gets its grace
        long waited = offSince > 0 ? now - offSince : 0;
        boolean due = !fixable || (offSince > 0 && waited >= GRACE_MS);
        boolean report = due && !(OFF + ":" + reason).equals(reported);
        return new Verdict(OFF, reason, report, due, fixable, due ? 0 : GRACE_MS - waited);
    }
}
