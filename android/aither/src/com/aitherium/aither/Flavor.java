package com.aitherium.aither;

import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.provider.Settings;

/**
 * Which channel this build ships through. build.py --store compiles a copy with STORE = true:
 * the Google Play build. Play updates the app itself (its policy forbids an app replacing its
 * own code), so that build never self-updates, and it does not hold
 * REQUEST_IGNORE_BATTERY_OPTIMIZATIONS (a restricted permission): it opens Android's battery
 * settings list instead, where the owner turns optimisation off for Aither in one tap.
 */
final class Flavor {
    static final boolean STORE = false;

    private Flavor() {}

    /** Android's "let Aither run in the background" screen for this build. */
    static Intent battery(Context c) {
        if (STORE) return new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS);
        return new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                Uri.parse("package:" + c.getPackageName()));
    }
}
