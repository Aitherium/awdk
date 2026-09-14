# Site Ingest Studio

A self-contained web analyzer powered by awdk. Point it at any website, and it will produce a structured **SiteSpec JSON** containing:

- **Content inventory** — all discoverable pages with markdown and headings
- **Brand tokens** — company name, colors, typography, tagline
- **SEO metadata** — title, description, keywords, NAP (Name/Address/Phone)
- **Social links** — LinkedIn, Twitter, Facebook, etc.
- **Integration hints** — CRM, chat, form, ecommerce providers detected

Runs locally on your machine with your own LLM key (Anthropic/OpenAI/DeepSeek/Ollama). No sign-in, no cloud required.

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Your LLM Key

Copy `.env.example` to `.env` and fill in your API key:

```bash
cp .env.example .env
# Edit .env with your ANTHROPIC_API_KEY, OPENAI_API_KEY, or DEEPSEEK_API_KEY
```

Or set the environment variable directly:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python serve.py
```

### 3. Start the Server

```bash
python serve.py
```

The app will open in your browser at `http://127.0.0.1:8131`. If not, navigate there manually.

### 4. Analyze a Website

In the chat box, paste a website URL (e.g., `https://example.com`). The agent will:

1. **Enumerate** pages from the sitemap/navigation
2. **Mirror** the entire site as markdown
3. **Screenshot** key pages (home, about, contact, product)
4. **Extract** brand tokens, colors, NAP, social links
5. **Assemble** a SiteSpec JSON
6. **Save** the result to `sitespec.json` in the workspace

### 5. Export Your SiteSpec

The completed `sitespec.json` lives in `.data/site_foundry/default/` (or your configured `AITHER_DATA_DIR`).

## Directory Structure

```
site-ingest/
├── serve.py              # FastAPI + awdk entry point
├── requirements.txt      # Dependencies
├── .env.example          # Configuration template
├── README.md             # This file
├── pack/
│   └── site-ingest/
│       ├── brain_pack.yaml         # Agent persona, system prompt, tool whitelist
│       ├── agent.yaml              # Capabilities, channels
│       └── skills/
│           └── site-ingest.md      # Methodology (enumeration -> mirror -> extract)
├── engine/
│   ├── __init__.py
│   └── tools.py          # mirror_site, screenshot_pages, extract_brand_tokens, save_sitespec
├── web/
│   └── index.html        # Minimal UI (real UI loaded dynamically)
└── tests/
    └── test_tools.py     # Unit tests for extract + save validation
```

## Tools Reference

### `mirror_site(base_url, max_pages=25)`

Download all discoverable pages from a site (via sitemap.xml + llms.txt) as markdown.

**Returns:** `pages_mirrored`, `output_dir`, `index_file`

### `screenshot_pages(url, paths_csv)`

Capture visual screenshots of key pages. Requires `AITHER_BROWSER_URL` env var.

**Returns:** `captured` (count), `images[]` (file paths)

### `extract_brand_tokens(workspace_dir)`

Heuristically scan mirrored markdown for:
- Company name, tagline
- Accent colors, palette, typography
- NAP (name, address, phone)
- Social links (regex-based)

**Returns:** `tokens` (dict with all extracted values)

### `save_sitespec(json_str, workspace_dir)`

Validate and write a SiteSpec JSON object.

**Validates:** Required fields (`slug`, `source_url`, `captured_at`)

**Returns:** `saved` (bool), `path`, `slug`

## SiteSpec JSON Schema

```json
{
  "slug": "example-site",
  "source_url": "https://example.com",
  "captured_at": "2026-01-15T10:30:00Z",
  "content_inventory": [
    {
      "path": "/",
      "title": "Home",
      "headings": ["H1 text", "H2 text"],
      "text_md": "Full markdown content...",
      "assets": [{"type": "image", "url": "..."}]
    }
  ],
  "ia": {
    "pages": [
      {"path": "/", "title": "Home", "purpose": "home|about|product|contact|..."}
    ]
  },
  "brand": {
    "company_name": "Example Corp",
    "tagline": "Your mission statement",
    "tone": "professional|friendly|technical",
    "accent_color": "#FF5733",
    "palette": {
      "primary": "#...",
      "secondary": "#...",
      "accent": "#..."
    },
    "typography": {
      "headings": "Font name",
      "body": "Font name"
    },
    "logo_url": "https://..."
  },
  "seo": {
    "title": "Meta title",
    "description": "Meta description",
    "keywords": ["kw1", "kw2"],
    "nap": {
      "name": "Example Corp",
      "address": "123 Main St, City, ST 12345",
      "phone": "+1-555-0100"
    },
    "service_areas": ["Area 1", "Area 2"],
    "social_links": {
      "linkedin": "https://...",
      "twitter": "https://...",
      "facebook": "https://..."
    },
    "jsonld_hints": {}
  },
  "integrations_hints": [
    {"type": "crm|chat|form|ecomm", "provider": "..."}
  ]
}
```

## Configuration

### Environment Variables

| Variable | Purpose | Example |
|----------|---------|---------|
| `SITE_INGEST_PROVIDER` | LLM provider | `anthropic`, `openai`, `deepseek`, `ollama` |
| `SITE_INGEST_MODEL` | Specific model | `claude-opus-4-1`, `gpt-4`, `deepseek-r1` |
| `ANTHROPIC_API_KEY` | Anthropic key | `sk-ant-...` |
| `OPENAI_API_KEY` | OpenAI key | `sk-...` |
| `DEEPSEEK_API_KEY` | DeepSeek key | `sk-...` |
| `AITHER_BROWSER_URL` | AitherBrowser service | `http://localhost:8200` |
| `AITHER_DATA_DIR` | Workspace root | `/path/to/data` (defaults to `.data/`) |
| `LOG_LEVEL` | Logging verbosity | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### Running on a Different Port

```bash
python serve.py --port 9000 --host 0.0.0.0
```

### Using Local Ollama

```bash
# Install & start Ollama (see https://ollama.ai)
ollama pull mistral  # or any model

# In another terminal:
python serve.py
# When prompted, select "Ollama" provider
```

## Tests

```bash
# Run all tests
python -m pytest tests/ -v

# Run a specific test
python -m pytest tests/test_tools.py::test_save_sitespec_valid -v
```

**Coverage:** `extract_brand_tokens` + `save_sitespec` validation.

## Troubleshooting

### "Not configured — add your LLM API key"

1. Check `.env` has your API key filled in
2. Or set the env var: `export ANTHROPIC_API_KEY=sk-ant-...`
3. Refresh the browser or restart `serve.py`

### "No pages mirrored"

- Site may require authentication (site-ingest only crawls public pages)
- Sitemap.xml or llms.txt might not exist; manual URL entry coming soon
- Try a well-known site first (e.g., `https://platform.anthropic.com`)

### Screenshot failures

- Requires `AITHER_BROWSER_URL` env var pointing to a running AitherBrowser service
- If not set, screenshots are skipped gracefully

## Development

### File Structure

- **serve.py** — FastAPI server, LLM routing, session management
- **engine/tools.py** — Core tool implementations (async where needed)
- **pack/site-ingest/brain_pack.yaml** — Agent persona & system prompt
- **pack/site-ingest/skills/site-ingest.md** — Methodology guide (loaded into system prompt)

### Adding Tools

Edit `engine/tools.py`:

1. Define a new function inside `build_ingest_tools(session)`
2. Add to the return list at the bottom
3. Whitelist in `pack/site-ingest/brain_pack.yaml` → `tools:`
4. Optionally add a skill .md file documenting usage

### Testing Locally

```bash
# Check syntax
python -m py_compile engine/tools.py serve.py

# Run unit tests
python -m pytest tests/ -v

# Lint
ruff check .
ruff check . --fix  # auto-fix
```

## License & Attribution

Built with [awdk](https://github.com/Aitherium/awdk), the portable AitherAgent library for building sign-in-free LLM apps.

---

**Questions?** Check the system prompt in `pack/site-ingest/brain_pack.yaml` or the methodology in `pack/site-ingest/skills/site-ingest.md`.
