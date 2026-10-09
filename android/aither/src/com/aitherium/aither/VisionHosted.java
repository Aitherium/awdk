package com.aitherium.aither;

import android.webkit.CookieManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * A picture asked about online: the same chat the app's Aither tab uses (POST
 * /api/agent-chat on app.aitherium.com, with this app's signed-in session cookie), the picture
 * as a data:image attachment. AitherOS routes every turn that carries an image to its vision
 * model whatever the words say, so a picture sent with no question is still looked at.
 * Only on the owner's tap (DescribeActivity says "Ask online" before anything is sent).
 */
final class VisionHosted {
    static final String CHAT = AppTabs.ORIGIN + "/api/agent-chat";

    private VisionHosted() {}

    /** Blocking. The answer text, or throws with a reason the person can read. */
    static String describe(byte[] jpeg, String question) throws Exception {
        String cookies = CookieManager.getInstance().getCookie(AppTabs.ORIGIN);
        if (!Session.hasSession(cookies)) throw new IllegalStateException("sign in to Aither first");
        JSONObject body = new JSONObject()
                .put("message", Vision.question(question))
                .put("agent", "aither")
                .put("attachments", new JSONArray().put("data:image/jpeg;base64,"
                        + android.util.Base64.encodeToString(jpeg, android.util.Base64.NO_WRAP)));
        HttpURLConnection c = (HttpURLConnection) new URL(CHAT).openConnection();
        c.setConnectTimeout(15000);
        c.setReadTimeout(180000);
        c.setDoOutput(true);
        c.setRequestProperty("Content-Type", "application/json");
        c.setRequestProperty("Accept", "text/event-stream");
        c.setRequestProperty("Origin", AppTabs.ORIGIN);
        c.setRequestProperty("Cookie", cookies);
        try (OutputStream o = c.getOutputStream()) {
            o.write(body.toString().getBytes(StandardCharsets.UTF_8));
        }
        int code = c.getResponseCode();
        if (code == 401 || code == 403) {
            Session.note(Session.State.OUT);
            throw new IllegalStateException("sign in to Aither again");
        }
        if (code != 200) throw new IllegalStateException("Aither online answered " + code);
        String text;
        try (InputStream in = c.getInputStream()) {
            text = new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
        return answer(text);
    }

    /** The last "answer" event's text, else the "complete" content; an "error" event throws. */
    static String answer(String sse) {
        String answer = "", complete = "";
        for (String[] ev : Vision.sse(sse)) {
            JSONObject d;
            try { d = new JSONObject(ev[1]); } catch (Exception e) { continue; }
            String type = d.optString("type", ev[0]);
            if ("answer".equals(type) || "final_answer".equals(type)) {
                String a = d.optString("answer", d.optString("content", ""));
                if (!a.isEmpty()) answer = a;
            } else if ("complete".equals(type)) {
                complete = d.optString("content", complete);
            } else if ("error".equals(type)) {
                throw new IllegalStateException(d.optString("error", "Aither online could not answer"));
            }
        }
        String out = !answer.isEmpty() ? answer : complete;
        if (out.trim().isEmpty()) throw new IllegalStateException("Aither online sent no answer");
        return out.trim();
    }
}
