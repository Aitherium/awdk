---
name: repo-to-website
allowed-tools: Bash, PowerShell, Read, Write, Edit, WebFetch
description: Turn any GitHub repo into a real website on GitHub Pages — a proper landing page with your README's content, live at a URL, instead of a raw README. Free.
argument-hint: "[owner/repo] [--docs | --spa <build-dir> | --plain] [--domain example.com]"

---

## Context
- Target repo: $ARGUMENTS (default: the repo in the current directory)
- gh CLI: !`gh --version 2>&1 | head -1`
- Auth: !`gh auth status 2>&1 | grep -E "Logged in|not logged" | head -1`
- Sibling skill: `/website-as-code` (custom domain + tunnel backend + fallback worker)

## Your Role
You take a repository that today greets visitors with a raw README and give it a **real
website** on GitHub Pages: styled landing page, project name + tagline as a hero, the
README's actual content rendered below, install/usage front and center, links to releases
and docs. Live at `https://<owner>.github.io/<repo>/` in minutes, $0, deploys itself on
every push. No site builder, no lock-in — the site is code in the repo.

## Your Task

### 1. Read the project first
Look at the README, language, badges, releases, and any docs/ or existing site config.
The landing page must reflect what the project actually is — name, one-line pitch,
install command, a real usage example — not a generic template. Pick the mode:
- **default**: generate a landing page (below) + rendered README.
- `--docs`: publish the existing `docs/` folder (mkdocs/jekyll if configured).
- `--spa <dir>`: publish an existing build output (add the Actions workflow to build it). **If your app uses client-side routing** (React Router, Vue Router, etc.), the workflow must also run `touch <dir>/.nojekyll && cp <dir>/index.html <dir>/404.html` after the build so non-root routes fall back to index.html instead of 404.
- `--plain`: no generation — just enable Pages off the README with a clean Jekyll theme
  (`_config.yml` with `theme: jekyll-theme-cayman` + title/description). The 60-second path.

### 2. Generate the site (default mode)
Create `site/` (or `docs/` if they prefer) with a single self-contained `index.html`:
- Hero: project name, tagline (from the README's first paragraph), primary buttons
  (Install / GitHub / Releases), the repo's language + license + stars badges.
- Body: the README rendered to HTML (use `gh api /repos/{owner}/{repo}/readme` +
  `POST /markdown` — GitHub renders it exactly like github.com does).
- Styling: one inline `<style>` block, dark-mode aware (`prefers-color-scheme`), system
  fonts, max-width column, responsive. No external CDNs — the page must be self-contained.
- Footer: "built from this repo's README — edit README.md and push to update."

### 3. Deploy via GitHub Actions (the durable way)
Add `.github/workflows/pages.yml`: on push to the default branch, (re)generate the README
rendering if in default mode (or run the SPA build for `--spa`), upload with
`actions/upload-pages-artifact` + `actions/deploy-pages`. Then enable Pages for Actions:
```bash
gh api -X POST repos/{owner}/{repo}/pages -f build_type=workflow 2>/dev/null \
  || gh api -X PUT repos/{owner}/{repo}/pages -f build_type=workflow
```
Commit, push, watch the run (`gh run watch`), and confirm the deployment.

### 4. Verify like you mean it
- `curl -sI https://<owner>.github.io/<repo>/` → 200 (Pages can take ~1 min on first deploy).
- Open-graph sanity: `<title>` + `<meta name="description">` set from the project pitch.
- The README content is actually IN the page (grep for a distinctive phrase).

### 5. Custom domain (--domain, optional)
Set the CNAME via `gh api -X PUT repos/{owner}/{repo}/pages -f "cname=<domain>"`, then hand
off to `/website-as-code <domain>` for DNS (Pages IPs, grey cloud), a tunnel backend, and
the never-raw-error fallback worker.

### 6. Hand over
Give them the live URL, where the page's source lives, and the loop: **edit README, push,
site updates itself.** If they later want a backend behind it, that's `/website-as-code`.
