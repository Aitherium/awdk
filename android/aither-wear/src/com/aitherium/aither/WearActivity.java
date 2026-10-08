package com.aitherium.aither;

import android.Manifest;
import android.app.Activity;
import android.app.KeyguardManager;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.os.Bundle;
import android.os.SystemClock;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.text.InputType;
import android.text.SpannableStringBuilder;
import android.text.Spanned;
import android.text.style.BackgroundColorSpan;
import android.text.style.ForegroundColorSpan;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.view.inputmethod.EditorInfo;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * Aither on the watch: one round-friendly screen in the watch face's look (WearTheme). Signed
 * out it shows a short code to enter on the phone (WearApi, the web's device grant). Signed in
 * it is a talk button for the chosen agent, the household approvals this account can still
 * answer (Approve / Deny, only from a locking watch that is unlocked: WearRules.guard), and a
 * few one-tap asks.
 *
 * Talking: the on-device recognizer only (Talk's rule); a watch without one gets a text box.
 * The answer STREAMS: each token is shown as it arrives and each finished sentence is spoken
 * at once (WearFeed -> WearVoice), the words lighting up as the voice reaches them, so the
 * text and the voice stay together instead of the voice starting after the whole answer.
 * Every turn logs its timings under the "AitherWearLat" tag.
 *
 * The "Talk to Aither" launcher entry (an alias of this activity) opens straight into
 * listening; it is what the owner can put on the watch's button.
 * Plain views, no AndroidX, no Play Services.
 */
public class WearActivity extends Activity {
    private static final int ASK_MIC = 1;
    private static final String LAT = "AitherWearLat";
    static final String TALK_ALIAS = "com.aitherium.aither.TalkToAither";

    private WearApi api;
    private WearTheme th;
    private SharedPreferences prefs;
    private LinearLayout col;
    private ScrollView scroll;
    /** Bumped on every screen change, so a late answer for an old screen is dropped. */
    private volatile int screen;
    private boolean home = true;
    private boolean talkOnStart;
    private SpeechRecognizer recognizer;
    /** Reads answers aloud in Aither's own voice (WearVoice); the watch's voice as fallback. */
    private WearVoice voice;
    /** When the last words were heard (elapsed ms), for the turn's timings. */
    private long heardAt;
    /** When home was last drawn: a wrist raise within a minute keeps its scroll position. */
    private long homeAt;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        api = new WearApi(this);
        voice = new WearVoice(this, api);
        prefs = getSharedPreferences("wear", MODE_PRIVATE);
        th = WearTheme.load(this);
        scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.setVerticalScrollBarEnabled(true);
        // the crown scrolls a focused ScrollView (rotary input)
        scroll.setFocusable(true);
        scroll.setFocusableInTouchMode(true);
        col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setGravity(Gravity.CENTER_HORIZONTAL);
        // a round face: keep text inside the inscribed square (inset = (1 - 1/sqrt 2) / 2)
        int w = getResources().getDisplayMetrics().widthPixels;
        boolean round = getResources().getConfiguration().isScreenRound();
        int side = round ? Math.round(w * 0.146f) : Ui.dp(this, 8);
        col.setPadding(side, round ? Math.round(side * 0.7f) : Ui.dp(this, 8), side, round ? side * 2 : Ui.dp(this, 16));
        scroll.addView(col);
        setContentView(scroll);
        paint();
        talkOnStart = wantsTalk(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (wantsTalk(intent)) talkOnStart = true;
    }

    private static boolean wantsTalk(Intent i) {
        return i != null && i.getComponent() != null && TALK_ALIAS.equals(i.getComponent().getClassName());
    }

    @Override
    protected void onResume() {
        super.onResume();
        scroll.requestFocus();
        if (talkOnStart && !api.token().isEmpty()) {
            talkOnStart = false;
            talk();
        } else if (home && (col.getChildCount() == 0 || SystemClock.elapsedRealtime() - homeAt > 60_000)) {
            render();
        }
    }

    @Override
    protected void onDestroy() {
        if (recognizer != null) recognizer.destroy();
        if (voice != null) voice.shutdown();
        super.onDestroy();
    }

    @Override
    public void onBackPressed() {
        if (home) {
            super.onBackPressed();
        } else {
            voice.stop();
            if (recognizer != null) recognizer.cancel();
            render();
        }
    }

    // ------------------------------------------------------------------ building blocks

    private void paint() {
        scroll.setBackgroundColor(th.bg);
        getWindow().getDecorView().setBackgroundColor(th.bg);
    }

    private int clear(boolean isHome) {
        home = isHome;
        if (isHome) homeAt = SystemClock.elapsedRealtime();
        // a conversation keeps the screen on (the answer is read while it is spoken)
        if (isHome) getWindow().clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        else getWindow().addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        col.removeAllViews();
        scroll.scrollTo(0, 0);
        return ++screen;
    }

    private TextView line(String s, float sp, int color) {
        TextView t = Ui.text(this, s, sp, color);
        t.setGravity(Gravity.CENTER_HORIZONTAL);
        t.setPadding(0, Ui.dp(this, 4), 0, Ui.dp(this, 4));
        col.addView(t, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));
        return t;
    }

    private TextView button(String label, boolean primary, View.OnClickListener l) {
        TextView b = Ui.text(this, label, 15, primary ? th.onAccent : th.ink);
        b.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        b.setGravity(Gravity.CENTER);
        b.setMinHeight(Ui.dp(this, 48)); // Wear's touch target
        b.setPadding(Ui.dp(this, 12), Ui.dp(this, 8), Ui.dp(this, 12), Ui.dp(this, 8));
        b.setBackground(Ui.round(this, primary ? th.accent : th.raised, 24, primary ? 0 : th.line));
        b.setOnClickListener(l);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.topMargin = Ui.dp(this, 6);
        col.addView(b, lp);
        return b;
    }

    /** A small section label, the watch face's mono caps. */
    private void label(String s) {
        TextView t = line(s.toUpperCase(Locale.ROOT), 10, th.faint);
        t.setTypeface(Typeface.create("monospace", Typeface.NORMAL));
        t.setLetterSpacing(0.12f);
        t.setPadding(0, Ui.dp(this, 12), 0, Ui.dp(this, 2));
    }

    /** Off the main thread; what it shows goes through onUi. */
    private static void work(Runnable job) {
        new Thread(job, "aither-wear").start();
    }

    /** On the main thread, and only while the screen {@code at} is still the one shown. */
    private void onUi(int at, Runnable r) {
        runOnUiThread(() -> { if (at == screen) r.run(); });
    }

    private String agent() {
        return WearApi.agentId(prefs.getString("agent", WearApi.AGENT));
    }

    // ------------------------------------------------------------------ screens

    private void render() {
        if (api.token().isEmpty()) signedOut(); else signedIn();
    }

    private void signedOut() {
        clear(true);
        brand();
        line("Sign in with your phone to talk to Aither and answer your home's requests.", 13, th.dim);
        button("Sign in", true, v -> signIn());
        if (!WearBlePair.linked(this) && !new WearNodeLink(this).linked()) {
            button("Add to my devices", false, v -> startActivity(new Intent(this, WearBlePair.class)));
        }
    }

    /** The wordmark: "aither" with the accent dot, as on the watch face. */
    private void brand() {
        SpannableStringBuilder b = new SpannableStringBuilder("aither •");
        b.setSpan(new ForegroundColorSpan(th.accent), 7, 8, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        TextView t = line("", 18, th.ink);
        t.setText(b);
        t.setTypeface(Typeface.create("sans-serif-light", Typeface.NORMAL));
        t.setLetterSpacing(0.08f);
    }

    private void signIn() {
        int at = clear(false);
        TextView status = line("Getting a code…", 13, th.dim);
        work(() -> {
            WearApi.Resp r = api.deviceCode();
            String device = r.str("device_code");
            if (r.code != 200 || device.isEmpty()) {
                onUi(at, () -> {
                    status.setText(WearRules.signInError(r.code, r.str("error")));
                    button("Back", false, v -> render());
                });
                return;
            }
            String code = WearRules.showCode(r.str("user_code"));
            String where = r.str("verification_uri").replaceFirst("^https?://", "");
            String page = where.isEmpty() ? WearApi.VERIFY : where;
            long until = System.currentTimeMillis() + Math.max(60, r.json.optInt("expires_in", 900)) * 1000L;
            // The QR is the App Link the phone's camera opens straight into Aither's approve
            // sheet (DeviceLink); the code under it is for typing in Settings > Link a device.
            String link = DeviceLink.linkUrl(code);
            onUi(at, () -> {
                if (!link.isEmpty()) {
                    status.setText("Scan with your phone's camera");
                    col.addView(qrView(link));
                    TextView big = line(code, 18, th.ink);
                    big.setTypeface(Typeface.create("monospace", Typeface.BOLD));
                    line("or in Aither on your phone: Settings > Link a device", 11, th.dim);
                } else {
                    status.setText("On your phone, open");
                    line(page, 13, th.accent);
                    TextView big = line(code, 24, th.ink);
                    big.setTypeface(Typeface.create("monospace", Typeface.BOLD));
                    line("and enter this code", 13, th.dim);
                }
            });
            int interval = Math.max(5, r.json.optInt("interval", 5));
            while (at == screen && System.currentTimeMillis() < until) {
                try { Thread.sleep(interval * 1000L); } catch (InterruptedException e) { return; }
                if (at != screen) return;
                WearApi.Resp t = api.deviceToken(device);
                String token = t.str("access_token");
                if (t.code == 200 && !token.isEmpty()) {
                    api.keep(token, t.json.optLong("expires_in", 0));
                    onUi(at, this::render);
                    return;
                }
                if (!WearRules.keepPolling(t.code)) {
                    onUi(at, () -> {
                        status.setText(WearRules.signInError(t.code, t.str("error")));
                        button("Try again", true, v -> signIn());
                    });
                    return;
                }
                interval = WearRules.nextInterval(interval, t.code, t.json == null ? 0 : t.json.optInt("interval", 0));
            }
            onUi(at, () -> {
                status.setText(WearRules.signInError(400, "expired_token"));
                button("Try again", true, v -> signIn());
            });
        });
    }

    /** The QR as a crisp bitmap (one pixel a module, scaled without smoothing), white with
     *  a 4-module quiet zone so a phone camera finds it on the watch's black face. */
    private View qrView(String text) {
        boolean[][] m = Qr.encode(text);
        int quiet = 4, n = m.length + quiet * 2;
        int[] px = new int[n * n];
        java.util.Arrays.fill(px, 0xFFFFFFFF);
        for (int y = 0; y < m.length; y++) {
            for (int x = 0; x < m.length; x++) {
                if (m[y][x]) px[(y + quiet) * n + x + quiet] = 0xFF000000;
            }
        }
        android.graphics.Bitmap bmp = android.graphics.Bitmap.createBitmap(px, n, n, android.graphics.Bitmap.Config.ARGB_8888);
        int side = Math.round(getResources().getDisplayMetrics().widthPixels * 0.62f) / n * n;
        android.graphics.drawable.BitmapDrawable d = new android.graphics.drawable.BitmapDrawable(getResources(), bmp);
        d.setFilterBitmap(false);
        d.setAntiAlias(false);
        android.widget.ImageView iv = new android.widget.ImageView(this);
        iv.setImageDrawable(d);
        iv.setScaleType(android.widget.ImageView.ScaleType.FIT_XY);
        iv.setContentDescription("Sign-in code " + text);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(side, side);
        lp.gravity = Gravity.CENTER_HORIZONTAL;
        lp.topMargin = Ui.dp(this, 4);
        iv.setLayoutParams(lp);
        return iv;
    }

    private void signedIn() {
        int at = clear(true);
        brand();
        // the talk orb: tap to talk to the chosen agent, the name under it picks another
        TextView orb = Ui.text(this, "Talk", 16, th.onAccent);
        orb.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        orb.setGravity(Gravity.CENTER);
        GradientDrawable disc = new GradientDrawable();
        disc.setShape(GradientDrawable.OVAL);
        disc.setColor(th.accent);
        disc.setStroke(Ui.dp(this, 6), th.glow);
        orb.setBackground(disc);
        orb.setContentDescription("Talk to " + WearApi.agentName(agent()));
        orb.setOnClickListener(v -> talk());
        orb.setOnLongClickListener(v -> { agents(); return true; });
        int orbSide = Ui.dp(this, 84);
        LinearLayout.LayoutParams olp = new LinearLayout.LayoutParams(orbSide, orbSide);
        olp.gravity = Gravity.CENTER_HORIZONTAL;
        olp.topMargin = Ui.dp(this, 4);
        col.addView(orb, olp);
        TextView who = line(WearApi.agentName(agent()) + "  ›", 13, th.accent);
        who.setOnClickListener(v -> agents());
        who.setMinHeight(Ui.dp(this, 40));
        who.setGravity(Gravity.CENTER);

        label("Quick asks");
        for (String[] q : WearApi.QUICK) {
            button(q[0], false, v -> ask(q[1], q[2].isEmpty() ? agent() : q[2]));
        }

        label("Waiting for you");
        TextView count = line("Checking…", 13, th.dim);
        LinearLayout cards = new LinearLayout(this);
        cards.setOrientation(LinearLayout.VERTICAL);
        col.addView(cards);

        label("Asks for you");
        TextView askCount = line("Checking…", 13, th.dim);
        LinearLayout asks = new LinearLayout(this);
        asks.setOrientation(LinearLayout.VERTICAL);
        col.addView(asks);

        // device join (WearNodeLink): the watch becomes a node on the owner's devices.
        // Joined by EITHER route counts: same-account/QR (WearNodeLink) or Bluetooth
        // (WearBlePair). Checking only the BLE flag left "Add to my devices" on screen
        // after a successful same-account join (owner, 2026-10-08).
        WearNodeLink node = new WearNodeLink(this);
        if (!WearBlePair.linked(this) && !node.linked()) {
            label("This watch");
            button("Add this watch to my devices", false, v ->
                    node.show(this, col, api, clear(false), () -> screen, () -> button("Done", true, x -> render())));
            button("Add nearby (Bluetooth)", false, v -> startActivity(new Intent(this, WearBlePair.class)));
        }

        label("Look");
        button("Style: " + WearTheme.name(WearTheme.STYLES, WearTheme.STYLE_NAMES, th.style), false, v -> {
            WearTheme.save(this, WearTheme.next(WearTheme.STYLES, th.style), th.accentName);
            restyle();
        });
        button("Accent: " + WearTheme.name(WearTheme.ACCENTS, WearTheme.ACCENT_NAMES, th.accentName), false, v -> {
            WearTheme.save(this, th.style, WearTheme.next(WearTheme.ACCENTS, th.accentName));
            restyle();
        });
        TextView spoken = button(voice.on() ? "Spoken answers: on" : "Spoken answers: off", false, null);
        spoken.setOnClickListener(v -> {
            voice.setOn(!voice.on());
            spoken.setText(voice.on() ? "Spoken answers: on" : "Spoken answers: off");
        });
        TextView out = Ui.text(this, "Sign out", 13, th.faint);
        out.setGravity(Gravity.CENTER);
        out.setPadding(0, Ui.dp(this, 16), 0, Ui.dp(this, 8));
        out.setOnClickListener(v -> { api.signOut(); render(); });
        col.addView(out, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));
        work(() -> {
            int[] status = {0};
            Map<String, ApprovalCard> byId = api.inbox(status);
            onUi(at, () -> {
                if (byId == null) {
                    if (status[0] == 401) { render(); return; }
                    count.setText(status[0] == 0 ? "No connection." : "Couldn't read your requests (" + status[0] + ").");
                    return;
                }
                List<ApprovalCard> waiting = WearRules.waiting(byId);
                WearTile.changed(this, "tile_approvals", waiting.size());
                count.setText(WearRules.countLine(waiting.size()));
                for (ApprovalCard c : waiting) cards.addView(card(c));
            });
        });
        work(() -> {
            int[] status = {0};
            List<WearDecision> open = api.decisions(status);
            onUi(at, () -> {
                if (open == null) {
                    askCount.setText(status[0] == 0 ? "No connection." : "Couldn't read your asks (" + status[0] + ").");
                    return;
                }
                WearTile.changed(this, "tile_asks", open.size());
                askCount.setText(open.isEmpty() ? "Nothing waiting." : open.size() == 1 ? "1 ask is waiting."
                        : open.size() + " asks are waiting.");
                for (WearDecision d : WearDecision.order(open)) asks.addView(ask(d));
            });
        });
    }

    private void restyle() {
        th = WearTheme.load(this);
        paint();
        render();
    }

    /** Pick who answers: Aither by default, or one of the owner's agents. */
    private void agents() {
        clear(false);
        label("Talk to");
        String now = agent();
        for (String[] a : WearApi.AGENTS) {
            button(a[1] + (a[0].equals(now) ? "  ✓" : ""), a[0].equals(now), v -> {
                prefs.edit().putString("agent", a[0]).apply();
                render();
            });
        }
    }

    private View card(ApprovalCard c) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(Ui.dp(this, 10), Ui.dp(this, 8), Ui.dp(this, 10), Ui.dp(this, 8));
        box.setBackground(Ui.round(this, th.card, Ui.RADIUS_SM, c.urgent ? th.accent : th.line));
        TextView title = Ui.text(this, c.title.isEmpty() ? "Aither needs your OK" : c.title, 14, th.ink);
        title.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        box.addView(title);
        if (!c.body.isEmpty()) box.addView(Ui.text(this, ApprovalCard.clip(c.body, 140), 12, th.dim));
        TextView status = Ui.text(this, c.statusLine(), 11, th.faint);
        box.addView(status);
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        TextView yes = chip("Approve", true);
        TextView no = chip("Deny", false);
        row.addView(yes, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1));
        LinearLayout.LayoutParams gap = new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1);
        gap.leftMargin = Ui.dp(this, 6);
        row.addView(no, gap);
        box.addView(row);
        yes.setOnClickListener(v -> decide(c, true, status, row));
        no.setOnClickListener(v -> decide(c, false, status, row));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.topMargin = Ui.dp(this, 8);
        box.setLayoutParams(lp);
        return box;
    }

    /** One agent ask: tap an option to answer here, or the card's page on the phone. */
    private View ask(WearDecision d) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(Ui.dp(this, 10), Ui.dp(this, 8), Ui.dp(this, 10), Ui.dp(this, 8));
        box.setBackground(Ui.round(this, th.card, Ui.RADIUS_SM, d.urgent() ? th.accent : th.line));
        TextView title = Ui.text(this, d.title.isEmpty() ? "An agent is asking" : ApprovalCard.clip(d.title, 90), 14, th.ink);
        title.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        box.addView(title);
        if (!d.summary.isEmpty()) box.addView(Ui.text(this, ApprovalCard.clip(d.summary, 140), 12, th.dim));
        TextView status = Ui.text(this, "", 11, th.faint);
        LinearLayout opts = new LinearLayout(this);
        opts.setOrientation(LinearLayout.VERTICAL);
        if (d.onWatch()) {
            for (String[] o : d.options) {
                TextView b = chip(ApprovalCard.clip(o[1], 40), o[0].equals(d.defaultKey));
                LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                        LinearLayout.LayoutParams.WRAP_CONTENT);
                lp.topMargin = Ui.dp(this, 4);
                opts.addView(b, lp);
                b.setOnClickListener(v -> answer(d, o, status, opts));
            }
        } else {
            status.setText("Answer on your phone: " + d.page().replaceFirst("^https://", ""));
        }
        box.addView(opts);
        box.addView(status);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.topMargin = Ui.dp(this, 8);
        box.setLayoutParams(lp);
        return box;
    }

    private void answer(WearDecision d, String[] option, TextView status, View buttons) {
        KeyguardManager km = getSystemService(KeyguardManager.class);
        String why = WearRules.guard(km != null && km.isDeviceSecure(), km == null || km.isDeviceLocked());
        if (why != null) {
            status.setText(why);
            return;
        }
        int at = screen;
        buttons.setVisibility(View.GONE);
        status.setText("Sending…");
        work(() -> {
            WearApi.Resp r = api.answer(d, option[0]);
            String text = WearDecision.outcome(r.code, option[1]);
            if (r.code == 403) text += " " + d.page().replaceFirst("^https://", "");
            String shown = text;
            onUi(at, () -> {
                status.setText(shown);
                if (r.code != 200 && r.code != 403 && r.code != 409 && r.code != 404) buttons.setVisibility(View.VISIBLE);
            });
        });
    }

    private TextView chip(String label, boolean primary) {
        TextView b = Ui.text(this, label, 13, primary ? th.onAccent : th.ink);
        b.setGravity(Gravity.CENTER);
        b.setMinHeight(Ui.dp(this, 40));
        b.setBackground(Ui.round(this, primary ? th.accent : th.raised, 20, primary ? 0 : th.line));
        return b;
    }

    private void decide(ApprovalCard c, boolean allow, TextView status, View buttons) {
        KeyguardManager km = getSystemService(KeyguardManager.class);
        String why = WearRules.guard(km != null && km.isDeviceSecure(), km == null || km.isDeviceLocked());
        if (why != null) {
            status.setText(why);
            return;
        }
        int at = screen;
        buttons.setVisibility(View.GONE);
        status.setText("Sending…");
        work(() -> {
            WearApi.Resp r = api.decide(c, allow);
            ApprovalCard after = r.code == 200 && r.json != null ? WearApi.parse(r.json) : null;
            String text;
            if (after != null && after.matches(c.noticeId, c.digest)) {
                text = after.statusLine();
                String err = r.str("error");
                if (!err.isEmpty() && allow) text += " (" + ApprovalCard.clip(err, 80) + ")";
            } else {
                // the phone's words without its "Tap to open Aither" (there is no Aither page here)
                text = ApprovalCard.refusal(r.code, r.str("detail")).replaceAll(" Tap .*$", "");
            }
            String shown = text;
            onUi(at, () -> status.setText(shown));
        });
    }

    // ------------------------------------------------------------------ talk

    private void talk() {
        voice.stop(); // barge-in: taking the mic silences the last answer
        int at = clear(false);
        TextView who = line(WearApi.agentName(agent()), 11, th.accent);
        who.setTypeface(Typeface.create("monospace", Typeface.NORMAL));
        TextView heard = line("", 15, th.ink);
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            heard.setText("Aither needs the microphone to listen.");
            requestPermissions(new String[] {Manifest.permission.RECORD_AUDIO}, ASK_MIC);
            typed(at);
            return;
        }
        if (onDeviceListening()) listen(at, heard); else listenAither(at, heard);
    }

    /**
     * No on-device recognizer (the Pixel Watch has none): record here, Aither's own
     * speech-to-text hears it (WearMic). Never Google's recognizer.
     */
    private void listenAither(int at, TextView heard) {
        heard.setText("Listening…");
        WearMic mic = new WearMic();
        TextView done = button("Done", true, v -> mic.stop());
        button("Type instead", false, v -> {
            mic.stop();
            int now = clear(false);
            line("Ask " + WearApi.agentName(agent()), 14, th.ink);
            typed(now);
        });
        GradientDrawable ring = new GradientDrawable();
        ring.setShape(GradientDrawable.OVAL);
        ring.setColor(th.glow);
        View dot = new View(this);
        dot.setBackground(ring);
        int d = Ui.dp(this, 56);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(d, d);
        lp.gravity = Gravity.CENTER_HORIZONTAL;
        lp.topMargin = Ui.dp(this, 6);
        col.addView(dot, 2, lp);
        long[] endAt = {0};
        work(() -> {
            String[] err = {null};
            String q = mic.hear(api, new WearMic.Listener() {
                @Override public void level(float v) {
                    onUi(at, () -> { float s = 0.7f + 0.6f * v; dot.setScaleX(s); dot.setScaleY(s); });
                }
                @Override public void sending() {
                    endAt[0] = SystemClock.elapsedRealtime();
                    onUi(at, () -> { heard.setText("Hearing you…"); heard.setTextColor(th.dim); done.setVisibility(View.GONE); });
                }
            }, err);
            long now = SystemClock.elapsedRealtime();
            if (endAt[0] > 0) Log.i(LAT, "recognized ms=" + (now - endAt[0]) + " after end of speech (aither stt)");
            onUi(at, () -> {
                if (q.isEmpty()) {
                    heard.setText(err[0] == null ? "I didn't catch that. Tap Try again." : err[0]);
                    done.setVisibility(View.VISIBLE);
                    done.setText("Try again");
                    done.setOnClickListener(v -> talk());
                    return;
                }
                heardAt = now;
                ask(q, agent());
            });
        });
    }

    /** Android's on-device recognizer only (Talk.canListen): the default one may upload audio. */
    private boolean onDeviceListening() {
        return Build.VERSION.SDK_INT >= 31
                && Talk.canListen(Build.VERSION.SDK_INT, SpeechRecognizer.isOnDeviceRecognitionAvailable(this));
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        super.onRequestPermissionsResult(code, perms, results);
        if (code == ASK_MIC && results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED && !home) {
            talk();
        }
    }

    private void typed(int at) {
        EditText input = new EditText(this);
        input.setHint("Type or speak");
        input.setTextColor(th.ink);
        input.setHintTextColor(th.faint);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_CAP_SENTENCES);
        input.setImeOptions(EditorInfo.IME_ACTION_SEND);
        input.setOnEditorActionListener((v, id, ev) -> {
            String q = input.getText().toString().trim();
            if (!q.isEmpty() && at == screen) { heardAt = SystemClock.elapsedRealtime(); ask(q, agent()); }
            return true;
        });
        col.addView(input, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));
        button("Ask", true, v -> {
            String q = input.getText().toString().trim();
            if (!q.isEmpty()) { heardAt = SystemClock.elapsedRealtime(); ask(q, agent()); }
        });
    }

    private void listen(int at, TextView heard) {
        if (recognizer == null) recognizer = SpeechRecognizer.createOnDeviceSpeechRecognizer(this);
        heard.setText("Listening…");
        long[] endAt = {0};
        TextView stop = button("Type instead", false, v -> {
            recognizer.cancel();
            int now = clear(false);
            line("Ask " + WearApi.agentName(agent()), 14, th.ink);
            typed(now);
        });
        recognizer.setRecognitionListener(new RecognitionListener() {
            @Override public void onResults(Bundle r) {
                if (at != screen) return;
                String q = Talk.heard(r.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION));
                if (q.isEmpty()) {
                    heard.setText(Talk.errorText(SpeechRecognizer.ERROR_NO_MATCH));
                    stop.setText("Try again");
                    stop.setOnClickListener(v -> talk());
                    return;
                }
                heardAt = SystemClock.elapsedRealtime();
                if (endAt[0] > 0) Log.i(LAT, "recognized ms=" + (heardAt - endAt[0]) + " after end of speech");
                ask(q, agent());
            }
            @Override public void onPartialResults(Bundle r) {
                String q = Talk.heard(r.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION));
                if (!q.isEmpty() && at == screen) heard.setText(q);
            }
            @Override public void onError(int error) {
                if (at != screen) return;
                if (error == Talk.ERROR_LANGUAGE_UNAVAILABLE && Build.VERSION.SDK_INT >= 33) {
                    recognizer.triggerModelDownload(speechIntent()); // fetches the pack; sends nothing
                }
                heard.setText(Talk.errorText(error).replace("Tap the mic", "Tap Try again").replace("Tap Talk", "Tap Try again"));
                stop.setText("Try again");
                stop.setOnClickListener(v -> talk());
            }
            @Override public void onReadyForSpeech(Bundle p) {}
            @Override public void onBeginningOfSpeech() {}
            @Override public void onRmsChanged(float db) {}
            @Override public void onBufferReceived(byte[] buf) {}
            @Override public void onEndOfSpeech() {
                endAt[0] = SystemClock.elapsedRealtime();
                if (at == screen) heard.setTextColor(th.dim);
            }
            @Override public void onEvent(int type, Bundle p) {}
        });
        recognizer.startListening(speechIntent());
    }

    private Intent speechIntent() {
        Intent i = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
        i.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
        i.putExtra(RecognizerIntent.EXTRA_LANGUAGE, Locale.getDefault().toLanguageTag());
        i.putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true);
        i.putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true);
        return i;
    }

    /**
     * Ask {@code agentId} and stream the answer: text as it arrives, each finished sentence
     * spoken at once with its words lit as the voice reaches them.
     */
    private void ask(String q, String agentId) {
        voice.stop();
        int at = clear(false);
        if (heardAt == 0) heardAt = SystemClock.elapsedRealtime();
        long t0 = heardAt;
        heardAt = 0;
        TextView who = line(WearApi.agentName(agentId), 11, th.accent);
        who.setTypeface(Typeface.create("monospace", Typeface.NORMAL));
        line(q, 13, th.dim);
        TextView answer = line("…", 15, th.ink);
        answer.setGravity(Gravity.START);
        WearFeed feed = new WearFeed();
        boolean speaking = voice.on();
        // where the voice is: absolute characters in feed.text(), for this display generation
        int[] spokenTo = {0}, spokenGen = {0}, curStart = {0};
        long[] t = new long[5]; // first token, first sentence, first audio, last token, last audio
        Runnable paint = () -> answer.setText(highlight(feed.text(), speaking, spokenGen[0] == feed.gen(),
                curStart[0], spokenTo[0]));
        voice.begin(new WearVoice.Progress() {
            @Override public void speaking(WearFeed.Sentence s, int upTo) {
                onUi(at, () -> {
                    if (s.gen != feed.gen()) return;
                    spokenGen[0] = s.gen;
                    curStart[0] = s.start;
                    spokenTo[0] = s.start + upTo;
                    paint.run();
                });
            }
            @Override public void started() {
                t[2] = SystemClock.elapsedRealtime();
                Log.i(LAT, "first_audio ms=" + (t[2] - t0) + " (after first sentence " + (t[1] > 0 ? t[2] - t[1] : -1) + ")");
            }
            @Override public void done() {
                t[4] = SystemClock.elapsedRealtime();
                Log.i(LAT, "last_audio ms=" + (t[4] - t0) + " (after last token " + (t[3] > 0 ? t[4] - t[3] : -1) + ")");
                onUi(at, () -> { spokenTo[0] = feed.text().length(); spokenGen[0] = feed.gen(); paint.run(); });
            }
        });
        TextView[] after = new TextView[1];
        work(() -> {
            Log.i(LAT, "ask agent=" + agentId + " chars=" + q.length());
            String[] err = {null};
            int code = api.stream(q, agentId, new WearApi.Stream() {
                @Override public void segment(String kind) {
                    onUi(at, () -> { voice.addAll(feed.segment(kind)); paint.run(); });
                }
                @Override public void token(String text) {
                    long now = SystemClock.elapsedRealtime();
                    if (t[0] == 0) { t[0] = now; Log.i(LAT, "first_token ms=" + (now - t0)); }
                    t[3] = now;
                    onUi(at, () -> {
                        List<WearFeed.Sentence> done = feed.token(text);
                        if (!done.isEmpty() && t[1] == 0) {
                            t[1] = SystemClock.elapsedRealtime();
                            Log.i(LAT, "first_sentence ms=" + (t[1] - t0));
                        }
                        voice.addAll(done);
                        paint.run();
                    });
                }
                @Override public void segmentEnd() {
                    onUi(at, () -> {
                        List<WearFeed.Sentence> done = feed.flush();
                        if (!done.isEmpty() && t[1] == 0) t[1] = SystemClock.elapsedRealtime();
                        voice.addAll(done);
                        paint.run();
                        showAfter(at, after);
                    });
                }
                @Override public void answer(String text) {
                    onUi(at, () -> { voice.addAll(feed.last(text)); paint.run(); });
                }
                @Override public void error(String why) { err[0] = why; }
            }, () -> at == screen);
            Log.i(LAT, "stream_closed ms=" + (SystemClock.elapsedRealtime() - t0) + " code=" + code);
            onUi(at, () -> {
                voice.addAll(feed.flush());
                voice.finish();
                if (feed.text().trim().isEmpty()) {
                    answer.setText(code == 401 ? "Signed out. Sign in again."
                            : code == 0 ? "No connection."
                            : code != 200 ? "Aither answered " + code + "."
                            : err[0] != null ? err[0] : "No answer came back.");
                    if (api.token().isEmpty()) { button("Sign in", true, v -> render()); return; }
                }
                showAfter(at, after);
            });
        });
    }

    /** The buttons under an answer, once (at the first segment end, or when the stream closes). */
    private void showAfter(int at, TextView[] after) {
        if (after[0] != null || at != screen) return;
        after[0] = button("Ask again", true, v -> talk());
        button("Done", false, v -> { voice.stop(); render(); });
    }

    /**
     * The answer as shown: what the voice has said in full ink, the word being said in the
     * accent, what is still to come dimmed. Without speech, all of it in full ink.
     */
    private CharSequence highlight(String text, boolean speaking, boolean sameGen, int wordFrom, int upTo) {
        SpannableStringBuilder b = new SpannableStringBuilder(text);
        if (!speaking || text.isEmpty()) return b;
        int said = sameGen ? Math.min(upTo, text.length()) : 0;
        if (said < text.length()) {
            b.setSpan(new ForegroundColorSpan(th.dim), said, text.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        }
        if (said > 0 && said < text.length()) {
            int w = said;
            while (w > Math.max(0, wordFrom) && !Character.isWhitespace(text.charAt(w - 1))) w--;
            if (w < said) {
                b.setSpan(new ForegroundColorSpan(th.onAccent), w, said, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
                b.setSpan(new BackgroundColorSpan(th.accent), w, said, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
            }
        }
        return b;
    }
}
