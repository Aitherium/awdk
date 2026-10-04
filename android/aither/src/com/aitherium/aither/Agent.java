package com.aitherium.aither;

import android.content.Context;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * An agent on the phone: the OpenAI tools loop (the shape adk's agents speak) against the
 * model on this phone, through the same door AitherOS uses (LocalProxy, 127.0.0.1:8486, the
 * per-install token). It sees only the tools the owner turned on and Android has granted;
 * a tool it was not offered is refused here even if the model names it.
 */
final class Agent {
    static final int MAX_STEPS = 3;
    // measured on the Fold (Bonsai 1.7B): this short prompt called the tool for 4-5 of 10
    // calendar questions; naming the cases it missed is what the small model needs
    static final String SYSTEM = "You are Aither, the owner's assistant on their phone. Answer briefly. "
            + "You cannot see the owner's calendar unless you call calendar_read. Call calendar_read "
            + "for ANY question about the owner's own time: appointments, meetings, events, plans, "
            + "schedule, availability (am I free / busy), when something of theirs happens, or what "
            + "is next. Never guess or answer those from memory. For general questions, answer directly.";

    /** What one turn did: the answer, which tools ran, and how long it took. */
    static final class Turn {
        String answer = "";
        String error = "";
        final JSONArray toolsCalled = new JSONArray();
        long ms;
    }

    private final Context ctx;
    private final Config cfg;
    private final boolean dryTools;

    /** dryTools: tools answer with fixed sample data and read nothing (measurement only). */
    Agent(Context c, boolean dryTools) {
        ctx = c.getApplicationContext();
        cfg = new Config(ctx);
        this.dryTools = dryTools;
    }

    JSONArray tools() throws Exception {
        JSONArray t = new JSONArray();
        if (dryTools || CalendarTool.allowed(ctx)) t.put(CalendarTool.spec());
        return t;
    }

    Turn ask(String question) {
        Turn turn = new Turn();
        long t0 = System.currentTimeMillis();
        try {
            JSONArray messages = new JSONArray()
                    .put(new JSONObject().put("role", "system").put("content", SYSTEM))
                    .put(new JSONObject().put("role", "user").put("content", question));
            JSONArray tools = tools();
            // Measured on the Fold: Bonsai 1.7B chose the calendar tool for 4-5 of 10 calendar
            // questions whatever the prompt said, and ignored tool_choice "required" (slower,
            // still no call). So the choice is made here, not by the model: a question that is
            // plainly about the owner's time runs calendar_read first and the model answers
            // from what it returned.
            if (tools.length() > 0 && CalendarTool.asksAboutTime(question)) {
                String id = "call_calendar_0";
                messages.put(new JSONObject().put("role", "assistant").put("content", "")
                        .put("tool_calls", new JSONArray().put(new JSONObject().put("id", id)
                                .put("type", "function").put("function", new JSONObject()
                                        .put("name", CalendarTool.NAME).put("arguments", "{\"days\":7}")))));
                turn.toolsCalled.put(CalendarTool.NAME);
                messages.put(new JSONObject().put("role", "tool").put("tool_call_id", id)
                        .put("content", run(CalendarTool.NAME, "{\"days\":7}")));
            }
            for (int step = 0; step < MAX_STEPS; step++) {
                JSONObject req = new JSONObject().put("model", LlmService.MODEL_ID)
                        .put("messages", messages).put("max_tokens", 256).put("temperature", 0.2);
                // the last step gets no tools, so the loop always ends in words
                if (tools.length() > 0 && step < MAX_STEPS - 1) req.put("tools", tools);
                JSONObject msg = chat(req).getJSONArray("choices").getJSONObject(0).getJSONObject("message");
                JSONArray calls = msg.optJSONArray("tool_calls");
                if (calls == null || calls.length() == 0) {
                    turn.answer = msg.optString("content", "").trim();
                    break;
                }
                messages.put(msg);
                for (int i = 0; i < calls.length(); i++) {
                    JSONObject call = calls.getJSONObject(i);
                    JSONObject fn = call.getJSONObject("function");
                    String name = fn.optString("name", "");
                    turn.toolsCalled.put(name);
                    messages.put(new JSONObject().put("role", "tool")
                            .put("tool_call_id", call.optString("id", name))
                            .put("content", run(name, fn.optString("arguments", "{}"))));
                }
            }
        } catch (Exception e) {
            turn.error = e.getClass().getSimpleName() + (e.getMessage() == null ? "" : ": " + e.getMessage());
        }
        turn.ms = System.currentTimeMillis() - t0;
        return turn;
    }

    private String run(String name, String argsJson) {
        try {
            JSONObject args = argsJson.isEmpty() ? new JSONObject() : new JSONObject(argsJson);
            if (CalendarTool.NAME.equals(name) && (dryTools || CalendarTool.allowed(ctx))) {
                return dryTools ? CalendarTool.sample() : CalendarTool.read(ctx, args.optInt("days", 7));
            }
            return new JSONObject().put("error", "tool " + name + " is not available").toString();
        } catch (Exception e) {
            return "{\"error\":\"the tool failed\"}";
        }
    }

    private JSONObject chat(JSONObject body) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL("http://127.0.0.1:" + LocalProxy.PORT
                + "/v1/chat/completions").openConnection();
        c.setRequestMethod("POST");
        c.setConnectTimeout(5000);
        c.setReadTimeout(120_000);
        c.setDoOutput(true);
        c.setRequestProperty("Content-Type", "application/json");
        c.setRequestProperty("Authorization", "Bearer " + cfg.llmToken());
        try (OutputStream o = c.getOutputStream()) {
            o.write(body.toString().getBytes(StandardCharsets.UTF_8));
        }
        int code = c.getResponseCode();
        InputStream in = code < 400 ? c.getInputStream() : c.getErrorStream();
        String text = in == null ? "" : NodeLink.read(in, 1 << 20);
        if (code != 200) throw new java.io.IOException("the model answered " + code + " " + text);
        return new JSONObject(text);
    }
}
