package com.aitherium.aither;

import android.app.appfunctions.AppFunctionException;
import android.app.appfunctions.AppFunctionManager;
import android.app.appfunctions.AppFunctionService;
import android.app.appfunctions.ExecuteAppFunctionRequest;
import android.app.appfunctions.ExecuteAppFunctionResponse;
import android.app.appsearch.GenericDocument;
import android.content.Context;
import android.content.Intent;
import android.content.pm.SigningInfo;
import android.net.Uri;
import android.os.Build;
import android.os.CancellationSignal;
import android.os.OutcomeReceiver;

/**
 * Aither's app functions (Android 16+): what Gemini, or another agent Android lets run app
 * functions (EXECUTE_APP_FUNCTIONS), can ask this app to do. The policy is GeminiFunctions;
 * this is the platform end, written on android.app.appfunctions directly (android.jar 36) so
 * the app needs no Jetpack or KSP. What the KSP processor would generate is hand-written
 * instead: assets/aither_functions.xml (the v2 index), assets/app_functions.xml (the v1 index
 * Android 16's first indexer reads) and app_functions_schema.xsd (copied from androidx,
 * Apache-2.0), all named from the manifest.
 *
 * Nothing here acts for the person: functions read a count or open a page of this app.
 */
public class AitherFunctionService extends AppFunctionService {
    @Override
    public void onExecuteFunction(ExecuteAppFunctionRequest req, String caller, SigningInfo signer,
                                  CancellationSignal cancel,
                                  OutcomeReceiver<ExecuteAppFunctionResponse, AppFunctionException> done) {
        Config cfg = new Config(this);
        boolean child = child(cfg);
        String id = req.getFunctionIdentifier();
        if (!GeminiFunctions.known(id)) {
            done.onError(new AppFunctionException(AppFunctionException.ERROR_FUNCTION_NOT_FOUND, id + " is not an Aither function"));
            return;
        }
        if (!GeminiFunctions.allowed(id, child)) {
            done.onError(new AppFunctionException(AppFunctionException.ERROR_DENIED, "not on a child's phone"));
            return;
        }
        GenericDocument p = req.getParameters();
        try {
            String answer;
            if (GeminiFunctions.ASK_AITHER.equals(id)) {
                String q = GeminiFunctions.clean(string(p, "prompt"), GeminiFunctions.MAX_QUESTION);
                if (q.isEmpty()) throw bad("prompt is empty");
                if (!cfg.localAiBlocked().isEmpty()) {
                    throw new AppFunctionException(AppFunctionException.ERROR_DENIED,
                            "Ask Aither is off on this phone: " + cfg.localAiBlocked());
                }
                AssistActivity.hand(q);
                open(new Intent(this, AssistActivity.class));
                answer = "Opened Ask Aither with your question; it answers on this phone.";
            } else if (GeminiFunctions.OPEN_APP.equals(id)) {
                String url = GeminiFunctions.appUrl(string(p, "app"), child);
                if (url == null) throw bad("app must be one of learn, family, hearth");
                open(view(url));
                answer = "Opened " + string(p, "app").trim() + " in Aither.";
            } else if (GeminiFunctions.PENDING_APPROVALS.equals(id)) {
                long[] n = Notices.openApprovals(this);
                answer = GeminiFunctions.approvalsLine((int) n[0], n[1], child);
            } else { // ASK_HEARTH
                String url = GeminiFunctions.hearthUrl(string(p, "request"), child);
                if (url == null) throw bad("request is empty");
                open(view(url));
                answer = "Opened Hearth with your request in the box. Send it there; anything that "
                        + "leaves your home waits for your approval.";
            }
            done.onResult(new ExecuteAppFunctionResponse(result(answer)));
        } catch (AppFunctionException e) {
            done.onError(e);
        } catch (RuntimeException e) {
            done.onError(new AppFunctionException(AppFunctionException.ERROR_APP_UNKNOWN_ERROR,
                    e.getClass().getSimpleName()));
        }
    }

    static boolean child(Config cfg) {
        return cfg.childDevice() || "child".equals(cfg.profileKind());
    }

    private static AppFunctionException bad(String why) {
        return new AppFunctionException(AppFunctionException.ERROR_INVALID_ARGUMENT, why);
    }

    private static String string(GenericDocument p, String name) {
        if (p == null) return "";
        String s = p.getPropertyString(name);
        return s == null ? "" : s;
    }

    @SuppressWarnings({"rawtypes", "unchecked"})
    private static GenericDocument result(String text) {
        return new GenericDocument.Builder("", "", "")
                .setPropertyString(ExecuteAppFunctionResponse.PROPERTY_RETURN_VALUE, text).build();
    }

    private Intent view(String url) {
        return new Intent(Intent.ACTION_VIEW, Uri.parse(url)).setClass(this, MainActivity.class);
    }

    private void open(Intent i) {
        startActivity(i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
    }

    /** Switch off in Android's index what this phone's role may not run (a child: askAither
     *  and askHearth), and back on for a grown-up. Called with Shortcuts.apply. */
    static void sync(Context c, boolean child) {
        if (Build.VERSION.SDK_INT < 36) return;
        AppFunctionManager m = c.getSystemService(AppFunctionManager.class);
        if (m == null) return;
        for (String id : GeminiFunctions.ALL) {
            int state = GeminiFunctions.allowed(id, child) ? AppFunctionManager.APP_FUNCTION_STATE_DEFAULT
                    : AppFunctionManager.APP_FUNCTION_STATE_DISABLED;
            try {
                m.setAppFunctionEnabled(id, state, c.getMainExecutor(), new OutcomeReceiver<Void, Exception>() {
                    @Override public void onResult(Void v) {}
                    @Override public void onError(Exception e) {} // not indexed yet: next launch
                });
            } catch (RuntimeException e) { /* no app functions on this build of Android */ }
        }
    }
}
