# Skill: Site Ingest

A repeatable procedure for analyzing a target website and producing a
structured SiteSpec JSON containing content inventory, brand tokens, SEO
metadata, and business information.

## When to use

- "Analyze this website and give me a SiteSpec JSON"
- "Extract the brand colors, company name, and content from this site"
- "I need the full website metadata (SEO, NAP, pages) in JSON format"
- Preparing a website for a builder/designer tool that needs structured input

## Procedure

1. **Enumerate.** Visit the target URL in your browser (via `fetch_url`). Identify
   the main navigation, sitemap link, and overall structure. List 3–5 major
   sections or service categories.

2. **Mirror.** Call `mirror_site(base_url, max_pages=25)` to download all
   discoverable pages as markdown. This captures the full content inventory
   offline, indexed by URL path.

3. **Screenshot.** Call `screenshot_pages(url, paths_csv)` with a comma-separated
   list of key page paths (e.g., `/,/about,/contact,/products`). This captures
   the visual design and layout to inform brand extraction. Returns a list of
   image paths.

4. **Extract.** Call `extract_brand_tokens(workspace_dir)` to scan the mirrored
   markdown + screenshots and heuristically extract:
   - Company name and tagline (from H1, meta tags)
   - Accent colors (from CSS, images, prominent elements)
   - Palette (primary, secondary, accent)
   - Typography (headings and body fonts if detectable)
   - Contact info: NAP (Name, Address, Phone) — regex over footer/contact pages
   - Social links (LinkedIn, Twitter, Facebook, etc.)
   - Service areas (if local business; from content patterns)

5. **Assemble.** Build the SiteSpec JSON object by hand or via the agent's
   synthesis:
   - Populate `content_inventory[]` with one entry per mirrored page
   - Fill `ia.pages[]` with page purpose (home, about, product, contact, etc.)
   - Populate `brand.*` with extracted tokens
   - Populate `seo.*` with meta title, description, keywords, NAP, social
   - Add `integrations_hints[]` if you spot CRM, chat, form, ecommerce providers

6. **Validate & Save.** Call `save_sitespec(json_str, workspace_dir)`. This
   validates required fields and writes `sitespec.json` to the workspace. Return
   the file path and success status.

## Quality bar

- **Completeness.** All 6 steps are required. Do not stop after mirroring.
- **Accuracy.** Extract REAL data from the site; never fabricate company names,
  colors, or contact info.
- **JSON validity.** Always validate JSON before saving.
- **Coverage.** Include ≥3 pages in the content inventory; ≥5 brand tokens.
- **Nulls allowed.** If info is absent (e.g., no phone number visible), use `null`
  or omit the field rather than inventing it.

## Call signatures

```
mirror_site(base_url, max_pages=25)
  → {"pages_mirrored": 12, "output_dir": "...", ...}

screenshot_pages(url, paths_csv)
  → {"captured": 3, "images": ["/path/to/home.png", ...]}

extract_brand_tokens(workspace_dir)
  → {"company_name": "...", "accent_color": "#...", "nap": {...}, ...}

save_sitespec(json_str, workspace_dir)
  → {"saved": true, "path": "sitespec.json", ...}
```
