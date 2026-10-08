package com.aitherium.aither;

import android.content.Context;
import android.content.SharedPreferences;

/**
 * The watch app's look, matching the Aither watch face (../aither-watchface): three styles
 * (Aither, Hearth, Minimal) and the face's four accents (cyan, ember, violet, white). Kept in
 * the wear prefs; Ui's constants stay the phone's brand kit, so the watch reads its colours
 * from here instead.
 */
final class WearTheme {
    static final String[] STYLES = {"aither", "hearth", "minimal"};
    static final String[] STYLE_NAMES = {"Aither", "Hearth", "Minimal"};
    static final String[] ACCENTS = {"style", "cyan", "ember", "violet", "white"};
    static final String[] ACCENT_NAMES = {"Style's own", "Cyan", "Ember", "Violet", "White"};

    final String style, accentName;
    final int bg, card, raised, line, ink, dim, faint, accent, onAccent, glow;

    private WearTheme(String style, String accentName, int bg, int card, int raised, int line,
                      int ink, int dim, int faint, int accent) {
        this.style = style;
        this.accentName = accentName;
        this.bg = bg;
        this.card = card;
        this.raised = raised;
        this.line = line;
        this.ink = ink;
        this.dim = dim;
        this.faint = faint;
        this.accent = accent;
        this.onAccent = luminance(accent) > 0.55 ? 0xFF000103 : 0xFFFFFFFF;
        this.glow = (accent & 0x00FFFFFF) | 0x33000000;
    }

    static WearTheme of(String style, String accentName) {
        int acc = accent(accentName);
        switch (style == null ? "" : style) {
            case "hearth":
                return new WearTheme("hearth", accentName, 0xFF0A0604, 0xFF140C08, 0xFF1E120B, 0xFF2A160C,
                        0xFFFFE9DA, 0xFFFFC9A3, 0xFF9A7A66, acc != 0 ? acc : 0xFFFF8950);
            case "minimal":
                return new WearTheme("minimal", accentName, 0xFF000000, 0xFF0B0B0B, 0xFF141414, 0xFF1E1E1E,
                        0xFFEEEEEE, 0xFF9BA6B1, 0xFF69737D, acc != 0 ? acc : 0xFFEEEEEE);
            default:
                return new WearTheme("aither", accentName, Ui.BG, Ui.CARD, Ui.RAISED, Ui.LINE,
                        Ui.INK, Ui.DIM, Ui.FAINT, acc != 0 ? acc : Ui.ACCENT);
        }
    }

    /** The face's accent colours; 0 for "the style's own". */
    static int accent(String name) {
        switch (name == null ? "" : name) {
            case "cyan": return 0xFF2AD7D7;
            case "ember": return 0xFFFF8950;
            case "violet": return 0xFF907AE9;
            case "white": return 0xFFEEEEEE;
            default: return 0;
        }
    }

    static WearTheme load(Context c) {
        SharedPreferences p = c.getSharedPreferences("wear", Context.MODE_PRIVATE);
        return of(p.getString("style", "aither"), p.getString("accent", "style"));
    }

    static void save(Context c, String style, String accent) {
        c.getSharedPreferences("wear", Context.MODE_PRIVATE).edit()
                .putString("style", style).putString("accent", accent).apply();
    }

    static String name(String[] ids, String[] names, String id) {
        for (int i = 0; i < ids.length; i++) if (ids[i].equals(id)) return names[i];
        return names[0];
    }

    static String next(String[] ids, String id) {
        for (int i = 0; i < ids.length; i++) if (ids[i].equals(id)) return ids[(i + 1) % ids.length];
        return ids[0];
    }

    private static double luminance(int c) {
        return (0.2126 * ((c >> 16) & 0xFF) + 0.7152 * ((c >> 8) & 0xFF) + 0.0722 * (c & 0xFF)) / 255.0;
    }
}
