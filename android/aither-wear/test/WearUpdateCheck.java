package com.aitherium.aither;

import java.util.Arrays;

/** Exit 0 when the watch picks the right release, asset and checksum (WearUpdate). */
public class WearUpdateCheck {
    static int bad = 0;

    static void eq(String what, Object got, Object want) {
        if (!String.valueOf(got).equals(String.valueOf(want))) {
            System.out.println("FAIL " + what + ": got " + got + ", want " + want);
            bad++;
        }
    }

    public static void main(String[] a) {
        // versions compare as numbers, never as text
        eq("0.3.26 > 0.3.25", WearUpdate.newer("0.3.26", "0.3.25"), true);
        eq("0.3.10 > 0.3.9", WearUpdate.newer("0.3.10", "0.3.9"), true);
        eq("equal is not newer", WearUpdate.newer("0.3.26", "0.3.26"), false);
        eq("junk never wins", WearUpdate.newer("0.3.x", "0.3.1"), false);

        // only published aither-android-vX.Y.Z tags count
        eq("tag", WearUpdate.tagVersion("aither-android-v0.3.26", false, false), "0.3.26");
        eq("draft", WearUpdate.tagVersion("aither-android-v0.4.0", true, false), "");
        eq("prerelease", WearUpdate.tagVersion("aither-android-v0.4.0", false, true), "");
        eq("other product", WearUpdate.tagVersion("adk-v9.9.9", false, false), "");
        eq("odd tag", WearUpdate.tagVersion("aither-android-v0.3.26-rc1", false, false), "");

        // the watch takes ITS asset, never the phone's (same package name)
        eq("wear asset", WearUpdate.wearAsset("0.3.26"), "aither-wear-0.3.26.apk");
        eq("face asset", WearUpdate.faceAsset(Arrays.asList(
                "aither-0.3.26.apk", "aither-wear-0.3.26.apk", "aither-watchface-0.1.0.apk",
                "aither-watchface-0.2.0.apk", "aither-watchface-x.apk", "SHA256SUMS")),
                "aither-watchface-0.2.0.apk");
        eq("no face", WearUpdate.faceAsset(Arrays.asList("aither-0.3.26.apk")), "");

        // SHA256SUMS exactly as the 0.3.25 release publishes it (binary-mode '*')
        String sums = "0fe553a50b8e98b4ac90eb4168420d8da93957ca5a861ea615b63e9edc0a45f7 *aither-0.3.25.apk\n"
                + "b95940d7b3e442a4389fa54fc8594f9101aa49958b1ef8e3da8c68687c3455ac *aither-watchface-0.1.0.apk\n"
                + "FA02DFEC9F2CB9C7E41F4F0BE2B77DDB9AFD49037E4064B55E583007F92A46A2  aither-wear-0.3.25.apk\n";
        eq("wear sum", WearUpdate.sumFor(sums, "aither-wear-0.3.25.apk"),
                "fa02dfec9f2cb9c7e41f4f0be2b77ddb9afd49037e4064b55e583007f92a46a2");
        eq("phone sum is not the watch's", WearUpdate.sumFor(sums, "aither-wear-0.3.26.apk"), "");
        eq("prefix is not a match", WearUpdate.sumFor(sums, "aither-0.3.2"), "");
        eq("short hash refused", WearUpdate.sumFor("abc *aither-wear-1.0.0.apk", "aither-wear-1.0.0.apk"), "");

        // only GitHub's own downloads for this repo
        eq("trusted", WearUpdate.trustedUrl(
                "https://github.com/Aitherium/awdk/releases/download/aither-android-v0.3.26/aither-wear-0.3.26.apk"), true);
        eq("elsewhere", WearUpdate.trustedUrl("https://example.com/aither-wear-0.3.26.apk"), false);
        eq("other repo", WearUpdate.trustedUrl("https://github.com/someone/awdk/releases/download/x.apk"), false);

        // every 6 hours; a clock that went backwards checks now
        long h = 3600_000L;
        eq("not due", WearUpdate.due(10 * h, 15 * h), false);
        eq("due", WearUpdate.due(10 * h, 16 * h), true);
        eq("clock back", WearUpdate.due(10 * h, 9 * h), true);

        if (bad > 0) System.exit(1);
        System.out.println("WearUpdateCheck ok");
    }
}
