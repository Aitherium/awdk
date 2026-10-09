package com.aitherium.aither;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.speech.tts.TextToSpeech;
import android.speech.tts.Voice;
import android.view.Gravity;
import android.view.View;
import android.view.inputmethod.EditorInfo;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import java.util.Locale;
import java.util.Set;

/**
 * Aither as the phone's assistant: what long-press power (or the assistant gesture) opens
 * once the owner makes Aither the assistant in Android's own role dialog. Ask in words, typed
 * or spoken; an on-device agent answers with the model on this phone and the tools the owner
 * allowed, and reads the answer aloud. Voice never leaves the phone (Talk says how).
 */
public class AssistActivity extends Activity {
    private static final int ASK_MIC = 3;
    /**
     * A question handed over by AitherFunctionService (Gemini's askAither), asked on open.
     * Held in this process, not in an intent extra: this activity is exported (the ASSIST
     * role), and another app must not be able to start it asking something.
     */
    private static String handed = "";
    private static long handedAt;

    static synchronized void hand(String question) {
        handed = question;
        handedAt = System.currentTimeMillis();
        handedDraft = false;
    }

    /** Text shared from another app (ShareActivity): shown in the box, never asked on its own. */
    static synchronized void handDraft(String text) {
        handed = text;
        handedAt = System.currentTimeMillis();
        handedDraft = true;
    }

    private static boolean handedDraft;

    private static synchronized String takeHanded() {
        String q = System.currentTimeMillis() - handedAt < 30_000L ? handed : "";
        handed = "";
        return q;
    }
    private static final String HINT = "What's on my calendar this week?";
    private final Handler main = new Handler(Looper.getMainLooper());
    private TextView log;
    private EditText input;
    private Button send;
    private Button mic;
    private CheckBox speak;
    private volatile boolean busy;
    private boolean listening;
    /** On screen: an answer that lands after the person left is shown, not spoken. */
    private boolean resumed;
    private String lastAnswer = "";
    /** The Aither page under the assistant when it opened, if any: its WebMCP tools. */
    private PageTools page;
    private Config cfg;
    /** Made on the first Talk tap (after the microphone is granted), on-device only. */
    private SpeechRecognizer recognizer;
    private TextToSpeech tts;
    /** Set once TextToSpeech is up with a voice that needs no network. */
    private boolean ttsReady;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        page = PageTools.onScreen(this); // now: the app's frame is paused under us, not yet stopped
        cfg = new Config(this);
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(pad, pad * 2, pad, pad);
        TextView title = new TextView(this);
        title.setText("Ask Aither");
        title.setTextSize(22);
        col.addView(title);
        log = new TextView(this);
        log.setTextSize(16);
        log.setPadding(0, pad, 0, pad);
        ScrollView scroll = new ScrollView(this);
        scroll.addView(log);
        col.addView(scroll, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));
        LinearLayout row = new LinearLayout(this);
        row.setGravity(Gravity.CENTER_VERTICAL);
        input = new EditText(this);
        input.setHint(HINT);
        input.setImeOptions(EditorInfo.IME_ACTION_SEND);
        input.setSingleLine(true);
        input.setOnEditorActionListener((v, id, e) -> {
            ask();
            return true;
        });
        row.addView(input, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        // push to talk: tap, speak, and it asks when you stop (tap again to stop early)
        mic = new Button(this);
        mic.setText("Talk");
        mic.setContentDescription("Talk to Aither");
        mic.setOnClickListener(v -> talk());
        if (!onDeviceListening()) mic.setVisibility(View.GONE); // no on-device recognizer: type
        row.addView(mic);
        send = new Button(this);
        send.setText("Ask");
        send.setOnClickListener(v -> ask());
        row.addView(send);
        col.addView(row);
        speak = new CheckBox(this);
        speak.setText("Speak answers");
        speak.setChecked(cfg.assistSpeak());
        speak.setOnCheckedChangeListener((v, on) -> {
            cfg.set(Talk.PREF_SPEAK, on);
            if (!on && tts != null) tts.stop();
        });
        col.addView(speak);
        Button picture = new Button(this); // "what's in this picture?" (DescribeActivity)
        picture.setText("Ask about a picture");
        picture.setOnClickListener(v -> startActivity(new Intent(this, DescribeActivity.class)));
        col.addView(picture);
        Button report = new Button(this); // flag an answer (Google Play's generative-AI policy)
        report.setText("Report an answer");
        report.setOnClickListener(v -> Report.open(this, lastAnswer, "Ask Aither"));
        col.addView(report);
        setContentView(col);
        Edge.fit(col);
        String blocked = cfg.localAiBlocked();
        if (!blocked.isEmpty()) {
            log.setText("The assistant is off: " + blocked + ".");
            input.setEnabled(false);
            send.setEnabled(false);
            mic.setVisibility(View.GONE);
            speak.setVisibility(View.GONE);
            return;
        }
        if (cfg.llmEnabled()) {
            startForegroundService(new Intent(this, LlmService.class));
        } else {
            log.setText("Turn on \"Run Bonsai 1.7B here\" in Aither on this phone first.");
        }
        tts = new TextToSpeech(this, status -> main.post(() ->
                ttsReady = status == TextToSpeech.SUCCESS && localVoice()));
        offerNano(col);
        askHanded();
    }

    @Override
    protected void onNewIntent(Intent i) { // singleTask: already open when Gemini hands one over
        super.onNewIntent(i);
        setIntent(i);
        if (input != null && input.isEnabled()) askHanded();
    }

    private void askHanded() {
        boolean draft;
        synchronized (AssistActivity.class) { draft = handedDraft; }
        String asked = takeHanded();
        if (asked.isEmpty() || busy) return;
        input.setText(asked);
        if (draft) { // shared from another app: the person reads it and taps Send
            input.setSelection(input.getText().length());
            input.requestFocus();
            return;
        }
        main.post(this::ask); // after onResume, so the turn counts as on screen
    }

    /**
     * Gemini Nano, when this build carries it (GeminiNano): the owner's switch once the phone
     * has it, or a one-time offer to download it. AICore fetches the model from Google; the
     * owner's questions are never part of that.
     */
    private void offerNano(LinearLayout col) {
        // Opt-in only: the owner turns it on in Settings (with the ML Kit disclosure) first.
        if (GeminiNano.engine(this) == null || !cfg.nanoPreferred() || !cfg.localAiBlocked().isEmpty()) return;
        CheckBox use = new CheckBox(this);
        use.setText("Use Gemini Nano for short questions");
        use.setChecked(cfg.nanoPreferred());
        use.setOnCheckedChangeListener((v, on) -> cfg.set("nano_preferred", on));
        use.setVisibility(View.GONE);
        col.addView(use);
        LinearLayout offer = new LinearLayout(this);
        offer.setVisibility(View.GONE);
        Button get = new Button(this);
        get.setText("Get Gemini Nano");
        Button no = new Button(this);
        no.setText("Not now");
        offer.addView(get);
        offer.addView(no);
        col.addView(offer);
        no.setOnClickListener(v -> {
            cfg.set("nano_declined", true);
            offer.setVisibility(View.GONE);
        });
        get.setOnClickListener(v -> {
            get.setEnabled(false);
            note("Downloading Gemini Nano through Android (AICore). Nothing you ask is sent.");
            GeminiNano.engine(this).download(new GeminiNano.Progress() {
                @Override public void bytes(long done) {}
                @Override public void done() {
                    GeminiNano.forgetStatus();
                    main.post(() -> {
                        offer.setVisibility(View.GONE);
                        use.setVisibility(View.VISIBLE);
                        note("Gemini Nano is ready on this phone.");
                    });
                }
                @Override public void failed(int code) {
                    main.post(() -> {
                        get.setEnabled(true);
                        note("The Gemini Nano download did not finish (code " + code + ").");
                    });
                }
            });
        });
        new Thread(() -> {
            int s = GeminiNano.status(this);
            main.post(() -> {
                if (isDestroyed()) return;
                use.setVisibility(s == NanoRoute.UNAVAILABLE ? View.GONE : View.VISIBLE);
                boolean ask = NanoRoute.offerDownload(cfg.localAiBlocked(), s, cfg.nanoDeclined());
                offer.setVisibility(ask ? View.VISIBLE : View.GONE);
            });
        }, "aither-nano-status").start();
    }

    private boolean onDeviceListening() {
        return Talk.canListen(Build.VERSION.SDK_INT,
                Build.VERSION.SDK_INT >= 31 && SpeechRecognizer.isOnDeviceRecognitionAvailable(this));
    }

    /** Keep the answer on the phone: a voice that needs no network, or no speaking at all. */
    private boolean localVoice() {
        if (tts == null) return false;
        Voice now = tts.getVoice();
        if (now != null && !now.isNetworkConnectionRequired()) return true;
        Locale want = now != null ? now.getLocale() : Locale.getDefault();
        Set<Voice> all = tts.getVoices();
        if (all == null) return false;
        Voice pick = null;
        for (Voice v : all) {
            if (v.isNetworkConnectionRequired()
                    || v.getFeatures().contains(TextToSpeech.Engine.KEY_FEATURE_NOT_INSTALLED)
                    || !v.getLocale().getLanguage().equals(want.getLanguage())) continue;
            if (pick == null || v.getLocale().equals(want)) pick = v;
        }
        return pick != null && tts.setVoice(pick) == TextToSpeech.SUCCESS;
    }

    private void say(String answer) {
        if (tts == null || !ttsReady || !resumed || !speak.isChecked()) return;
        String s = Talk.spoken(answer);
        if (!s.isEmpty()) tts.speak(s, TextToSpeech.QUEUE_FLUSH, null, "aither-answer");
    }

    /** The Talk button. The microphone is asked for here, on the first tap, never before. */
    private void talk() {
        if (tts != null) tts.stop();
        if (!cfg.localAiBlocked().isEmpty()) { // became a child's phone while this stayed open
            mic.setVisibility(View.GONE);
            speak.setVisibility(View.GONE);
            return;
        }
        if (listening) {
            if (recognizer != null) recognizer.stopListening(); // what was said so far still asks
            return;
        }
        if (busy) return;
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] {Manifest.permission.RECORD_AUDIO}, ASK_MIC);
            return;
        }
        listen();
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        super.onRequestPermissionsResult(code, perms, results);
        if (code != ASK_MIC) return;
        if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
            listen();
        } else {
            note("Without the microphone you can still type your question.");
        }
    }

    private Intent speechIntent() {
        Intent i = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
        i.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
        i.putExtra(RecognizerIntent.EXTRA_LANGUAGE, Locale.getDefault().toLanguageTag());
        i.putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true); // the recognizer is on-device anyway
        i.putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true);
        return i;
    }

    private void listen() {
        if (!onDeviceListening()) { // never the default recognizer: it may upload the audio
            mic.setVisibility(View.GONE);
            return;
        }
        if (recognizer == null) {
            recognizer = SpeechRecognizer.createOnDeviceSpeechRecognizer(this);
            recognizer.setRecognitionListener(new Heard());
        }
        listening = true;
        mic.setText("Stop");
        input.setText("");
        input.setHint("Listening…");
        recognizer.startListening(speechIntent());
    }

    private void doneListening() {
        listening = false;
        mic.setText("Talk");
        input.setHint(HINT);
    }

    private void note(String s) {
        log.append((log.length() > 0 ? "\n\n" : "") + s);
    }

    /** The recognizer's callbacks (main thread): words as they come, then ask with the last. */
    private final class Heard implements RecognitionListener {
        @Override public void onResults(Bundle r) {
            doneListening();
            String q = Talk.heard(r.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION));
            if (q.isEmpty()) {
                note(Talk.errorText(SpeechRecognizer.ERROR_NO_MATCH));
                return;
            }
            input.setText(q);
            ask();
        }
        @Override public void onPartialResults(Bundle r) {
            String q = Talk.heard(r.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION));
            if (!q.isEmpty()) input.setText(q);
        }
        @Override public void onError(int error) {
            doneListening();
            if (error == Talk.ERROR_LANGUAGE_UNAVAILABLE && Build.VERSION.SDK_INT >= 33 && recognizer != null) {
                recognizer.triggerModelDownload(speechIntent()); // fetches the speech pack; sends nothing
            }
            note(Talk.errorText(error));
        }
        @Override public void onReadyForSpeech(Bundle p) {}
        @Override public void onBeginningOfSpeech() {}
        @Override public void onRmsChanged(float db) {}
        @Override public void onBufferReceived(byte[] buf) {}
        @Override public void onEndOfSpeech() {}
        @Override public void onEvent(int type, Bundle p) {}
    }

    private void ask() {
        String q = input.getText().toString().trim();
        if (q.isEmpty() || busy) return;
        if (listening) { // typed and sent mid-listen: the typed question wins, the mic stops
            if (recognizer != null) recognizer.cancel();
            doneListening();
        }
        busy = true;
        send.setEnabled(false);
        mic.setEnabled(false);
        input.setText("");
        log.append((log.length() > 0 ? "\n\n" : "") + "You: " + q + "\n");
        // the answer line starts here: Nano's stream rewrites it, the final answer replaces it
        CharSequence before = log.getText().toString();
        log.append("…");
        StringBuilder streamed = new StringBuilder();
        boolean foreground = resumed;
        new Thread(() -> {
            Agent.Turn t = new Agent(this, false, page).ask(q, foreground, chunk -> main.post(() -> {
                streamed.append(chunk);
                if (busy) log.setText(before + "Aither: " + streamed + " …");
            }));
            StringBuilder names = new StringBuilder();
            for (int i = 0; i < t.toolsCalled.length(); i++) {
                names.append(i > 0 ? ", " : "").append(t.toolsCalled.optString(i));
            }
            String used = names.length() > 0 ? "  [used " + names + "]" : "";
            String by = "gemini-nano".equals(t.engine) ? "  [Gemini Nano, on this phone]" : "";
            String text = t.error.isEmpty() ? t.answer : "Sorry, that did not work (" + t.error + ").";
            main.post(() -> {
                lastAnswer = "Q: " + q + "\nA: " + text;
                log.setText(before + "Aither: " + text + used + by);
                busy = false;
                send.setEnabled(true);
                mic.setEnabled(true);
                if (!isDestroyed()) say(text);
            });
        }, "aither-assist").start();
    }

    @Override
    protected void onResume() {
        super.onResume();
        resumed = true;
    }

    @Override
    protected void onPause() {
        super.onPause();
        resumed = false;
        // out of view: stop listening (nothing half-heard is asked) and stop talking
        if (listening && recognizer != null) {
            recognizer.cancel();
            doneListening();
        }
        if (tts != null) tts.stop();
    }

    @Override
    protected void onDestroy() {
        if (recognizer != null) recognizer.destroy();
        if (tts != null) tts.shutdown();
        super.onDestroy();
    }
}
