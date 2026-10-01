---
name: website-as-code
allowed-tools: Bash, PowerShell, Read, Write, Edit, WebFetch
description: Deploy a website as code — GitHub Pages frontend + Cloudflare Tunnel backend on any machine + an automated never-show-a-raw-error fallback. $0/month plus the domain.
argument-hint: "[domain] [--backend-port 8000] [--no-worker]"

---

## Context
- Target domain: $ARGUMENTS
- Worker template: `scripts/fallback-worker/worker.js` + `wrangler.toml.example` (this repo)
- wrangler: !`npx wrangler --version 2>&1 | tail -1`
- cloudflared: !`cloudflared --version 2>&1 | head -1`

## Your Role
You set up the **website-as-code stack** — the architecture that replaces site builders
(Wix/Squarespace) with three free, composable layers the user owns end to end:

1. **Frontend = GitHub Pages.** The static site lives in a git repo, deploys on push, and is
   served by GitHub's CDN. It survives the user's hardware, network, and Cloudflare account
   having a bad day. This layer is the *floor*: the site literally cannot be fully down.
2. **Backend = Cloudflare Tunnel from any machine.** A `cloudflared` daemon on a home PC,
   spare laptop, or $5 VPS publishes `api.domain` (and any other dynamic hostname) with no
   port-forwarding, no static IP, and TLS handled at the edge.
3. **Safety net = the fallback Worker.** On the dynamic hostnames only: passes through when
   healthy, snapshots anonymous HTML as a last-good copy, and during an outage serves that
   copy (bannered, auto-reloading) or a branded maintenance page — **never Cloudflare's raw
   error 530/1033**. API paths get an honest JSON 503.

Total cost: the domain. Everything else is free tier.

## Your Task

### 1. Frontend on GitHub Pages
- Static site (or SPA build output) in a repo; enable Pages (branch or Actions deploy).
- Add the custom domain in Pages settings (creates the CNAME file) and wait for the
  certificate check to pass.

### 2. DNS on Cloudflare (the domain's zone)
- **Apex + www → GitHub Pages, proxy OFF (grey cloud):**
  - `A @` → `185.199.108.153`, `185.199.109.153`, `185.199.110.153`, `185.199.111.153`
  - `AAAA @` → `2606:50c0:8000::153` … `8003::153`
  - `CNAME www` → `<user>.github.io`
  - Grey cloud is deliberate: the static layer should not depend on the proxy either.
- Dynamic hostnames get created by the tunnel (next step) as proxied CNAMEs.

### 3. Backend via Cloudflare Tunnel
```bash
cloudflared tunnel login
cloudflared tunnel create mysite
cloudflared tunnel route dns mysite api.<domain>
```
Config (`~/.cloudflared/config.yml`):
```yaml
tunnel: <tunnel-id>
credentials-file: ~/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: api.<domain>
    service: http://localhost:8000   # the user's backend port
  - service: http_status:404
```
Run it (`cloudflared tunnel run mysite`) and install it as a service
(`cloudflared service install`) so it survives reboots. **Redundancy for free:** run the
same tunnel (same credentials) on a second machine — Cloudflare load-balances the live
connectors and fails over automatically when one dies.

### 4. The automated fallback (unless --no-worker)
- Set up the worker directory:
  ```bash
  mkdir fallback-worker
  cp scripts/fallback-worker/worker.js fallback-worker/
  cp scripts/fallback-worker/wrangler.toml.example fallback-worker/wrangler.toml
  ```
- Edit three fields in `fallback-worker/worker.js` SERVICE config (lines 15–17):
  - `name`: your service name (shows on the maintenance page)
  - `accent`: your brand color (e.g., `#5ad1ff`)
  - `staticFallback`: your GitHub Pages URL (e.g., `https://username.github.io`)
- In `fallback-worker/wrangler.toml`, replace `example.com` with YOUR domain in BOTH places:
  - `zone_name`: the Cloudflare zone where your domain is registered
  - `pattern`: the dynamic hostname(s) you want protected (e.g., `api.yourdomain.com/*`)
  - Add one route entry per dynamic hostname your tunnel uses.

  **Teach the rule: a tunnel hostname missing from routes shows a raw Cloudflare error during an outage.** Adding a hostname = tunnel ingress + routes, same commit, every time.
- Authenticate and deploy:
  ```bash
  npx wrangler login    # opens browser to authorize access
  npx wrangler deploy
  ```
  Verify: `curl -s https://api.<yourdomain> -I` should reach your backend (or show the maintenance page if it's down).

### 5. Verify like you mean it
- `https://<domain>` serves from `server: GitHub.com` (curl -sI).
- `https://api.<domain>` reaches the backend through the tunnel.
- Kill the backend (or stop cloudflared) and confirm `api.<domain>` shows the branded
  maintenance page / JSON 503 — not a raw Cloudflare error. Restart; confirm pass-through.
- Optional: revisit a page you loaded while healthy, with the backend down — you should get
  the last-good snapshot with the banner.

### 6. Hand over
Report the layer map (what fails → what the visitor sees), where each piece lives in the
repo, and the one recurring maintenance rule: **new public hostname ⇒ tunnel ingress +
worker route together.** Suggest a drift check in CI if they add hostnames often.
