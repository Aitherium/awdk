---
name: awwall
description: "Fail-closed egress allowlist: declare hosts a workload may reach, check/explain a host, emit hosts/iptables/json. Use when sandboxing an agent's network access."
---

# awwall — egress allowlist that fails closed

```bash
awwall allow api.example.com --description "why"   # --type exact|domain|glob
awwall check api.example.com                       # allowed?
awwall explain evil.example.net                    # which rule allowed or denied it
awwall list
awwall emit --format iptables --output rules.sh    # hosts | iptables | json
```

Policy file: `--policy-file P` (top-level, before the verb); default
`~/.awwall/policy.json`. An empty policy denies everything.

**Exit codes:** `check` 0 allowed · 1 denied · 2 policy file unparseable (cannot
judge — never 0).

**Trap:** multi-part hosts default to DOMAIN rules (subdomains included); pass
`--type exact` when you mean one host only.
