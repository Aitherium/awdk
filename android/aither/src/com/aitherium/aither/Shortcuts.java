package com.aitherium.aither;

import android.content.Context;
import android.content.Intent;
import android.content.pm.ShortcutInfo;
import android.content.pm.ShortcutManager;
import android.graphics.drawable.Icon;
import android.net.Uri;

import java.util.ArrayList;
import java.util.List;

/**
 * The launcher shortcuts (long-press the icon), by who uses this phone. One app, one icon:
 * every shortcut opens a page of AitherOS in MainActivity (which routes it to the right
 * tab, AppTabs.route), or this phone's settings.
 *
 * The rows mirror the AitherOS page's own shortcut table; a test on that side reads this
 * file and fails when the two drift. A child gets
 * their own (Learn, My Sprite, Quests, Room, My Space) and no settings; everyone else gets the
 * grown-up set. Dynamic, not res/xml: a manifest shortcut can never be withdrawn, so a
 * child's phone would always offer the grown-up ones.
 *
 * Who is a child comes from the page: AitherOS sets localStorage aither.device.child once a
 * child identity signs in on this device and clears it only
 * for a provably adult one. MainActivity reads it with the household keys.
 */
final class Shortcuts {
    /** id, label, path on the app origin. */
    static final String[][] ADULT = {
        {"hearth", "Hearth", "/?app=hearth"},
        {"family", "Family", "/?app=family"},
        {"sprite", "Sprite", "/?app=sprite"},
        {"learn", "Learn", "/?app=learn"},
        {"academy", "Academy", "/?app=academy"},
        {"spaces", "Spaces", "/?app=spaces"},
    };
    static final String[][] CHILD = {
        {"learn", "Learn", "/learn"},
        {"sprite", "My Sprite", "/learn/sprite"},
        {"quests", "Quests", "/learn/sprite#quests"},
        {"room", "Room", "/hearth#room"},
        {"space", "My Space", "/hearth"},
    };
    static final String ORIGIN = "https://app.aitherium.com";

    private Shortcuts() {}

    /** The shortcut's own tile (res/mipmap-xxxhdpi/ic_shortcut_<id>.png, generated with the
     *  app icon by apply_aither_icon.py). R, not a name lookup: a missing tile fails the build.
     *  The app shell draws the same glyphs from vectors (Shell.iconRes), not these tiles. */
    static int iconRes(String id) {
        switch (id) {
            case "hearth": return R.mipmap.ic_shortcut_hearth;
            case "family": return R.mipmap.ic_shortcut_family;
            case "sprite": return R.mipmap.ic_shortcut_sprite;
            case "learn": return R.mipmap.ic_shortcut_learn;
            case "academy": return R.mipmap.ic_shortcut_academy;
            case "spaces": return R.mipmap.ic_shortcut_spaces;
            case "quests": return R.mipmap.ic_shortcut_quests;
            case "room": return R.mipmap.ic_shortcut_room;
            case "space": return R.mipmap.ic_shortcut_space;
            case "phone": return R.mipmap.ic_shortcut_phone;
            default: return R.mipmap.ic_launcher;
        }
    }

    private static Icon icon(Context c, String id) {
        return Icon.createWithResource(c, iconRes(id));
    }

    /** The launcher id. Prefixed: up to 0.3.4 hearth/family/learn/phone were res/xml
     *  shortcuts, and a pinned one of those is immutable once the manifest drops it, so
     *  reusing the bare id would make setDynamicShortcuts throw. */
    static String sid(String id) { return "s." + id; }

    /** The App Actions capability declared in res/xml/shortcuts.xml. */
    static final String OPEN_APP_FEATURE = "actions.intent.OPEN_APP_FEATURE";

    /** Lets an assistant match "open <label> in Aither" to this shortcut (API 33+). */
    private static void bindFeature(ShortcutInfo.Builder b, String label) {
        if (android.os.Build.VERSION.SDK_INT < 33) return;
        b.addCapabilityBinding(new android.content.pm.Capability.Builder(OPEN_APP_FEATURE).build(),
                new android.content.pm.CapabilityParams.Builder("feature", label).build());
    }

    /** Publish the set for this role; replaces whatever set was there. */
    static void apply(Context c, boolean child) {
        ShortcutManager sm = c.getSystemService(ShortcutManager.class);
        if (sm == null) return;
        List<String> phone = java.util.Collections.singletonList(sid("phone"));
        // The grown-up's "This phone" pin is switched off for a child and back on for an
        // adult (enable BEFORE the set: a disabled shortcut cannot be republished).
        try {
            if (child) sm.disableShortcuts(phone, "Not on a child's phone");
            else sm.enableShortcuts(phone);
        } catch (RuntimeException e) { /* never pinned */ }
        List<ShortcutInfo> out = new ArrayList<>();
        int rank = 0;
        for (String[] s : child ? CHILD : ADULT) {
            Intent i = new Intent(Intent.ACTION_VIEW, Uri.parse(ORIGIN + s[2])).setClass(c, MainActivity.class);
            ShortcutInfo.Builder sb = new ShortcutInfo.Builder(c, sid(s[0])).setShortLabel(s[1])
                    .setIcon(icon(c, s[0])).setIntent(i).setRank(rank++);
            bindFeature(sb, s[1]);
            out.add(sb.build());
        }
        if (!child) {
            Intent i = new Intent(Intent.ACTION_VIEW).setClass(c, SettingsActivity.class);
            out.add(new ShortcutInfo.Builder(c, sid("phone")).setShortLabel("This phone")
                    .setIcon(icon(c, "phone")).setIntent(i).setRank(rank).build());
        }
        int max = sm.getMaxShortcutCountPerActivity();
        try {
            sm.setDynamicShortcuts(out.size() > max ? out.subList(0, max) : out);
        } catch (RuntimeException e) { /* rate-limited or launcher refused: keep the old set */ }
    }
}
