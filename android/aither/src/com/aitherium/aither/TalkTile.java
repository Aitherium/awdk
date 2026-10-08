package com.aitherium.aither;

import android.app.PendingIntent;
import android.os.Build;
import android.service.quicksettings.Tile;
import android.service.quicksettings.TileService;

/**
 * The "Talk to Aither" Quick Settings tile: one tap opens the assistant (AssistActivity),
 * the same screen as the widget's Talk button. Only the system binds it
 * (BIND_QUICK_SETTINGS_TILE); it holds no state, so it is always shown inactive.
 */
public class TalkTile extends TileService {
    @Override
    public void onStartListening() {
        Tile t = getQsTile();
        if (t == null) return;
        t.setState(Tile.STATE_INACTIVE);
        t.updateTile();
    }

    @Override
    public void onClick() {
        if (isLocked()) unlockAndRun(this::open); // the assistant runs tools: never over the lock screen
        else open();
    }

    @SuppressWarnings("deprecation")
    private void open() {
        // Android 14+ refuses the Intent form for an app targeting 34+; earlier ones lack the
        // PendingIntent form
        if (Build.VERSION.SDK_INT >= 34) startActivityAndCollapse(AitherWidget.talk(this));
        else startActivityAndCollapse(AitherWidget.talkIntent(this));
    }
}
