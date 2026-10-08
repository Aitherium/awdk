package com.aitherium.aither;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

import org.json.JSONObject;

/**
 * Approve / Deny on an approval notification (Notices.show). Not exported: only this app's
 * own immutable PendingIntents reach it. It sends the notice id + digest the card was posted
 * with, as the signed-in account (the WebView's session cookie), and replaces the card with
 * the server's answer: "Approved by you · 1 of 2", "Denied by you", or why it was refused.
 */
public class NoticeActionReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context ctx, Intent i) {
        if (!Notices.ACTION_DECIDE.equals(i.getAction())) return;
        String id = i.getStringExtra(Notices.EXTRA_ID);
        String digest = i.getStringExtra(Notices.EXTRA_DIGEST);
        boolean allow = i.getBooleanExtra(Notices.EXTRA_ALLOW, false);
        String title = i.getStringExtra(Notices.EXTRA_TITLE);
        if (ApprovalCard.decideBody(id, digest, allow) == null) return; // never a malformed answer
        ApprovalCard sent = new ApprovalCard(id, "approval", title, "", true, false, digest,
                "pending", 0, 1, allow ? "yes" : "no");
        Notices.show(ctx, sent, "Sending…");
        PendingResult async = goAsync();
        new Thread(() -> {
            try {
                Object[] got = Notices.decide(ctx, id, digest, allow);
                int code = (Integer) got[0];
                JSONObject j = (JSONObject) got[1];
                if (code == 200 && j != null) {
                    ApprovalCard after = Notices.parse(j);
                    if (after != null && after.matches(id, digest)) {
                        String line = after.statusLine();
                        String err = j.optString("error", "");
                        if (!err.isEmpty() && allow) line += " (" + ApprovalCard.clip(err, 80) + ")";
                        Notices.show(ctx, after, line);
                        return;
                    }
                }
                String detail = j == null ? "" : j.optString("detail", "");
                Notices.show(ctx, sent, ApprovalCard.refusal(code, detail));
            } finally {
                async.finish();
            }
        }, "aither-decide").start();
    }
}
