# Skill: Deep Research

A repeatable procedure for answering any research question with rigor and citations.
Loaded by the pack and folded into the agent's working instructions.

## When to use
Any question that asks for facts, comparisons, current events, market/landscape
analysis, "what is the latest on…", or a written report.

## Procedure
1. **Decompose.** Restate the question. List 3–6 concrete sub-questions you must
   answer. Decide what a *good* answer must contain.
2. **Recall first.** Call `recall` on each sub-question. If you already found it
   this session, reuse it — do not spend a fresh search. (This is also what makes
   the token meter show savings.)
3. **Search.** For gaps, call `web_search` (one focused query at a time) or
   `deep_research` for a broad multi-angle sweep. Run at least two queries for any
   non-trivial sub-question. Prefer primary sources and recent, reputable pages.
4. **Read.** Call `fetch_url` on the 2–4 best results per sub-question. Read the
   actual text — snippets are not evidence. Pull out specific numbers, dates,
   named entities, and direct quotes.
5. **Save findings.** For every solid fact, call `save_finding(claim, source_url)`.
   This builds the knowledge graph and prevents re-researching the same thing.
6. **Cross-check.** A claim needs ≥2 independent sources. If sources disagree,
   say so explicitly and present both. Note recency and source quality.
7. **Synthesize.** Write the answer in your own words, grounded entirely in the
   findings, with inline citations `[1]`, `[2]`. Never assert anything you didn't
   retrieve.

## Quality bar
- No uncited non-obvious claims. No invented numbers, quotes, or sources.
- Distinguish fact (cited) from your analysis (labeled as such).
- State confidence and gaps honestly. "I could not find a reliable source for X."
