# Skill: Report Writing

Turn research findings into a clean, sourced deliverable.

## When to use
When the user asks for a report, brief, summary, comparison, memo, or "write it up",
or any time a written artifact (PDF / DOCX / Markdown) is more useful than chat.

## Structure (default)
1. **Title** — specific and dated where relevant.
2. **Executive summary** — 3–5 sentences: the answer up front.
3. **Key findings** — bulleted, each with an inline citation `[n]`.
4. **Detail sections** — one `## heading` per sub-question, with analysis.
5. **Contradictions / open questions** — what's disputed or unknown.
6. **Sources** — auto-appended by the generator from everything you cited.

## How to emit
- `generate_markdown(title, content)` — fast, chat-friendly.
- `generate_pdf(title, content)` — polished hand-off document.
- `generate_docx(title, content)` — when the recipient will edit it.

Use `#`/`##` for headings and `- ` for bullets in `content`; the generator renders
them and appends the **Sources** list automatically — so cite with `[1]`, `[2]`
and make sure each was registered via `web_search`/`deep_research`/`save_finding`.

## Quality bar
- Lead with the answer; don't bury it.
- Every claim in the report traces to a source in the Sources list.
- Tight prose. No filler. No "as an AI". No fabricated precision.
