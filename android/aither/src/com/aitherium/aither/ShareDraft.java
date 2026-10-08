package com.aitherium.aither;

/** What a share into Ask Aither becomes: one draft, subject first, trimmed and capped. Pure. */
final class ShareDraft {
    /** Long enough for a pasted answer, short enough for the phone's model. */
    static final int MAX_CHARS = 4000;

    private ShareDraft() {}

    static String of(String subject, String body) {
        String s = subject == null ? "" : subject.trim();
        String t = body == null ? "" : body.trim();
        String out = s.isEmpty() || t.startsWith(s) ? t : (t.isEmpty() ? s : s + "\n\n" + t);
        return out.length() > MAX_CHARS ? out.substring(0, MAX_CHARS) : out;
    }
}
