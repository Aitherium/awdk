package com.aitherium.aither;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * One streamed answer, as the watch shows and speaks it. Pure Java (no Android), so the
 * rules run on a desktop JVM (test/WearFeedCheck.java).
 *
 * /api/agent-chat streams Genesis' eager protocol: {@code answer_segment} (kind initial,
 * continuation or refinement), then {@code token} events, then {@code segment_end}; the
 * terminal {@code answer}/{@code complete} can arrive half a minute later, after a refine
 * pass (measured 2026-10-08: last token 2.3 s, complete 34.8 s). So the watch never waits
 * for it. Text is shown as the tokens arrive, and every finished sentence is handed out at
 * once to be spoken, so the voice starts while the rest is still arriving and the screen
 * and the speech stay together.
 *
 * - initial / continuation: appended.
 * - refinement: replaces what is shown (the display generation goes up, so a highlight for
 *   the old text is dropped); sentences already spoken are not said a second time.
 * - the terminal answer is used only when nothing streamed (a server without eager
 *   streaming sends only that); after segments it repeats the last one and is ignored.
 *
 * A code block is shown but not read: its first sentence is said as "the code is on screen".
 */
final class WearFeed {
    /** One piece of the answer to speak: where it sits on screen and what is said. */
    static final class Sentence {
        final int start, end, gen;
        final String text, say;

        Sentence(int start, int end, int gen, String text, String say) {
            this.start = start;
            this.end = end;
            this.gen = gen;
            this.text = text;
            this.say = say;
        }

        @Override
        public String toString() { return say; }
    }

    /** The first piece may stop at a clause once it is this long: the voice starts sooner. */
    static final int FIRST_CLAUSE = 28;
    /** Spoken characters per answer; the rest stays on screen (Talk.MAX_SPOKEN's rule). */
    static final int MAX_SAY = 1200;

    private final StringBuilder shown = new StringBuilder();
    private final Set<String> said = new HashSet<>();
    private int cursor;      // shown[0, cursor) has been cut into sentences
    private int gen;         // bumped whenever the shown text is replaced
    private boolean fence;   // inside a ``` block
    private boolean fenceSaid;
    private boolean any;     // a sentence has been handed out
    private int saidChars;
    private boolean capped;
    private boolean streamed; // an answer_segment arrived: the terminal answer is not the whole text

    String text() { return shown.toString(); }

    int gen() { return gen; }

    /** A new answer_segment. Returns the sentences finished by it (none, today). */
    List<Sentence> segment(String kind) {
        streamed = true;
        List<Sentence> out = new ArrayList<>();
        if ("refinement".equals(kind)) {
            replace("");
        } else if (shown.length() > 0) {
            out.addAll(flush());
            if (!endsWithBreak()) shown.append("\n\n");
            cursor = shown.length();
        }
        return out;
    }

    /** Some streamed text. Returns the sentences it finished. */
    List<Sentence> token(String t) {
        if (t == null || t.isEmpty()) return new ArrayList<>();
        shown.append(t);
        return cut(false);
    }

    /** segment_end (or the stream ended): whatever is left is a sentence. */
    List<Sentence> flush() {
        return cut(true);
    }

    /**
     * The terminal answer. After streamed segments it is only the last segment's text (Genesis
     * eager: "answer" = the continuation), so nothing changes; from a server that streamed
     * nothing it is the whole answer and is shown and said.
     */
    List<Sentence> last(String answer) {
        String a = answer == null ? "" : answer.trim();
        if (streamed || a.isEmpty() || norm(a).equals(norm(shown.toString()))) return flush();
        replace(a);
        return cut(true);
    }

    private void replace(String text) {
        shown.setLength(0);
        shown.append(text);
        cursor = 0;
        gen++;
        fence = false;
    }

    private boolean endsWithBreak() {
        int n = shown.length();
        return n >= 2 && shown.charAt(n - 1) == '\n' && shown.charAt(n - 2) == '\n';
    }

    private List<Sentence> cut(boolean all) {
        List<Sentence> out = new ArrayList<>();
        while (cursor < shown.length()) {
            int end = boundary(cursor, !any);
            if (end < 0) {
                if (!all) break;
                end = shown.length();
            }
            Sentence s = make(cursor, end);
            cursor = end;
            if (s != null) out.add(s);
        }
        return out;
    }

    /** Index just past the next sentence end at or after {@code from}, or -1 (not yet). */
    private int boundary(int from, boolean first) {
        int n = shown.length();
        for (int i = from; i < n; i++) {
            char c = shown.charAt(i);
            if (c == '\n') return i + 1;
            if (c == '`' && i + 2 < n && shown.charAt(i + 1) == '`' && shown.charAt(i + 2) == '`') {
                // a fence line is its own piece
                int nl = shown.indexOf("\n", i);
                return nl < 0 ? -1 : nl + 1;
            }
            boolean stop = c == '.' || c == '!' || c == '?' || c == '…';
            boolean clause = first && (c == ',' || c == ';' || c == ':' || c == '—') && i - from >= FIRST_CLAUSE;
            if (stop || clause) {
                int j = i + 1; // past closing marks: **Yes.** / "Done." / (here.)
                while (j < n && "*_)]\"'”’".indexOf(shown.charAt(j)) >= 0) j++;
                // "29.5", "e.g." and an initial ("J. R.") are not sentence ends
                if (j < n && Character.isWhitespace(shown.charAt(j)) && !(c == '.' && abbreviation(i))) {
                    return j;
                }
            }
        }
        return -1;
    }

    /** The '.' at {@code dot} ends a lone letter ("J.", "e.g.") or a known short title. */
    private boolean abbreviation(int dot) {
        int w = dot;
        while (w > 0 && Character.isLetter(shown.charAt(w - 1))) w--;
        String word = shown.substring(w, dot).toLowerCase();
        return word.length() == 1 || TITLES.contains(word);
    }

    private static final Set<String> TITLES = new HashSet<>(java.util.Arrays.asList(
            "mr", "mrs", "ms", "dr", "st", "mt", "vs", "approx"));

    private Sentence make(int start, int end) {
        String raw = shown.substring(start, end);
        String t = raw.trim();
        if (t.isEmpty()) return null;
        String say;
        if (t.startsWith("```")) {
            fence = !fence;
            if (fence && !fenceSaid) {
                fenceSaid = true;
                say = "The code is on screen.";
            } else {
                return null;
            }
        } else if (fence) {
            return null;
        } else {
            say = Talk.spoken(t);
        }
        if (say.isEmpty()) return null;
        String key = norm(say);
        if (!said.add(key)) return null; // a refinement repeating what was already said
        if (capped) return null;
        if (saidChars + say.length() > MAX_SAY && any) {
            capped = true;
            say = "The rest is on screen.";
        }
        saidChars += say.length();
        any = true;
        int lead = raw.indexOf(t.charAt(0));
        return new Sentence(start + Math.max(0, lead), start + Math.max(0, lead) + t.length(), gen, t, say);
    }

    /** Where each word of {@code text} ends (the index past its last character). */
    static int[] wordEnds(String text) {
        List<Integer> ends = new ArrayList<>();
        for (int i = 0; i < text.length(); i++) {
            boolean last = i + 1 == text.length() || Character.isWhitespace(text.charAt(i + 1));
            if (!Character.isWhitespace(text.charAt(i)) && last) ends.add(i + 1);
        }
        int[] out = new int[ends.size()];
        for (int i = 0; i < out.length; i++) out[i] = ends.get(i);
        return out;
    }

    /**
     * How far into the text the voice is, {@code posMs} into {@code durMs} of audio: the end
     * of the word being said. Speech time is taken as proportional to characters, which keeps
     * a sentence's highlight within a word or two of the voice.
     */
    static int reached(int[] ends, int posMs, int durMs) {
        if (ends.length == 0) return 0;
        int total = ends[ends.length - 1];
        float at = total * Math.max(0f, Math.min(1f, posMs / (float) Math.max(1, durMs)));
        for (int e : ends) if (e >= at) return e;
        return total;
    }

    static String norm(String s) {
        return s == null ? "" : s.replaceAll("\\s+", " ").trim().toLowerCase();
    }
}
