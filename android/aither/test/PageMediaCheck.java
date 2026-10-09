package com.aitherium.aither;

import java.util.Arrays;

/** Exit 0 when a page's mic/camera request gets exactly what the design says. */
public class PageMediaCheck {
    static int bad = 0;

    static void eq(String what, Object got, Object want) {
        if (!String.valueOf(got).equals(String.valueOf(want))) {
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
            bad++;
        }
    }

    static String g(String[] wanted, boolean origin, boolean mic, boolean cam) {
        return Arrays.toString(PageMedia.grants(wanted, origin, mic, cam));
    }

    public static void main(String[] a) {
        String[] both = {PageMedia.MIC, PageMedia.CAMERA};
        String[] cam = {PageMedia.CAMERA};
        String[] odd = {"android.webkit.resource.PROTECTED_MEDIA_ID", PageMedia.CAMERA};
        eq("our page, both held", g(both, true, true, true), "[" + PageMedia.MIC + ", " + PageMedia.CAMERA + "]");
        eq("our page, no camera permission", g(both, true, true, false), "[" + PageMedia.MIC + "]");
        eq("our page, camera only", g(cam, true, false, true), "[" + PageMedia.CAMERA + "]");
        eq("someone else's page gets nothing", g(both, false, true, true), "[]");
        eq("unknown resources refused", g(odd, true, true, true), "[" + PageMedia.CAMERA + "]");
        eq("null request", g(null, true, true, true), "[]");
        eq("asks for camera permission", PageMedia.needsCameraPermission(cam, true, false), true);
        eq("never for a foreign page", PageMedia.needsCameraPermission(cam, false, false), false);
        eq("not when held", PageMedia.needsCameraPermission(cam, true, true), false);
        eq("not for mic only", PageMedia.needsCameraPermission(new String[] {PageMedia.MIC}, true, false), false);
        if (bad > 0) System.exit(1);
        System.out.println("PageMediaCheck ok");
    }
}
