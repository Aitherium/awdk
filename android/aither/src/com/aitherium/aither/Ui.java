package com.aitherium.aither;

import android.content.Context;
import android.content.res.ColorStateList;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.view.Gravity;
import android.view.View;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.Switch;
import android.widget.TextView;

/**
 * The native screens in the Aither brand kit: the tokens of the brand's design spec, the
 * same derived values the apps' ui-core tokens.css carries (void ground, surface cards,
 * element-cyan accent, the three text levels, 16dp panel / 10dp compact radii, the 4-8-12-
 * 16-24 spacing scale). Never a hex of our own: change the brand there, then here.
 * Type: the brand face is Inter, which Android does not ship; the system sans stands in,
 * with Inter's weights (400 body, 500 labels, 600-700 titles). Plain views, no AndroidX.
 */
final class Ui {
    static final int BG = 0xFF000103;      // background / void
    static final int CARD = 0xFF02060D;    // surface
    static final int RAISED = 0xFF040C15;  // surface-raised
    static final int LINE = 0xFF162330;    // border
    static final int INK = 0xFFEEEEEE;     // text-primary
    static final int DIM = 0xFF9BA6B1;     // text-secondary
    static final int FAINT = 0xFF69737D;   // text-dim
    static final int ACCENT = 0xFF2AD7D7;  // aitherium (element cyan)
    static final int PRESS = 0x332AD7D7;   // the accent as a touch ripple
    static final float RADIUS = 16;        // rounded.lg: panels
    static final float RADIUS_SM = 10;     // rounded.md: compact overlays

    private Ui() {}

    static int dp(Context c, float v) {
        return Math.round(v * c.getResources().getDisplayMetrics().density);
    }

    static TextView text(Context c, String s, float sp, int color) {
        TextView t = new TextView(c);
        t.setText(s);
        t.setTextSize(sp);
        t.setTextColor(color);
        return t;
    }

    /** A screen title: big, bold ink. */
    static TextView title(Context c, String s) {
        TextView t = text(c, s, 26, INK);
        t.setTypeface(Typeface.create("sans-serif", Typeface.BOLD));
        return t;
    }

    /** A section heading: small accent capitals with room above it. */
    static TextView section(Context c, String s) {
        TextView t = text(c, s.toUpperCase(java.util.Locale.ROOT), 14, ACCENT);
        t.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        t.setLetterSpacing(0.03f); // label-medium
        t.setPadding(dp(c, 4), dp(c, 24), 0, dp(c, 8));
        return t;
    }

    /** The one plain line that says what the control above it does. */
    static TextView note(Context c, String s) {
        TextView t = text(c, s, 14, DIM);
        t.setPadding(0, 0, 0, dp(c, 10));
        return t;
    }

    /** A rounded card that holds one section's rows. */
    static LinearLayout card(Context c) {
        LinearLayout l = new LinearLayout(c);
        l.setOrientation(LinearLayout.VERTICAL);
        l.setBackground(round(c, CARD, RADIUS, LINE));
        int p = dp(c, 16);
        l.setPadding(p, dp(c, 8), p, dp(c, 4));
        return l;
    }

    static GradientDrawable round(Context c, int fill, float radiusDp, int stroke) {
        GradientDrawable g = new GradientDrawable();
        g.setColor(fill);
        g.setCornerRadius(dp(c, radiusDp));
        if (stroke != 0) g.setStroke(dp(c, 1), stroke);
        return g;
    }

    /** A switch row in the dark theme. */
    static Switch style(Switch s) {
        Context c = s.getContext();
        s.setTextColor(INK);
        s.setTextSize(16);
        s.setPadding(0, dp(c, 10), 0, dp(c, 2));
        int[][] states = {{android.R.attr.state_checked}, {}};
        s.setThumbTintList(new ColorStateList(states, new int[] {ACCENT, DIM}));
        s.setTrackTintList(new ColorStateList(states, new int[] {0x882AD7D7, LINE}));
        return s;
    }

    /** A full-width tappable row: accent label, ripple, rounded. Used in place of Button. */
    static TextView action(Context c, String label, View.OnClickListener l) {
        TextView t = text(c, label, 16, ACCENT);
        t.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        t.setPadding(0, dp(c, 12), 0, dp(c, 2));
        t.setBackground(ripple(c, 0));
        t.setClickable(true);
        t.setOnClickListener(l);
        return t;
    }

    static RippleDrawable ripple(Context c, int fill) {
        GradientDrawable shape = round(c, fill == 0 ? 0x00000000 : fill, RADIUS_SM, 0);
        return new RippleDrawable(ColorStateList.valueOf(PRESS), shape, round(c, 0xFFFFFFFF, RADIUS_SM, 0));
    }

    /** A vector icon (res/drawable ic_nav_*) tinted to the ink colour, 48dp touch target. */
    static ImageView icon(Context c, int res, String desc, View.OnClickListener l) {
        ImageView v = new ImageView(c);
        v.setImageResource(res);
        v.setImageTintList(ColorStateList.valueOf(INK));
        v.setContentDescription(desc);
        v.setScaleType(ImageView.ScaleType.CENTER);
        v.setBackground(new RippleDrawable(ColorStateList.valueOf(PRESS), null, null));
        v.setOnClickListener(l);
        v.setLayoutParams(new LinearLayout.LayoutParams(dp(c, 48), dp(c, 48)));
        return v;
    }

    /** A 56dp bar: [leading icons] title [trailing icons]. The caller adds the icons. */
    static LinearLayout bar(Context c) {
        LinearLayout b = new LinearLayout(c);
        b.setOrientation(LinearLayout.HORIZONTAL);
        b.setGravity(Gravity.CENTER_VERTICAL);
        b.setBackgroundColor(BG);
        b.setPadding(dp(c, 4), 0, dp(c, 4), 0);
        b.setMinimumHeight(dp(c, 56));
        return b;
    }

    /** The title in a bar: one line, takes the free width. */
    static TextView barTitle(Context c) {
        TextView t = text(c, "", 18, INK);
        t.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        t.setSingleLine(true);
        t.setEllipsize(android.text.TextUtils.TruncateAt.END);
        t.setPadding(dp(c, 12), 0, dp(c, 8), 0);
        t.setLayoutParams(new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        return t;
    }

    /** A 1dp divider line. */
    static View rule(Context c) {
        View v = new View(c);
        v.setBackgroundColor(LINE);
        v.setLayoutParams(new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, dp(c, 1)));
        return v;
    }
}
