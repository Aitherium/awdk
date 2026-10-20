package com.aitherium.aither;

import android.graphics.Insets;
import android.os.Build;
import android.view.View;
import android.view.WindowInsets;

/**
 * Edge to edge: an app targeting Android 15+ draws under the status and navigation bars and
 * Android 16 removed the opt-out. Each screen pads its root by the bars, the cutout and the
 * keyboard (which also replaces adjustResize), on top of the padding it already had.
 */
final class Edge {
    private Edge() {}

    static void fit(View v) {
        if (Build.VERSION.SDK_INT < 30) return; // before Android 11 nothing draws under the bars
        int l = v.getPaddingLeft(), t = v.getPaddingTop(), r = v.getPaddingRight(), b = v.getPaddingBottom();
        v.setOnApplyWindowInsetsListener((view, ins) -> {
            Insets i = ins.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout()
                    | WindowInsets.Type.ime());
            view.setPadding(l + i.left, t + i.top, r + i.right, b + i.bottom);
            return WindowInsets.CONSUMED;
        });
        v.requestApplyInsets();
    }

    /**
     * The app's main frame (Shell). A child's frame pads like every other screen ({@link #fit}).
     * A grown-up's OS draws edge to edge under transparent bars: the frame pads for the
     * keyboard only, hands the page its safe areas in CSS px (Shell.insets, AppTabs.insetsJs)
     * and passes the bars on to the WebView so env(safe-area-inset-*) can see them where
     * WebView supports it. With the keyboard up the bottom bar is under it: bottom inset 0.
     */
    static void fitShell(View v, Shell s) {
        if (Build.VERSION.SDK_INT < 30) return;
        int l = v.getPaddingLeft(), t = v.getPaddingTop(), r = v.getPaddingRight(), b = v.getPaddingBottom();
        v.setOnApplyWindowInsetsListener((view, ins) -> {
            if (s.child()) {
                Insets i = ins.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout()
                        | WindowInsets.Type.ime());
                view.setPadding(l + i.left, t + i.top, r + i.right, b + i.bottom);
                return WindowInsets.CONSUMED;
            }
            Insets bars = ins.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
            Insets ime = ins.getInsets(WindowInsets.Type.ime());
            boolean keyboard = ime.bottom > 0;
            view.setPadding(l, t, r, b + (keyboard ? ime.bottom : 0));
            float d = view.getResources().getDisplayMetrics().density;
            s.insets(Math.round(bars.top / d), Math.round(bars.right / d),
                    keyboard ? 0 : Math.round(bars.bottom / d), Math.round(bars.left / d));
            WindowInsets.Builder pass = new WindowInsets.Builder(ins)
                    .setInsets(WindowInsets.Type.ime(), Insets.NONE);
            if (keyboard) pass.setInsets(WindowInsets.Type.navigationBars(), Insets.NONE);
            return pass.build();
        });
        v.requestApplyInsets();
    }
}
