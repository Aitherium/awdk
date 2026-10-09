package com.aitherium.aither;

import android.app.Activity;
import android.content.Context;
import android.media.AudioManager;
import android.os.Build;

/**
 * Is a phone call going on? Owner, 2026-10-08: "what if alexander is talking on his phone".
 * The page's back-and-forth voice loop (Veil lib/voice-loop.ts) re-opens the mic after each
 * answer; during a call (ringing, cellular, VoIP) nothing may listen. AudioManager's mode says
 * so without any phone-state permission. The app tells its pages (window event "aither:call",
 * window.AitherApp.inCall()), refuses the page's mic, and refuses its own recognizer.
 */
final class Calls {
    private Calls() {}

    /** AudioManager modes that mean a call: RINGTONE 1, IN_CALL 2, IN_COMMUNICATION 3,
     *  CALL_SCREENING 4, CALL_REDIRECT 5, COMMUNICATION_REDIRECT 6. Pure, for CallsCheck. */
    static boolean inCallMode(int mode) {
        return mode >= 1 && mode <= 6;
    }

    static boolean inCall(Context c) {
        AudioManager am = c.getSystemService(AudioManager.class);
        return am != null && inCallMode(am.getMode());
    }

    interface Listener {
        void changed(boolean inCall);
    }

    /** Calls {@code l} when a call starts or ends (Android 12+). Returns a handle for {@link #unwatch}. */
    static Object watch(Activity a, Listener l) {
        if (Build.VERSION.SDK_INT < 31) return null;
        AudioManager am = a.getSystemService(AudioManager.class);
        if (am == null) return null;
        boolean[] last = {inCallMode(am.getMode())};
        AudioManager.OnModeChangedListener m = mode -> {
            boolean now = inCallMode(mode);
            if (now != last[0]) {
                last[0] = now;
                a.runOnUiThread(() -> l.changed(now));
            }
        };
        am.addOnModeChangedListener(a.getMainExecutor(), m);
        return m;
    }

    static void unwatch(Activity a, Object handle) {
        if (handle == null || Build.VERSION.SDK_INT < 31) return;
        AudioManager am = a.getSystemService(AudioManager.class);
        if (am != null) am.removeOnModeChangedListener((AudioManager.OnModeChangedListener) handle);
    }
}
