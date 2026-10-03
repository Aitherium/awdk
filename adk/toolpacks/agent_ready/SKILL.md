---
name: agent-readiness
description: Make a website discoverable and usable by AI agents (content signals, Link headers, markdown negotiation, API catalog, OAuth discovery, auth.md, MCP and agent-skills cards) and prove each fix with a probe. Use when a scanner such as isitagentready.com reports failing checks, or before launching a site agents should find.
---

# Agent readiness

Agents do not browse; they fetch well-known paths, read headers and ask for
markdown. A site is agent-ready when those requests get real answers. This skill
is the order to fix them in and the traps that waste a day.

## 1. Measure first

Probe the site yourself (no third party) and keep the output; it is your
before-picture:

```
agent_ready_probe("https://example.com")     # awdk agent_ready pack
agent_ready_scan("https://example.com")      # isitagentready.com, third-party
```

Without the pack, the scanner is one request:
`curl -s -X POST https://isitagentready.com/api/scan -H 'content-type: application/json' -d '{"url":"https://example.com"}'`.

## 2. Static files (any host)

| path | content |
|---|---|
| `robots.txt` | `Content-Signal: search=yes, ai-input=yes, ai-train=no` under `User-agent: *` (your own values) |
| `/.well-known/api-catalog` | RFC 9727 linkset: per API an `anchor`, `service-desc`, `service-doc`, `status` |
| `/openapi.json` | the API description the catalog's `service-desc` points at |
| `/.well-known/oauth-protected-resource` | RFC 9728: `resource`, `authorization_servers`, `scopes_supported`, `bearer_methods_supported: ["header"]` |
| `/auth.md` | H1 containing `auth.md`; audience, registration endpoints, methods, how to send the credential |
| `/.well-known/mcp/server-card.json` | `serverInfo`, transport `endpoint`, `capabilities` |
| `/.well-known/agent-skills/index.json` | `$schema` v0.2.0, `skills[]` with `sha256:` digests |
| `/llms.txt` | the markdown summary an agent reads first |

**Digests:** compute `sha256` from the exact bytes you publish, at build time.
A digest computed on a Windows checkout with CRLF line endings will not match
what a Linux CI publishes.

## 3. What static hosting cannot do

GitHub Pages, S3 and plain CDNs cannot set response headers or negotiate
content. Three checks need an edge layer:

- `Link` headers on the homepage (`rel="api-catalog"`, `service-desc`, `service-doc`, `describedby`)
- `/.well-known/api-catalog` served as `application/linkset+json` (an extensionless file is served as `application/octet-stream`)
- `Accept: text/markdown` answered with markdown (`Vary: Accept`)

`agent_ready_worker_template()` returns a Cloudflare Worker that does exactly
this and passes every other request straight through; any error falls back to
passthrough, so the worst a bug can do is drop the extra headers.

**Trap:** a Worker route only fires on a proxied (orange-cloud) DNS record. A
deployed Worker on an unproxied record does nothing and reports success. Verify
with `curl -sI https://example.com/ | grep -i '^link'`.

## 4. OAuth when your issuer lives elsewhere

If your authorization server is on another host (`idp.example.com/identity`),
point `authorization_servers` at it from the protected-resource metadata. To
answer `/.well-known/openid-configuration` at the apex, return the real
issuer's document with a `Link: <...>; rel="canonical"` to it. Never invent an
issuer, and fall through to a 404 when the issuer is down rather than serving a
stale or made-up document.

## 5. Do not publish what you do not run

Every discovery document is an instruction agents will act on.

- **A2A agent card**: only with a live A2A endpoint in `supportedInterfaces`.
- **Commerce (ACP, UCP, AP2, MPP, x402)**: only with a checkout that actually
  takes the payment the document describes, at the real price.
- **Web Bot Auth**: only when your own bots sign their requests with the key.

A missing protocol is an honest fail on a scanner. A fake one sends an agent to
a door that is not there, or worse, into a payment flow that does not exist.

## 6. Prove it

Re-run the probe and the scanner after the deploy is live, not after the merge.
Report each check as pass/fail with its evidence; a check you did not re-run is
not done.
