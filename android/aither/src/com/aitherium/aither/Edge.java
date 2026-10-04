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
}
