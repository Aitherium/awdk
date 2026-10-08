package com.aitherium.aither;

import java.util.List;
import java.util.regex.Pattern;

/**
 * Ask Aither by voice: the rules AssistActivity follows to listen and to read answers aloud
 * (pure Java so test/TalkCheck.java runs it on a desktop JVM).
 *
 * Speech stays on the phone. Listening uses only Android's on-device recognizer (Android 12+,
 * SpeechRecognizer.createOnDeviceSpeechRecognizer): the default recognizer may send audio to
 * its maker's servers even when asked to prefer offline, so a phone without the on-device
 * one gets no mic button and is asked to type. Answers are spoken by a TextToSpeech voice
 * that needs no network, so the answer text stays on the phone too.
 */
final class Talk {
    private Talk() {}

    /** The assistant's "Speak answers" switch (Config); on unless the owner turned it off. */
    static final String PREF_SPEAK = "assist_speak";
    /** Under TextToSpeech.getMaxSpeechInputLength() (4000), with room to spare. */
    static final int MAX_SPOKEN = 3900;

    /** The on-device recognizer exists from Android 12 (API 31); before that, typing only. */
    static boolean canListen(int sdk, boolean onDeviceAvailable) {
        return sdk >= 31 && onDeviceAvailable;
    }

    /** What was heard: the recognizer's first non-blank guess (they come best first), or "". */
    static String heard(List<String> guesses) {
        if (guesses == null) return "";
        for (String g : guesses) {
            if (g != null && !g.trim().isEmpty()) return g.trim().replaceAll("\\s+", " ");
        }
        return "";
    }

    private static final Pattern FENCE = Pattern.compile("```.*?(```|$)", Pattern.DOTALL);
    private static final Pattern MD_LINK = Pattern.compile("\\[([^\\]]*)\\]\\([^)]*\\)");
    private static final Pattern URL = Pattern.compile("\\bhttps?://[^\\s)]*[^\\s.,;:!?)]");
    // Placeholders for what this class itself says, so Speakable's bracket rule leaves them
    // (private-use marks: String.trim() would eat a control character at either end)
    private static final String CODE = "CODE", LINK = "LINK";

    /**
     * An answer as it should sound: code blocks and links are left on the screen, stage
     * directions, emoji and markdown marks are not read out (Speakable), and a long answer
     * stops at the last whole sentence that fits.
     */
    static String spoken(String answer) {
        if (answer == null) return "";
        String s = FENCE.matcher(answer).replaceAll(" " + CODE + " ");
        s = MD_LINK.matcher(s).replaceAll("$1");
        s = URL.matcher(s).replaceAll(LINK);
        s = Speakable.of(s);
        s = s.replace(CODE, "(the code is on screen)").replace(LINK, "a link");
        if (s.length() <= MAX_SPOKEN) return s;
        String head = s.substring(0, MAX_SPOKEN);
        int cut = Math.max(head.lastIndexOf(". "), Math.max(head.lastIndexOf("! "), head.lastIndexOf("? ")));
        if (cut > 0) return head.substring(0, cut + 1) + " The rest is on screen.";
        int sp = head.lastIndexOf(' ');
        return (sp > 0 ? head.substring(0, sp) : head) + "… the rest is on screen.";
    }

    /** SpeechRecognizer.ERROR_LANGUAGE_UNAVAILABLE: the on-device speech pack is missing. */
    static final int ERROR_LANGUAGE_UNAVAILABLE = 13;

    /** A recognizer error (SpeechRecognizer.ERROR_* codes) in words for the person. */
    /** What a person sees when nothing was heard (the same words as lib/native-voice.ts). */
    static final String NO_HEAR = "I didn't hear anything. Tap the mic and try again.";

    static String errorText(int code) {
        switch (code) {
            case 5: // ERROR_CLIENT: Google's on-device engine reports silence this way
            case 6: // ERROR_SPEECH_TIMEOUT
            case 7: // ERROR_NO_MATCH
                return NO_HEAR;
            case 3: // ERROR_AUDIO
                return "The microphone isn't working right now.";
            case 8: // ERROR_RECOGNIZER_BUSY
                return "Speech recognition is busy. Try again in a moment.";
            case 9: // ERROR_INSUFFICIENT_PERMISSIONS
                return "Aither needs microphone access to listen. Allow it in Android settings.";
            case 12: // ERROR_LANGUAGE_NOT_SUPPORTED
                return "This phone can't recognize your language on the device. Type instead.";
            case ERROR_LANGUAGE_UNAVAILABLE:
                return "The phone is downloading its speech pack. Try again when it finishes.";
            default: // network/server codes cannot happen on-device; anything else is the engine.
                // Never a raw code: a person cannot act on "(11)".
                return "Voice input stopped. Tap the mic and try again, or type instead.";
        }
    }
}
