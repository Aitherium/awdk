package com.aitherium.aither;

import android.content.Context;
import android.content.SharedPreferences;

import androidx.concurrent.futures.ResolvableFuture;
import androidx.wear.protolayout.ActionBuilders;
import androidx.wear.protolayout.ColorBuilders;
import androidx.wear.protolayout.DimensionBuilders;
import androidx.wear.protolayout.LayoutElementBuilders;
import androidx.wear.protolayout.ModifiersBuilders;
import androidx.wear.protolayout.ResourceBuilders;
import androidx.wear.protolayout.TimelineBuilders;
import androidx.wear.tiles.RequestBuilders;
import androidx.wear.tiles.TileBuilders;
import androidx.wear.tiles.TileService;

import com.google.common.util.concurrent.ListenableFuture;

import java.util.List;
import java.util.Map;

/**
 * The Aither Tile: a swipe from the watch face shows the wordmark, a Talk orb that opens
 * straight into listening ("Talk to Aither", WearActivity.TALK_ALIAS), and how many of the
 * household's approvals and the agents' asks wait for this account; that line opens the app.
 *
 * The counts are read when the tile is asked for (the inbox and /api/decisions, a few seconds
 * at most, off the main thread), else the last ones the app saw. The tile refreshes every 15
 * minutes. Colours follow the app's style and accent (WearTheme).
 */
public class WearTile extends TileService {
    private static final String RES = "1";
    private static final long FRESH_MS = 15 * 60_000L;
    private static final long READ_MS = 6000;

    @Override
    protected ListenableFuture<TileBuilders.Tile> onTileRequest(RequestBuilders.TileRequest req) {
        ResolvableFuture<TileBuilders.Tile> f = ResolvableFuture.create();
        Context c = getApplicationContext();
        new Thread(() -> {
            int[] counts = counts(c);
            try {
                f.set(tile(c, counts));
            } catch (Exception e) {
                f.setException(e);
            }
        }, "aither-tile").start();
        return f;
    }

    @Override
    protected ListenableFuture<ResourceBuilders.Resources> onTileResourcesRequest(RequestBuilders.ResourcesRequest req) {
        ResolvableFuture<ResourceBuilders.Resources> f = ResolvableFuture.create();
        f.set(new ResourceBuilders.Resources.Builder().setVersion(RES).build());
        return f;
    }

    /** {approvals, asks, signed in (1/0)}: read now when it is quick, else the app's last. */
    static int[] counts(Context c) {
        SharedPreferences p = c.getSharedPreferences("wear", Context.MODE_PRIVATE);
        WearApi api = new WearApi(c);
        if (api.token().isEmpty()) return new int[] {0, 0, 0};
        int[] got = {p.getInt("tile_approvals", 0), p.getInt("tile_asks", 0), 1};
        Thread t = new Thread(() -> {
            int[] status = {0};
            Map<String, ApprovalCard> byId = api.inbox(status);
            if (byId != null) got[0] = WearRules.waiting(byId).size();
            List<WearDecision> open = api.decisions(status);
            if (open != null) got[1] = open.size();
            remember(c, got[0], got[1]);
        }, "aither-tile-read");
        t.start();
        try { t.join(READ_MS); } catch (InterruptedException e) { /* the last counts */ }
        return got;
    }

    /** The app keeps the tile's counts current whenever it reads them. */
    static void remember(Context c, int approvals, int asks) {
        c.getSharedPreferences("wear", Context.MODE_PRIVATE).edit()
                .putInt("tile_approvals", approvals).putInt("tile_asks", asks).apply();
    }

    /** One count changed in the app: keep it and ask the tile host for a fresh tile. */
    static void changed(Context c, String key, int n) {
        SharedPreferences p = c.getSharedPreferences("wear", Context.MODE_PRIVATE);
        if (p.getInt(key, -1) == n) return;
        p.edit().putInt(key, n).apply();
        try {
            getUpdater(c).requestUpdate(WearTile.class);
        } catch (Exception e) { /* no tile added: nothing to refresh */ }
    }

    static String waitingLine(int approvals, int asks) {
        if (approvals + asks == 0) return "Nothing waiting";
        StringBuilder b = new StringBuilder();
        if (approvals > 0) b.append(approvals).append(approvals == 1 ? " approval" : " approvals");
        if (asks > 0) {
            if (b.length() > 0) b.append(" · ");
            b.append(asks).append(asks == 1 ? " ask" : " asks");
        }
        return b.toString();
    }

    private TileBuilders.Tile tile(Context c, int[] counts) {
        WearTheme th = WearTheme.load(c);
        LayoutElementBuilders.Column.Builder col = new LayoutElementBuilders.Column.Builder()
                .setHorizontalAlignment(LayoutElementBuilders.HORIZONTAL_ALIGN_CENTER)
                .addContent(text("aither", 16, th.ink, false))
                .addContent(space(8));
        if (counts[2] == 0) {
            col.addContent(clickable(text("Sign in on the watch", 14, th.accent, true), launch(WearActivity.class.getName()), "open"));
        } else {
            col.addContent(orb(th))
                    .addContent(space(10))
                    .addContent(clickable(text(waitingLine(counts[0], counts[1]), 13,
                            counts[0] + counts[1] > 0 ? th.accent : th.dim, false),
                            launch(WearActivity.class.getName()), "waiting"));
        }
        LayoutElementBuilders.Box root = new LayoutElementBuilders.Box.Builder()
                .setWidth(DimensionBuilders.expand())
                .setHeight(DimensionBuilders.expand())
                .setModifiers(new ModifiersBuilders.Modifiers.Builder()
                        .setBackground(new ModifiersBuilders.Background.Builder()
                                .setColor(ColorBuilders.argb(th.bg)).build()).build())
                .addContent(col.build())
                .build();
        return new TileBuilders.Tile.Builder()
                .setResourcesVersion(RES)
                .setFreshnessIntervalMillis(FRESH_MS)
                .setTileTimeline(TimelineBuilders.Timeline.fromLayoutElement(root))
                .build();
    }

    private static LayoutElementBuilders.LayoutElement orb(WearTheme th) {
        float side = 72;
        return new LayoutElementBuilders.Box.Builder()
                .setWidth(DimensionBuilders.dp(side))
                .setHeight(DimensionBuilders.dp(side))
                .setModifiers(new ModifiersBuilders.Modifiers.Builder()
                        .setBackground(new ModifiersBuilders.Background.Builder()
                                .setColor(ColorBuilders.argb(th.accent))
                                .setCorner(new ModifiersBuilders.Corner.Builder()
                                        .setRadius(DimensionBuilders.dp(side / 2)).build()).build())
                        .setBorder(new ModifiersBuilders.Border.Builder()
                                .setWidth(DimensionBuilders.dp(4))
                                .setColor(ColorBuilders.argb(th.glow)).build())
                        .setClickable(new ModifiersBuilders.Clickable.Builder()
                                .setId("talk").setOnClick(launch(WearActivity.TALK_ALIAS)).build())
                        .setSemantics(new ModifiersBuilders.Semantics.Builder()
                                .setContentDescription("Talk to Aither").build())
                        .build())
                .addContent(text("Talk", 15, th.onAccent, true))
                .build();
    }

    private static ActionBuilders.LaunchAction launch(String className) {
        return new ActionBuilders.LaunchAction.Builder()
                .setAndroidActivity(new ActionBuilders.AndroidActivity.Builder()
                        .setPackageName("com.aitherium.aither")
                        .setClassName(className).build())
                .build();
    }

    private static LayoutElementBuilders.LayoutElement clickable(LayoutElementBuilders.Text t,
                                                                ActionBuilders.Action a, String id) {
        return new LayoutElementBuilders.Box.Builder()
                .setModifiers(new ModifiersBuilders.Modifiers.Builder()
                        .setClickable(new ModifiersBuilders.Clickable.Builder().setId(id).setOnClick(a).build())
                        .setPadding(new ModifiersBuilders.Padding.Builder()
                                .setAll(DimensionBuilders.dp(8)).build())
                        .build())
                .addContent(t)
                .build();
    }

    private static LayoutElementBuilders.Text text(String s, float sp, int color, boolean bold) {
        return new LayoutElementBuilders.Text.Builder()
                .setText(s)
                .setMaxLines(2)
                .setFontStyle(new LayoutElementBuilders.FontStyle.Builder()
                        .setSize(DimensionBuilders.sp(sp))
                        .setColor(ColorBuilders.argb(color))
                        .setWeight(bold ? LayoutElementBuilders.FONT_WEIGHT_MEDIUM : LayoutElementBuilders.FONT_WEIGHT_NORMAL)
                        .build())
                .build();
    }

    private static LayoutElementBuilders.LayoutElement space(float dp) {
        return new LayoutElementBuilders.Spacer.Builder().setHeight(DimensionBuilders.dp(dp)).build();
    }
}
