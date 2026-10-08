package com.aitherium.aither;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.webkit.CookieManager;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONObject;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * "Link a device": sign a watch, TV or laptop in to the account on this phone. The device
 * shows a code and its QR (DeviceLink.linkUrl); the phone camera opens that App Link here
 * (via MainActivity), or the owner types the code (Settings > Link a device). One question,
 * Approve or Cancel, then POST /api/auth/device-authorize with this app's session, the
 * same call the web page makes. A child's phone is refused in words; Identity refuses a
 * child account too (403).
 */
public class LinkActivity extends Activity {
    static final String EXTRA_CODE = "code";
    static final String AUTHORIZE = AppTabs.ORIGIN + "/api/auth/device-authorize";

    private Config cfg;
    private LinearLayout body;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        cfg = new Config(this);
        body = new LinearLayout(this);
        body.setOrientation(LinearLayout.VERTICAL);
        int pad = Ui.dp(this, 16);
        body.setPadding(pad, pad, pad, pad * 2);

        android.widget.ScrollView scroll = new android.widget.ScrollView(this);
        scroll.addView(body);
        LinearLayout screen = new LinearLayout(this);
        screen.setOrientation(LinearLayout.VERTICAL);
        screen.setBackgroundColor(Ui.BG);
        LinearLayout bar = Ui.bar(this);
        bar.addView(Ui.icon(this, R.drawable.ic_nav_back, "Back", v -> finish()));
        TextView title = Ui.barTitle(this);
        title.setText("Link a device");
        bar.addView(title);
        screen.addView(bar);
        screen.addView(Ui.rule(this));
        screen.addView(scroll, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));
        setContentView(screen);
        Edge.fit(screen);
        show(codeOf(getIntent()));
    }

    @Override
    protected void onNewIntent(Intent i) {
        super.onNewIntent(i);
        show(codeOf(i));
    }

    /** The code an intent carries: our extra, or a link (aither://link, the /link pages). */
    static String codeOf(Intent i) {
        if (i == null) return null;
        String c = DeviceLink.normalize(i.getStringExtra(EXTRA_CODE));
        if (c != null) return c;
        Uri u = i.getData();
        return u == null ? null : DeviceLink.codeFrom(u.toString());
    }

    private void show(String code) {
        body.removeAllViews();
        String no = DeviceLink.childRefusal(cfg.profileKind(), cfg.childDevice());
        if (no != null) {
            body.addView(Ui.note(this, no));
            body.addView(Ui.action(this, "Close", v -> finish()));
            return;
        }
        body.addView(Ui.note(this, "Point your phone's camera at the code on your watch, TV or laptop, "
                + "or type the code it shows."));
        EditText box = new EditText(this);
        box.setHint("ABCD-2345");
        box.setTextColor(Ui.INK);
        box.setHintTextColor(Ui.FAINT);
        box.setTextSize(24);
        box.setGravity(Gravity.CENTER);
        box.setTypeface(android.graphics.Typeface.MONOSPACE);
        box.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_CAP_CHARACTERS
                | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        if (code != null) box.setText(code);
        body.addView(box, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));
        TextView ask = Ui.text(this, code == null ? "" : DeviceLink.question(code), 16, Ui.INK);
        ask.setPadding(0, Ui.dp(this, 12), 0, Ui.dp(this, 8));
        body.addView(ask);
        TextView result = Ui.text(this, "", 14, Ui.DIM);
        TextView approve = Ui.action(this, "Approve", null);
        TextView cancel = Ui.action(this, "Cancel", v -> finish());
        approve.setOnClickListener(v -> {
            String c = DeviceLink.codeFrom(box.getText().toString());
            if (c == null) {
                result.setText("That isn't a device code. It looks like ABCD-2345.");
                return;
            }
            ask.setText(DeviceLink.question(c));
            approve.setEnabled(false);
            result.setText("Linking…");
            new Thread(() -> {
                String said = authorize(c);
                runOnUiThread(() -> {
                    result.setText(said);
                    approve.setEnabled(true);
                    if (said.contains("is signed in")) {
                        approve.setVisibility(View.GONE);
                        cancel.setText("Done");
                    }
                });
            }, "aither-link").start();
        });
        body.addView(approve);
        body.addView(cancel);
        body.addView(result);
    }

    /** The approve call with this app's session; the outcome in words. Blocking. */
    private String authorize(String code) {
        String cookies = CookieManager.getInstance().getCookie(AppTabs.ORIGIN);
        if (!Session.hasSession(cookies)) return DeviceLink.refusal(401, "");
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(AUTHORIZE).openConnection();
            c.setConnectTimeout(10000);
            c.setReadTimeout(20000);
            c.setRequestMethod("POST");
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json");
            c.setRequestProperty("Accept", "application/json");
            c.setRequestProperty("Cookie", cookies);
            String token = bearer(cookies);
            if (!token.isEmpty()) c.setRequestProperty("Authorization", "Bearer " + token);
            byte[] out = new JSONObject().put("user_code", code).toString().getBytes(StandardCharsets.UTF_8);
            try (OutputStream o = c.getOutputStream()) { o.write(out); }
            int status = c.getResponseCode();
            JSONObject j = read(status < 400 ? c.getInputStream() : c.getErrorStream());
            if (status == 200 && "authorized".equals(j.optString("status"))) {
                return DeviceLink.approved(j.optString("client_name"));
            }
            return DeviceLink.refusal(status, j.optString("detail", j.optString("error")));
        } catch (Exception e) {
            return DeviceLink.refusal(0, "");
        } finally {
            if (c != null) c.disconnect();
        }
    }

    private static String bearer(String cookies) {
        for (String kv : cookies.split(";")) {
            String s = kv.trim();
            if (s.startsWith(Session.COOKIE + "=")) return s.substring(Session.COOKIE.length() + 1);
        }
        return "";
    }

    private static JSONObject read(InputStream in) {
        if (in == null) return new JSONObject();
        try (InputStream s = in) {
            return new JSONObject(new String(s.readAllBytes(), StandardCharsets.UTF_8));
        } catch (Exception e) {
            return new JSONObject();
        }
    }
}
