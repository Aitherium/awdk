package com.aitherium.aither;

import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * What a line SOUNDS like: every TextToSpeech call in the app runs it first, so a reply
 * written for the screen ("*blinks* *bounces* 🐾 Hi Athena!") is said as "Hi Athena!", never
 * "asterisk blinks asterisk" (heard from a child's Sprite, 2026-10-07).
 *
 * The same rules live in lib/media/speakable.py (the voice plane) and awkit speakable.ts
 * (the web); test/SpeakableCheck.java pins the same vectors. Pure Java: runs on a desktop JVM.
 */
final class Speakable {
    private Speakable() {}

    private static final String WORDS = "[A-Za-z][A-Za-z' ,\\-]{0,60}";
    private static final Pattern STAR = Pattern.compile("(?<!\\*)\\*(?![*\\s])([^*\\n]{1,60}?)(?<!\\s)\\*(?!\\*)");
    private static final Pattern UNDER = Pattern.compile("(?<![\\w_])_(?![_\\s])([^_\\n]{1,60}?)(?<!\\s)_(?![\\w_])");
    private static final Pattern PAREN = Pattern.compile("\\((" + WORDS + ")\\)");
    private static final Pattern SQUARE = Pattern.compile("\\[(" + WORDS + ")\\](?!\\()");
    private static final Pattern EMOJI = Pattern.compile(
            "[\\x{1F000}-\\x{1FAFF}\\x{2600}-\\x{27BF}\\x{2B00}-\\x{2BFF}\\x{FE00}-\\x{FE0F}"
                    + "\\x{E0020}-\\x{E007F}\\x{200D}\\x{20E3}\\x{2300}-\\x{23FF}\\x{2190}-\\x{21FF}"
                    + "\\x{3030}\\x{303D}\\x{3297}\\x{3299}]+");
    private static final Pattern LINE_MARK = Pattern.compile("(?m)^[ \\t]*(#{1,6}|>|[-*+•]|\\d+[.)])[ \\t]+");
    private static final Pattern MARKS = Pattern.compile("(?<!\\w)[*_]+|[*_]+(?!\\w)|`+|~~|#+(?=\\s)");
    private static final Pattern ORPHAN = Pattern.compile("^[\\s,.;:!?\\-–—]+");
    private static final Pattern SPACE_PUNCT = Pattern.compile("\\s+([,.;:!?])");

    /** True when a *span* is one word inside a sentence: words on both sides. */
    private static boolean emphasis(String inner, String src, int start, int end) {
        String word = inner.trim();
        if (word.isEmpty() || word.contains(" ")) return false;
        String before = src.substring(0, start).replaceAll("\\s+$", "");
        String after = src.substring(end).replaceAll("^\\s+", "");
        if (before.isEmpty() || after.isEmpty()) return false;
        char b = before.charAt(before.length() - 1), a = after.charAt(0);
        return Character.isLetter(b) && Character.isLetter(a) && Character.isLowerCase(a);
    }

    private static String spans(Pattern rx, String src) {
        Matcher m = rx.matcher(src);
        StringBuilder out = new StringBuilder();
        while (m.find()) {
            String keep = emphasis(m.group(1), src, m.start(), m.end()) ? " " + m.group(1).trim() + " " : " ";
            m.appendReplacement(out, Matcher.quoteReplacement(keep));
        }
        m.appendTail(out);
        return out.toString();
    }

    /** {@code text} with stage directions, emoji and markdown removed, as a voice should say it. */
    static String of(String text) {
        if (text == null || text.isEmpty()) return "";
        String s = spans(STAR, text);
        s = spans(UNDER, s);
        s = PAREN.matcher(s).replaceAll(" ");
        s = SQUARE.matcher(s).replaceAll(" ");
        s = EMOJI.matcher(s).replaceAll(" ");
        s = LINE_MARK.matcher(s).replaceAll("");
        s = MARKS.matcher(s).replaceAll("");
        s = s.trim().replaceAll("\\s+", " ");
        s = SPACE_PUNCT.matcher(s).replaceAll("$1");
        s = ORPHAN.matcher(s).replaceAll("");
        return s.trim();
    }
}
