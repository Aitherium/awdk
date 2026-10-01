---
name: ship-an-app-free
description: Take an idea to a working app at a public URL using only free tiers — GitHub Pages for the site, GitHub Actions for the build, and a free serverless backend when the app needs one. Written for someone who has never deployed anything: what to choose, the exact commands, the settings that silently break a deploy, and how to verify the live URL actually serves your app.
---

# ship-an-app-free — idea → public URL, $0, no server to rent

You do not need a credit card, a VPS, or a devops background. You need a GitHub account and an
agent that can run commands.

## Choose the shape first — this decides everything after

| Your app | Host | Cost |
|---|---|---|
| Site, docs, portfolio, dashboard, calculator, game — **runs entirely in the browser** | **GitHub Pages** | free forever |
| Needs a server: logins, a database, secret API keys, webhooks | **Cloudflare Workers** free tier | free to 100k req/day |
| Needs to run on a schedule | **GitHub Actions** cron | free for public repos |
| Needs a real database | **Cloudflare D1** or **Turso** free tier | free tier |

> **Start in the browser if you possibly can.** A static app has no server to secure, no bill
> to run up, no cold starts, and no way to leak a secret key. Most "I need a backend" ideas
> don't — until they need logins or a private API key.

**A secret in browser code is public.** Anyone can read it with View Source. If your app needs
an API key, that is exactly the moment it needs a backend — jump to the Workers section.

---

## Path A — static app on GitHub Pages

### 1. Make the repo

```bash
mkdir my-app && cd my-app
git init
echo "<h1>hello</h1>" > index.html
git add -A && git commit -m "first commit"
gh repo create my-app --public --source=. --push
```

No `gh`? Install the [GitHub CLI](https://cli.github.com/), or create the repo in the web UI
and `git push` to it.

### 2. Turn Pages on

```bash
gh api -X POST repos/{owner}/my-app/pages -f build_type=workflow
```

Or in the web UI: **Settings → Pages → Source: GitHub Actions**.

### 3. Add the deploy workflow

`.github/workflows/deploy.yml` — this exact file works for a plain HTML/CSS/JS app:

```yaml
name: Deploy to Pages
on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read
  pages: write
  id-token: write

concurrency:
  group: pages
  cancel-in-progress: false

jobs:
  deploy:
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/configure-pages@v5
      - uses: actions/upload-pages-artifact@v3
        with:
          path: .            # for a built app use ./dist or ./build
      - id: deployment
        uses: actions/deploy-pages@v4
```

**The `permissions:` block is not optional.** Without `pages: write` and `id-token: write` the
deploy fails with a permissions error that reads like an account problem but is just this.

For a framework app (React/Vite/Next static export), build first and upload the output dir:

```yaml
      - uses: actions/setup-node@v4
        with: { node-version: '20', cache: 'npm' }
      - run: npm ci && npm run build
      - uses: actions/upload-pages-artifact@v3
        with:
          path: ./dist       # Vite: dist · CRA: build · Next export: out
```

### 4. Deploy and verify

```bash
git add -A && git commit -m "add deploy workflow" && git push
gh run watch                                    # follow the build
gh api repos/{owner}/my-app/pages --jq .html_url # your live URL
```

**Verify it serves YOUR app, not a 404 page:**

```bash
curl -sSL -o /dev/null -w "%{http_code}\n" "$(gh api repos/{owner}/my-app/pages --jq .html_url)"
curl -sSL "$(gh api repos/{owner}/my-app/pages --jq .html_url)" | head -20
```

A `200` alone is not proof — GitHub's 404 page is itself served with a 200 in some paths.
**Read the HTML and confirm it's yours.**

### The four things that break a Pages deploy

| Symptom | Cause | Fix |
|---|---|---|
| 404 at the URL, workflow green | uploaded the wrong `path:` | point it at the real build output |
| Blank page, console 404s on `/assets/…` | app assumes it's at the domain root; Pages serves at `/repo-name/` | set the base path — Vite: `base: '/my-app/'`; Next: `basePath` |
| Deploy fails on permissions | missing `permissions:` block | add all three lines |
| Pushes don't redeploy | workflow watches the wrong branch | match `branches:` to your default branch |
| 404 on client routes like `/app/dashboard` | SPA client-side routing fallback missing | after build: add `touch out/.nojekyll && cp out/index.html out/404.html` (Vite: `dist/`; CRA: `build/`; Next: `out/`) |

---

## Path B — needs a backend: Cloudflare Workers

Use this when you have logins, a private API key, a database, or webhooks.

```bash
npm install -g wrangler
wrangler login
wrangler init my-api
cd my-api
wrangler deploy
```

You get a public `*.workers.dev` URL immediately. Keep secrets **out of the code**:

```bash
wrangler secret put MY_API_KEY          # prompts; never appears in the repo
```

Read it in the Worker as `env.MY_API_KEY`. **Never** put a key in a `.js` file, in
`wrangler.toml`, or in a GitHub Actions log.

Wire your Pages frontend to it by calling the Worker URL, and allow that origin in the
Worker's CORS headers. The **[`website-as-code`](website-as-code.md)** and
**[`repo-to-website`](repo-to-website.md)** skills in this pack cover the site side in depth.

---

## Path C — runs on a schedule

Free cron, no server, in `.github/workflows/cron.yml`:

```yaml
name: scheduled job
on:
  schedule:
    - cron: '0 9 * * *'      # 09:00 UTC daily — UTC, not your timezone
  workflow_dispatch:          # keep this: lets you run it manually to test

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: ./my-script.sh
```

**Always include `workflow_dispatch`** — otherwise your only way to test a daily job is to
wait a day. Scheduled runs on free accounts can be delayed by several minutes under load, and
GitHub disables schedules on repos with no activity for 60 days.

---

## Let your agent build it

You have an agent and a local model. Use them:

> "Build a single-page app that does X. Plain HTML, CSS, and JavaScript, no build step.
> Then follow the ship-an-app-free skill to deploy it to GitHub Pages and give me the live URL."

Then hold it to the verification step — **make it show you the fetched HTML from the live
URL**, not just a green checkmark. A green workflow that deployed the wrong directory is the
most common outcome, and it looks like success everywhere except the actual page.

## Free-tier limits worth knowing before you build

| Service | Free tier | Where it bites |
|---|---|---|
| GitHub Pages | 1 GB site, 100 GB/mo bandwidth, ~10 builds/hr | soft limits; fine for real projects |
| GitHub Actions | unlimited for public repos; 2,000 min/mo private | make the repo public if you can |
| Cloudflare Workers | 100k requests/day | plenty for a side project |
| Cloudflare D1 | 5 GB storage | plenty for a side project |

**Public repos get dramatically more free compute than private ones.** If the code isn't
sensitive, make it public — and run [`secretguard`](secretguard.md) over it first to be sure
nothing private is committed.

## Next

- **[`secretguard`](secretguard.md)** — scan for leaked secrets **before** going public
- **[`security-audit`](security-audit.md)** — OWASP pass once it's live
- **[`repo-to-website`](repo-to-website.md)** / **[`website-as-code`](website-as-code.md)** — richer site builds
- **[`aither-start`](aither-start.md)** — the guided path this plugs into
