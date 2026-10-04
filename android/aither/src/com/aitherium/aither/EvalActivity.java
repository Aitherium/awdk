package com.aitherium.aither;

import android.app.Activity;
import android.os.Bundle;
import android.os.PowerManager;
import android.util.Log;

import org.json.JSONObject;

/**
 * Measures the on-device agent: does the model on this phone call the calendar tool when a
 * question needs it, and only then? Launchable only by adb (the activity requires DUMP, which
 * apps cannot hold). Tools run DRY (fixed sample data), so nothing of the owner's is read, and
 * only verdicts and timings are logged (tag AitherEval), never answers.
 */
public class EvalActivity extends Activity {
    static final String TAG = "AitherEval";
    static final String[][] CASES = {
            {"What's on my calendar this week?", "tool"},
            {"Do I have any meetings tomorrow?", "tool"},
            {"When is my dentist appointment?", "tool"},
            {"Am I free on Wednesday afternoon?", "tool"},
            {"What's my schedule for the next few days?", "tool"},
            {"Is there anything on my calendar on Tuesday?", "tool"},
            {"When is the team sync?", "tool"},
            {"How busy am I this week?", "tool"},
            {"What is my next event?", "tool"},
            {"Do I have plans this weekend?", "tool"},
            {"What is the capital of France?", "none"},
            {"Write a haiku about rain.", "none"},
            {"What is 17 times 23?", "none"},
            {"Explain what a rainbow is in one sentence.", "none"},
            {"Translate 'good morning' into Spanish.", "none"},
            {"Give me three names for a cat.", "none"},
            {"What does CPU stand for?", "none"},
            {"How many days are in a leap year?", "none"},
            {"Suggest a quick breakfast.", "none"},
            {"What is the boiling point of water in Celsius?", "none"},
    };

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        PowerManager pm = getSystemService(PowerManager.class);
        // a locked phone parks the CPU; the measurement must not depend on the screen
        PowerManager.WakeLock wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "aither:eval");
        wake.acquire(30 * 60_000L);
        new Thread(() -> {
            try {
                measure(pm);
            } finally {
                if (wake.isHeld()) wake.release();
            }
            runOnUiThread(this::finish);
        }, "aither-eval").start();
    }

    private void measure(PowerManager pm) {
        {
            Agent agent = new Agent(this, true);
            int right = 0;
            long total = 0;
            for (int i = 0; i < CASES.length; i++) {
                Agent.Turn t = agent.ask(CASES[i][0]);
                boolean called = t.toolsCalled.length() > 0;
                boolean ok = t.error.isEmpty() && called == "tool".equals(CASES[i][1]);
                if (ok) right++;
                total += t.ms;
                try {
                    Log.i(TAG, new JSONObject().put("case", i).put("want", CASES[i][1])
                            .put("called", called).put("ok", ok).put("ms", t.ms)
                            .put("answer_chars", t.answer.length())
                            .put("error", t.error.isEmpty() ? null : t.error.substring(0, Math.min(120, t.error.length())))
                            .put("thermal", pm.getCurrentThermalStatus()).toString());
                } catch (Exception e) { /* logging only */ }
                if (pm.getCurrentThermalStatus() >= PowerManager.THERMAL_STATUS_MODERATE) {
                    Log.i(TAG, "stopped: the phone is warm");
                    break;
                }
            }
            Log.i(TAG, "DONE right=" + right + "/" + CASES.length + " mean_ms=" + (total / CASES.length));
        }
    }
}
