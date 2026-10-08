package com.aitherium.aither;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import org.json.JSONObject;

import java.util.Locale;

/**
 * window.AitherVoice: push-to-talk for pages (a child talking to their Sprite). The WebView
 * has no Web Speech recognition, so the page asks the app, which listens with Android's
 * ON-DEVICE recognizer only (Talk.canListen: the default recognizer may upload the audio).
 * What was heard comes back to the page as a DOM event:
 *
 *   window.addEventListener('aither-voice', e => e.detail)  // {type, text}
 *   type: "listening" | "partial" | "final" | "error" | "end"
 *
 * Nothing is recorded or kept; the words go only to the page that asked. Only aitherium.com
 * pages load in this app (MainActivity), so only they can ask.
 */
final class PageVoice {
    static final int ASK_MIC = 41;

    private static PageVoice current;

    private final Activity act;
    private SpeechRecognizer recognizer;
    private WebView asker;
    private boolean pending; // listen() waiting for the microphone permission

    private PageVoice(Activity a) { act = a; }

    static synchronized PageVoice of(Activity a) {
        if (current == null || current.act != a) current = new PageVoice(a);
        return current;
    }

    boolean available() {
        return Talk.canListen(Build.VERSION.SDK_INT,
                Build.VERSION.SDK_INT >= 31 && SpeechRecognizer.isOnDeviceRecognitionAvailable(act));
    }

    void listen(WebView web) {
        asker = web;
        if (!available()) { send("error", Talk.errorText(12)); send("end", ""); return; }
        if (act.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            pending = true;
            act.requestPermissions(new String[] {Manifest.permission.RECORD_AUDIO}, ASK_MIC);
            return;
        }
        start();
    }

    /** MainActivity.onRequestPermissionsResult for ASK_MIC. */
    void onPermission(boolean granted) {
        if (!pending) return;
        pending = false;
        if (granted) start();
        else { send("error", Talk.errorText(9)); send("end", ""); }
    }

    void stop() {
        if (recognizer != null) recognizer.stopListening(); // what was said so far still counts
    }

    void destroy() {
        if (recognizer != null) { recognizer.destroy(); recognizer = null; }
    }

    private void start() {
        if (recognizer == null) {
            recognizer = SpeechRecognizer.createOnDeviceSpeechRecognizer(act);
            recognizer.setRecognitionListener(new Heard());
        }
        Intent i = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
        i.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
        i.putExtra(RecognizerIntent.EXTRA_LANGUAGE, Locale.getDefault().toLanguageTag());
        i.putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true);
        i.putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true);
        recognizer.cancel();
        recognizer.startListening(i);
        android.util.Log.i("AitherVoice", "page listening (on-device recognizer)");
        send("listening", "");
    }

    private void send(String type, String text) {
        WebView w = asker;
        if (w == null) return;
        String detail;
        try {
            detail = new JSONObject().put("type", type).put("text", text == null ? "" : text).toString();
        } catch (org.json.JSONException e) {
            return;
        }
        String js = "window.dispatchEvent(new CustomEvent('aither-voice',{detail:" + detail + "}))";
        w.post(() -> w.evaluateJavascript(js, null));
    }

    private final class Heard implements RecognitionListener {
        @Override public void onResults(Bundle r) {
            String q = Talk.heard(r.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION));
            if (q.isEmpty()) send("error", Talk.errorText(SpeechRecognizer.ERROR_NO_MATCH));
            else send("final", q);
            send("end", "");
        }
        @Override public void onPartialResults(Bundle r) {
            String q = Talk.heard(r.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION));
            if (!q.isEmpty()) send("partial", q);
        }
        @Override public void onError(int error) {
            if (error == Talk.ERROR_LANGUAGE_UNAVAILABLE && Build.VERSION.SDK_INT >= 33 && recognizer != null) {
                Intent i = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
                i.putExtra(RecognizerIntent.EXTRA_LANGUAGE, Locale.getDefault().toLanguageTag());
                recognizer.triggerModelDownload(i); // fetches the speech pack; sends nothing
            }
            send("error", Talk.errorText(error));
            send("end", "");
        }
        @Override public void onReadyForSpeech(Bundle b) {}
        @Override public void onBeginningOfSpeech() {}
        @Override public void onRmsChanged(float v) {}
        @Override public void onBufferReceived(byte[] b) {}
        @Override public void onEndOfSpeech() {}
        @Override public void onEvent(int t, Bundle b) {}
    }

    /** The page's handle; one per WebView so the answer goes back to the page that asked. */
    static final class Bridge {
        private final Activity act;
        private final WebView web;

        Bridge(Activity a, WebView w) { act = a; web = w; }

        /** True when this phone can listen on the device (Android 12+ with the on-device engine). */
        @JavascriptInterface
        public boolean canListen() {
            return PageVoice.of(act).available();
        }

        /** Start listening; results arrive as 'aither-voice' events. Asks for the mic once. */
        @JavascriptInterface
        public void listen() {
            act.runOnUiThread(() -> PageVoice.of(act).listen(web));
        }

        /** Stop listening; what was heard so far is still delivered as "final". */
        @JavascriptInterface
        public void stop() {
            act.runOnUiThread(() -> PageVoice.of(act).stop());
        }
    }
}
