package com.aitherium.aither;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.widget.RemoteViews;

/**
 * The "Aither" home-screen widget (2x1, stretches to 4x1): Talk to Aither opens the
 * assistant (AssistActivity), Learn and Family open those tabs. The tabs go the way a
 * launcher shortcut goes (Shortcuts.ADULT's ?app= paths into MainActivity), so on a
 * child's phone AppTabs.route sends them to the child's homes (CHILD_APP_HOMES) and the
 * assistant says itself that it is off (Config.localAiBlocked).
 *
 * Three fixed buttons, nothing to refresh: no update period, no state, nothing exported
 * (the system delivers APPWIDGET_UPDATE to a receiver that is not).
 */
public class AitherWidget extends AppWidgetProvider {
    /** button id, path on the app origin; the request code is the row's index. */
    static final String[][] TABS = {
        {"learn", "/?app=learn"},
        {"family", "/?app=family"},
    };

    @Override
    public void onUpdate(Context c, AppWidgetManager m, int[] ids) {
        RemoteViews v = views(c);
        for (int id : ids) m.updateAppWidget(id, v);
    }

    static RemoteViews views(Context c) {
        RemoteViews v = new RemoteViews(c.getPackageName(), R.layout.widget_aither);
        v.setOnClickPendingIntent(R.id.widget_talk, talk(c));
        v.setOnClickPendingIntent(R.id.widget_learn, tab(c, 0));
        v.setOnClickPendingIntent(R.id.widget_family, tab(c, 1));
        return v;
    }

    /** The assistant, the same screen long-press power opens. Also the Quick Settings tile's. */
    static Intent talkIntent(Context c) {
        return new Intent(c, AssistActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
    }

    static PendingIntent talk(Context c) {
        return PendingIntent.getActivity(c, 100, talkIntent(c),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    private static PendingIntent tab(Context c, int row) {
        Intent i = new Intent(Intent.ACTION_VIEW, Uri.parse(Shortcuts.ORIGIN + TABS[row][1]))
                .setClass(c, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        return PendingIntent.getActivity(c, 101 + row, i,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }
}
