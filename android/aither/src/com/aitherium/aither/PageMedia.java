package com.aitherium.aither;

import java.util.ArrayList;
import java.util.List;

/**
 * What a page's getUserMedia request may have. Pure Java so test/PageMediaCheck.java runs it
 * on a desktop JVM; MainActivity's onPermissionRequest asks it.
 *
 * Only an https aitherium.com page (Hear.pageMayHear) ever gets anything, and only what the
 * app itself holds: the mic with the mic permission (the page's own voice stack), the camera with
 * CAMERA (the live camera, which the page opens only after a tap and after Genesis agreed:
 * signed in, opted in, screen on, a child only with a guardian's allow). Anything else in
 * the request is refused, and a request that would get nothing is denied outright.
 */
final class PageMedia {
    static final String MIC = "android.webkit.resource.AUDIO_CAPTURE";
    static final String CAMERA = "android.webkit.resource.VIDEO_CAPTURE";

    private PageMedia() {}

    /** The resources to grant (empty: deny). */
    static String[] grants(String[] wanted, boolean originOk, boolean hasMic, boolean hasCamera) {
        List<String> out = new ArrayList<>();
        if (!originOk || wanted == null) return new String[0];
        for (String r : wanted) {
            if (MIC.equals(r) && hasMic) out.add(MIC);
            else if (CAMERA.equals(r) && hasCamera) out.add(CAMERA);
        }
        return out.toArray(new String[0]);
    }

    /** True when the page asked for the camera but the app has not been given it yet. */
    static boolean needsCameraPermission(String[] wanted, boolean originOk, boolean hasCamera) {
        if (!originOk || hasCamera || wanted == null) return false;
        for (String r : wanted) if (CAMERA.equals(r)) return true;
        return false;
    }
}
