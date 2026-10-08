package com.aitherium.aither;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;

/**
 * "Ask Aither" from another app: the share sheet (ACTION_SEND text/plain, e.g. a Gemini answer),
 * the text-selection menu (ACTION_PROCESS_TEXT) and aither://ask?q=. The text lands in Ask
 * Aither's box as a draft; the person reads it and taps Ask. It is never asked on its own: this
 * activity is exported, and another app must not be able to make Aither answer something unseen.
 */
public class ShareActivity extends Activity {
    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        String text = draftFrom(getIntent());
        if (!text.isEmpty()) {
            AssistActivity.handDraft(text);
            startActivity(new Intent(this, AssistActivity.class)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP));
        }
        finish();
    }

    /** The shared, selected or linked text as one draft; "" when there is none. */
    static String draftFrom(Intent i) {
        if (i == null) return "";
        String action = i.getAction();
        CharSequence body = null, subject = null;
        if (Intent.ACTION_SEND.equals(action)) {
            body = i.getCharSequenceExtra(Intent.EXTRA_TEXT);
            subject = i.getCharSequenceExtra(Intent.EXTRA_SUBJECT);
        } else if (Intent.ACTION_PROCESS_TEXT.equals(action)) {
            body = i.getCharSequenceExtra(Intent.EXTRA_PROCESS_TEXT);
        } else if (Intent.ACTION_VIEW.equals(action)) {
            Uri u = i.getData();
            if (u != null && "aither".equals(u.getScheme()) && "ask".equals(u.getHost())) {
                body = u.getQueryParameter("q");
            }
        }
        return ShareDraft.of(subject == null ? "" : subject.toString(), body == null ? "" : body.toString());
    }
}
