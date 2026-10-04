package com.aitherium.aither;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.net.Uri;
import android.webkit.CookieManager;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * "Report": anyone can flag an AI answer (or anything else on screen) as offensive or wrong,
 * from inside the app. Google Play requires it of apps with generative AI. It is offered on
 * selected text in AitherOS (the text-selection menu) and under Ask Aither's answers.
 *
 * The report is filed as platform feedback (a support ticket) with the user's own session;
 * when that is not possible (signed out, offline) the phone's email app opens with the report
 * already written, so a report is never lost silently.
 */
final class Report {
    static final String API = "https://api.aitherium.com/api/feedback/platform";
    static final String EMAIL = "support@aitherium.com";
    static final int MAX = 4000;

    private Report() {}

    /** The report body: what was flagged, where, and what the user said about it. */
    static String body(String flagged, String where, String note) {
        String f = flagged == null ? "" : flagged.trim();
        if (f.length() > MAX) f = f.substring(0, MAX) + "…";
        return "Reported content:\n" + f
                + "\n\nWhere: " + (where == null ? "" : where)
                + "\nApp: Aither for Android " + Config.VERSION + (Flavor.STORE ? " (Google Play)" : "")
                + (note == null || note.trim().isEmpty() ? "" : "\n\nWhat is wrong:\n" + note.trim());
    }

    /** Ask what is wrong, then file the report. */
    static void open(Activity a, String flagged, String where) {
        int pad = (int) (16 * a.getResources().getDisplayMetrics().density);
        LinearLayout col = new LinearLayout(a);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(pad, pad / 2, pad, 0);
        TextView quote = new TextView(a);
        String q = flagged == null ? "" : flagged.trim();
        quote.setText(q.isEmpty() ? "Nothing selected: describe what you saw." : "“"
                + (q.length() > 300 ? q.substring(0, 300) + "…" : q) + "”");
        quote.setPadding(0, 0, 0, pad / 2);
        col.addView(quote);
        EditText note = new EditText(a);
        note.setHint("What is wrong with it? (optional)");
        col.addView(note);
        new AlertDialog.Builder(a)
                .setTitle("Report to Aitherium")
                .setView(col)
                .setNegativeButton("Cancel", null)
                .setPositiveButton("Send report", (d, w) -> {
                    String text = body(flagged, where, note.getText().toString());
                    new Thread(() -> {
                        boolean sent = post(text);
                        a.runOnUiThread(() -> {
                            if (sent) {
                                Toast.makeText(a, "Thanks: your report was sent.", Toast.LENGTH_LONG).show();
                            } else {
                                email(a, text);
                            }
                        });
                    }, "aither-report").start();
                })
                .show();
    }

    /** File it with the user's AitherOS session. True when Aitherium accepted it. */
    static boolean post(String text) {
        try {
            HttpURLConnection c = (HttpURLConnection) new URL(API).openConnection();
            c.setRequestMethod("POST");
            c.setConnectTimeout(15000);
            c.setReadTimeout(20000);
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json");
            c.setRequestProperty("Origin", "https://aitherium.com");
            c.setRequestProperty("User-Agent", NodeLink.UA);
            String cookie = CookieManager.getInstance().getCookie("https://api.aitherium.com");
            if (cookie != null) c.setRequestProperty("Cookie", cookie);
            JSONObject b = new JSONObject().put("type", "complaint")
                    .put("subject", "[AI content report] Aither for Android")
                    .put("body", text).put("page_url", "android-app")
                    .put("metadata", new JSONObject().put("kind", "ai_content_report"));
            try (OutputStream o = c.getOutputStream()) {
                o.write(b.toString().getBytes(StandardCharsets.UTF_8));
            }
            int code = c.getResponseCode();
            return code >= 200 && code < 300;
        } catch (Exception e) {
            return false;
        }
    }

    /** The fallback: the user's email app, report pre-written, sent by them. */
    static void email(Activity a, String text) {
        Intent i = new Intent(Intent.ACTION_SENDTO, Uri.parse("mailto:" + EMAIL))
                .putExtra(Intent.EXTRA_SUBJECT, "[AI content report] Aither for Android")
                .putExtra(Intent.EXTRA_TEXT, text);
        try {
            a.startActivity(i);
        } catch (RuntimeException e) {
            Toast.makeText(a, "Could not send the report. Email " + EMAIL + ".", Toast.LENGTH_LONG).show();
        }
    }
}
