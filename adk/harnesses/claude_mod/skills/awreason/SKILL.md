---
name: awreason
description: "Ask the awreason reasoning service a question at a chosen depth. Use for a deliberate multi-step answer from the service, or to check it is up."
---

# awreason — client for the reasoning service

```bash
export AWREASON_URL=https://<origin>   AWREASON_TOKEN=<bearer>   # or --url/--token
awreason health
awreason ask "why does X happen" --depth deep     # skip|shallow|gate|deep|critical
awreason session MyAgent "long multi-part query"
awreason --json stats
awreason --self-test                               # contract check, offline
```

`--url/--token/--json` are top-level flags: they go BEFORE the subcommand.

**Exit codes:** 0 answered · non-zero on a transport or service error (a refused
connection prints the socket error and exits 1).

**Trap:** without `AWREASON_URL` the client does not discover the fleet service — you
get "actively refused". Set the origin explicitly; it never guesses one.
