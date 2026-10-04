package com.aitherium.aither;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.inputmethod.EditorInfo;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/**
 * Aither as the phone's assistant: what long-press power (or the assistant gesture) opens
 * once the owner makes Aither the assistant in Android's own role dialog. Ask in words; an
 * on-device agent answers with the model on this phone and the tools the owner allowed.
 */
public class AssistActivity extends Activity {
    private final Handler main = new Handler(Looper.getMainLooper());
    private TextView log;
    private EditText input;
    private Button send;
    private volatile boolean busy;
    private String lastAnswer = "";

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
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
        input.setHint("What's on my calendar this week?");
        input.setImeOptions(EditorInfo.IME_ACTION_SEND);
        input.setSingleLine(true);
        input.setOnEditorActionListener((v, id, e) -> {
            ask();
            return true;
        });
        row.addView(input, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        send = new Button(this);
        send.setText("Ask");
        send.setOnClickListener(v -> ask());
        row.addView(send);
        col.addView(row);
        Button report = new Button(this); // flag an answer (Google Play's generative-AI policy)
        report.setText("Report an answer");
        report.setOnClickListener(v -> Report.open(this, lastAnswer, "Ask Aither"));
        col.addView(report);
        setContentView(col);
        Edge.fit(col);
        Config cfg = new Config(this);
        String blocked = cfg.localAiBlocked();
        if (!blocked.isEmpty()) {
            log.setText("The assistant is off: " + blocked + ".");
            input.setEnabled(false);
            send.setEnabled(false);
        } else if (cfg.llmEnabled()) {
            startForegroundService(new Intent(this, LlmService.class));
        } else {
            log.setText("Turn on \"Run Bonsai 1.7B here\" in Aither on this phone first.");
        }
    }

    private void ask() {
        String q = input.getText().toString().trim();
        if (q.isEmpty() || busy) return;
        busy = true;
        send.setEnabled(false);
        input.setText("");
        log.append((log.length() > 0 ? "\n\n" : "") + "You: " + q + "\n…");
        new Thread(() -> {
            Agent.Turn t = new Agent(this, false).ask(q);
            StringBuilder names = new StringBuilder();
            for (int i = 0; i < t.toolsCalled.length(); i++) {
                names.append(i > 0 ? ", " : "").append(t.toolsCalled.optString(i));
            }
            String used = names.length() > 0 ? "  [used " + names + "]" : "";
            String text = t.error.isEmpty() ? t.answer : "Sorry, that did not work (" + t.error + ").";
            main.post(() -> {
                lastAnswer = "Q: " + q + "\nA: " + text;
                CharSequence s = log.getText();
                log.setText(s.subSequence(0, s.length() - 1));
                log.append("Aither: " + text + used);
                busy = false;
                send.setEnabled(true);
            });
        }, "aither-assist").start();
    }
}
