---
name: awtunnel
description: "Validate cloudflared ingress rules and give one local port a public URL. Use before editing tunnel routes, or to expose a local service briefly for a demo or test."
---

# awtunnel — ingress rules and one-port public URLs

```bash
awtunnel validate rules.yaml [--from-cloudflared] [--no-resolve] [--json]
awtunnel check rules.yaml api.example.com /v1/health   # which rule serves this path
awtunnel up --port 8080 [--protocol https] [--once]    # quick public URL (--once: smoke)
awtunnel status                                        # is the recorded tunnel alive
awtunnel down
```

**Exit codes:** 0 ok · 1 validation failed / nothing to act on · 2 could not judge
(bad file, no cloudflared, tunnel never came up).

**Trap:** a rule set that validates can still fail at runtime: `http://` into a TLS
origin closes the socket and cloudflared reports "Unable to reach the origin" while
the origin is healthy. Match the origin's scheme, and never list the same host:port
under both schemes.

Trap: an `up` URL is PUBLIC and has NO auth. Never point it at a fleet port (8150 MicroScheduler, 8182 MCP gateway, 8001 Genesis); use `--once` or run `down` when done.
