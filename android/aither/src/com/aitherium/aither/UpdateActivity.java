package com.aitherium.aither;

import android.app.Activity;
import android.app.PendingIntent;
import android.content.Intent;
import android.content.pm.PackageInstaller;
import android.net.Uri;
import android.os.Bundle;
import android.provider.Settings;
import android.widget.Toast;

import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.io.OutputStream;

/**
 * The one tap from the update notification: hands the verified APK to Android's installer,
 * which shows its own confirmation. The first time, Android asks once to allow Aither to
 * install updates; coming back here carries on.
 */
public class UpdateActivity extends Activity {
    static final String STATUS = "com.aitherium.aither.INSTALL_STATUS";

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        if (STATUS.equals(getIntent().getAction())) {
            onStatus(getIntent());
            return;
        }
        install();
    }

    @Override
    protected void onNewIntent(Intent i) {
        super.onNewIntent(i);
        if (STATUS.equals(i.getAction())) onStatus(i);
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (asked && getPackageManager().canRequestPackageInstalls()) {
            asked = false;
            install();
        }
    }

    private boolean asked;

    private void install() {
        Updater u = new Updater(this);
        File apk = u.ready(); // re-verified: hash, package, newer, release certificate
        if (apk == null) {
            Toast.makeText(this, "No verified update is waiting", Toast.LENGTH_LONG).show();
            finish();
            return;
        }
        if (!getPackageManager().canRequestPackageInstalls()) {
            asked = true;
            startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:" + getPackageName())));
            return;
        }
        try {
            PackageInstaller pi = getPackageManager().getPackageInstaller();
            PackageInstaller.SessionParams sp =
                    new PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL);
            sp.setAppPackageName(getPackageName());
            int id = pi.createSession(sp);
            try (PackageInstaller.Session s = pi.openSession(id)) {
                try (InputStream in = new FileInputStream(apk);
                     OutputStream o = s.openWrite("aither.apk", 0, apk.length())) {
                    byte[] buf = new byte[1 << 16];
                    int n;
                    while ((n = in.read(buf)) > 0) o.write(buf, 0, n);
                    s.fsync(o);
                }
                Intent back = new Intent(this, UpdateActivity.class).setAction(STATUS);
                PendingIntent cb = PendingIntent.getActivity(this, id, back,
                        PendingIntent.FLAG_MUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
                s.commit(cb.getIntentSender());
            }
        } catch (Exception e) {
            Toast.makeText(this, "Install failed: " + e.getClass().getSimpleName(), Toast.LENGTH_LONG).show();
            finish();
        }
    }

    private void onStatus(Intent i) {
        int st = i.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
        if (st == PackageInstaller.STATUS_PENDING_USER_ACTION) {
            @SuppressWarnings("deprecation") // the typed overload is API 33; this app runs from 29
            Intent confirm = i.getParcelableExtra(Intent.EXTRA_INTENT);
            if (confirm != null) startActivity(confirm);
            return;
        }
        if (st != PackageInstaller.STATUS_SUCCESS) {
            String msg = i.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
            Toast.makeText(this, "Not installed" + (msg == null ? "" : ": " + msg), Toast.LENGTH_LONG).show();
        }
        finish();
    }
}
