# Skill: Mirror Website

Bulk-extract a documentation site or blog into a local Markdown knowledge base,
then cite from it. Use this instead of many individual `fetch_url` calls when you
need multiple pages from the same site, or when the user wants the whole docs set
preserved offline.

## When to use

- "Mirror the entire docs site for X so you can cite from it"
- "Download and preserve the docs for Y"
- "Build a knowledge base from this docs site / blog"
- You need broad coverage of one site (many pages) for a thorough answer

## Procedure

1. Call `mirror_website(base_url, max_pages=25)`.
2. It discovers pages via `sitemap.xml` and `llms.txt`, scoped to the same host
   and URL-path prefix (so `…/docs/` won't drag in the marketing site).
3. Each page is downloaded as Markdown — native `.md` (Mintlify docs) is taken
   verbatim; otherwise the HTML is stripped to readable text — and written under
   the artifacts directory, with an auto-generated `INDEX.md`.
4. **Every page is registered as a citable source.** The return value reports how
   many sources were registered; cite them inline with `[1]`, `[2]`, … exactly
   like `web_search`/`fetch_url` results.

## Call signature

```
mirror_website(
  base_url="https://platform.claude.com/docs/en/api/",
  max_pages=25      # safety cap (default 25, max 200)
)
```

## What you get back

- `pages_mirrored` — pages successfully written as Markdown
- `output_dir` — local directory the mirror was written to
- `index_file` — path to the auto-generated `INDEX.md`
- `sources_registered` — pages added to the citation ledger

## Quality bar

- Cite mirrored pages with inline `[n]` just like any other source.
- Keep `max_pages` modest (≈25) to stay within the research budget; raise it only
  when the user explicitly wants a large mirror.
- The mirror is a one-time bulk snapshot; it does not track later changes to the
  live site.
