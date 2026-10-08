package com.aitherium.aither;

import android.Manifest;
import android.app.Activity;
import android.app.KeyguardManager;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Typeface;
import android.os.Build;
import android.os.Bundle;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.text.InputType;
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
 * Aither on the watch: one round-friendly screen. Signed out it shows a short code to enter
 * on the phone (WearApi, the web's device grant). Signed in it offers "Talk to Aither" and
 * "Waiting for you": the household approvals this account can still answer, each with
 * Approve / Deny, answered only from a locked-capable watch that is unlocked (WearRules.guard).
 *
 * Listening follows the phone's rule (Talk): the on-device recognizer only; a watch without
 * one gets a text box, whose keyboard offers the watch's own voice typing at the owner's
 * choice. Plain views, no AndroidX, no Play Services.
 */
public class WearActivity extends Activity {
    private static final int ASK_MIC = 1;

    private WearApi api;
    private LinearLayout col;
    private ScrollView scroll;
    /** Bumped on every screen change, so a late answer for an old screen is dropped. */
    private volatile int screen;
    private boolean home = true;
    private SpeechRecognizer recognizer;
    /** Reads answers aloud in Aither's own voice (WearVoice); the watch's voice as fallback. */
    private WearVoice voice;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        api = new WearApi(this);
        voice = new WearVoice(this, api);
        scroll = new ScrollView(this);
        scroll.setBackgroundColor(Ui.BG);
        scroll.setFillViewport(true);
        col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setGravity(Gravity.CENTER_HORIZONTAL);
        // a round face: keep text inside the inscribed square (inset = (1 - 1/sqrt 2) / 2)
        int w = getResources().getDisplayMetrics().widthPixels;
        boolean round = getResources().getConfiguration().isScreenRound();
        int side = round ? Math.round(w * 0.146f) : Ui.dp(this, 8);
        col.setPadding(side, round ? side : Ui.dp(this, 8), side, round ? side * 2 : Ui.dp(this, 16));
        scroll.addView(col);
        setContentView(scroll);
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (home) render();
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
            render();
        }
    }

    // ------------------------------------------------------------------ building blocks

    private int clear(boolean isHome) {
        home = isHome;
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
        TextView b = Ui.text(this, label, 15, primary ? Ui.BG : Ui.INK);
        b.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        b.setGravity(Gravity.CENTER);
        b.setMinHeight(Ui.dp(this, 48)); // Wear's touch target
        b.setPadding(Ui.dp(this, 12), Ui.dp(this, 8), Ui.dp(this, 12), Ui.dp(this, 8));
        b.setBackground(Ui.round(this, primary ? Ui.ACCENT : Ui.RAISED, 24, primary ? 0 : Ui.LINE));
        b.setOnClickListener(l);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.topMargin = Ui.dp(this, 6);
        col.addView(b, lp);
        return b;
    }

    /** Off the main thread; what it shows goes through onUi. */
    private static void work(Runnable job) {
        new Thread(job, "aither-wear").start();
    }

    /** On the main thread, and only while the screen {@code at} is still the one shown. */
    private void onUi(int at, Runnable r) {
        runOnUiThread(() -> { if (at == screen) r.run(); });
    }

    // ------------------------------------------------------------------ screens

    private void render() {
        if (api.token().isEmpty()) signedOut(); else signedIn();
    }

    private void signedOut() {
        clear(true);
        TextView t = line("Aither", 20, Ui.INK);
        t.setTypeface(Typeface.create("sans-serif", Typeface.BOLD));
        line("Sign in with your phone to talk to Aither and answer your home's requests.", 13, Ui.DIM);
        button("Sign in", true, v -> signIn());
    }

    private void signIn() {
        int at = clear(false);
        TextView status = line("Getting a code…", 13, Ui.DIM);
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
                    TextView big = line(code, 18, Ui.INK);
                    big.setTypeface(Typeface.create("monospace", Typeface.BOLD));
                    line("or in Aither on your phone: Settings > Link a device", 11, Ui.DIM);
                } else {
                    status.setText("On your phone, open");
                    line(page, 13, Ui.ACCENT);
                    TextView big = line(code, 24, Ui.INK);
                    big.setTypeface(Typeface.create("monospace", Typeface.BOLD));
                    line("and enter this code", 13, Ui.DIM);
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
        button("Talk to Aither", true, v -> talk());
        TextView count = line("Checking what's waiting…", 13, Ui.DIM);
        LinearLayout cards = new LinearLayout(this);
        cards.setOrientation(LinearLayout.VERTICAL);
        col.addView(cards);
        TextView out = Ui.text(this, "Sign out", 13, Ui.FAINT);
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
                count.setText(WearRules.countLine(waiting.size()));
                for (ApprovalCard c : waiting) cards.addView(card(c));
            });
        });
    }

    private View card(ApprovalCard c) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(Ui.dp(this, 10), Ui.dp(this, 8), Ui.dp(this, 10), Ui.dp(this, 8));
        box.setBackground(Ui.round(this, Ui.CARD, Ui.RADIUS_SM, c.urgent ? Ui.ACCENT : Ui.LINE));
        TextView title = Ui.text(this, c.title.isEmpty() ? "Aither needs your OK" : c.title, 14, Ui.INK);
        title.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        box.addView(title);
        if (!c.body.isEmpty()) box.addView(Ui.text(this, ApprovalCard.clip(c.body, 140), 12, Ui.DIM));
        TextView status = Ui.text(this, c.statusLine(), 11, Ui.FAINT);
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

    private TextView chip(String label, boolean primary) {
        TextView b = Ui.text(this, label, 13, primary ? Ui.BG : Ui.INK);
        b.setGravity(Gravity.CENTER);
        b.setMinHeight(Ui.dp(this, 40));
        b.setBackground(Ui.round(this, primary ? Ui.ACCENT : Ui.RAISED, 20, primary ? 0 : Ui.LINE));
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
        TextView heard = line("", 14, Ui.INK);
        if (onDeviceListening()) {
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                heard.setText("Aither needs the microphone to listen.");
                requestPermissions(new String[] {Manifest.permission.RECORD_AUDIO}, ASK_MIC);
                typed(at);
                return;
            }
            listen(at, heard);
        } else {
            heard.setText("Ask Aither");
            typed(at);
        }
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
        input.setTextColor(Ui.INK);
        input.setHintTextColor(Ui.FAINT);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_CAP_SENTENCES);
        input.setImeOptions(EditorInfo.IME_ACTION_SEND);
        input.setOnEditorActionListener((v, id, ev) -> {
            String q = input.getText().toString().trim();
            if (!q.isEmpty() && at == screen) ask(q);
            return true;
        });
        col.addView(input, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));
        button("Ask", true, v -> {
            String q = input.getText().toString().trim();
            if (!q.isEmpty()) ask(q);
        });
    }

    private void listen(int at, TextView heard) {
        if (recognizer == null) recognizer = SpeechRecognizer.createOnDeviceSpeechRecognizer(this);
        heard.setText("Listening…");
        TextView stop = button("Type instead", false, v -> {
            recognizer.cancel();
            int now = clear(false);
            line("Ask Aither", 14, Ui.INK);
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
                ask(q);
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
            @Override public void onEndOfSpeech() { if (at == screen) heard.setText("…"); }
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

    private void ask(String q) {
        int at = clear(false);
        TextView you = line(q, 13, Ui.DIM);
        TextView answer = line("Thinking…", 15, Ui.INK);
        answer.setGravity(Gravity.START);
        work(() -> {
            String[] err = {null};
            String a = api.ask(q, err);
            onUi(at, () -> {
                if (a.isEmpty()) {
                    answer.setText(err[0] == null ? "No answer came back." : err[0]);
                    if (api.token().isEmpty()) { button("Sign in", true, v -> render()); return; }
                } else {
                    answer.setText(a);
                    voice.speak(a);
                }
                button("Ask again", true, v -> talk());
                TextView mute = button(voice.on() ? "Voice off" : "Voice on", false, null);
                mute.setOnClickListener(v -> {
                    voice.setOn(!voice.on());
                    mute.setText(voice.on() ? "Voice off" : "Voice on");
                });
                button("Done", false, v -> { voice.stop(); render(); });
            });
        });
    }
}
