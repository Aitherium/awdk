package com.aitherium.aither;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;

import java.io.File;
import java.io.FileNotFoundException;

/**
 * Where the camera app writes the picture DescribeActivity asked it to take: one file in this
 * app's cache, content://com.aitherium.aither.shot/shot.jpg. Not exported; the camera app is
 * let in for that one capture by the intent's FLAG_GRANT_WRITE_URI_PERMISSION. Nothing lands
 * in the gallery and nothing needs the CAMERA or storage permissions.
 */
public class ShotProvider extends ContentProvider {
    static final String AUTHORITY = "com.aitherium.aither.shot";
    static final Uri SHOT = Uri.parse("content://" + AUTHORITY + "/shot.jpg");

    static File file(android.content.Context c) {
        return new File(c.getCacheDir(), "shot.jpg");
    }

    @Override public boolean onCreate() { return true; }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        if (!"/shot.jpg".equals(uri.getPath())) throw new FileNotFoundException(uri.toString());
        return ParcelFileDescriptor.open(file(getContext()), ParcelFileDescriptor.parseMode(mode));
    }

    @Override public String getType(Uri uri) { return "image/jpeg"; }
    @Override public Cursor query(Uri u, String[] p, String s, String[] a, String o) { return null; }
    @Override public Uri insert(Uri u, ContentValues v) { return null; }
    @Override public int delete(Uri u, String s, String[] a) { return 0; }
    @Override public int update(Uri u, ContentValues v, String s, String[] a) { return 0; }
}
