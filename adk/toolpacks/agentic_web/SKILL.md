---
name: agentic-web
description: Publish a domain for AI agents from one site profile -- A2A card (+AP2), MCP card, skills index, OAuth metadata, WebMCP, Web Bot Auth keys, DNS-AID, ACP, UCP, x402 (test), MPP -- then deploy, gate and re-scan. Use on "make this site agent-ready", "fix our isitagentready score", or onboarding a business to the agentic web.
---
# Agentic Web

One `SiteProfile` (YAML) describes a domain: who it is, which agents and skills it
exposes, how it authenticates, which DNS-AID records it wants and which commerce
protocols it advertises. Every document is generated from it; nothing is hand-written.

## The loop

1. **Scan before.** `agentic_web_scan(url, mode="both")` -- remote is
   isitagentready.com (a third party sees the URL), local is offline and keyless.
   Both return `pass | fail | neutral` per check id (`discovery.a2aAgentCard`,
   `commerce.acp`, `discoverability.dnsAid`, ...).
2. **Write the profile.** Start from `examples/site-profile.example.yaml`. List only
   endpoints you have seen answer: an A2A card needs a live A2A endpoint, ACP/UCP
   need a real checkout.
3. **Generate.** `agentic_web_generate(profile)` returns every artifact with its
   path, content type and sha256. Nothing is written.
4. **Plan.** `agentic_web_plan(profile_path=..., target=..., target_options=...)`
   returns a `plan_id`, a diff per change and the gates.
   Targets: `static_dir`, `pages_repo_pr` (new branch + PR, never the default
   branch), `veil_public`, `cf_worker` (serves the documents with the right
   Content-Type, passes everything else through), `cf_dns` (DNS-AID SVCB records).
5. **Apply.** `agentic_web_apply(plan_id)`. A gated plan returns `gate_required`
   with the decision card it raised; re-run with `decision_id` once the card is
   answered `approve`.
6. **Verify.** `agentic_web_verify(url, expect="discovery.a2aAgentCard,commerce.acp")`.

## Hard gates

| gate | when | what the pack does |
|---|---|---|
| `dnssec_enable` | DNS-AID needs DNSSEC-validated answers | card + instructions; **never executed** |
| `registrar_ds` | the DS record at the registrar | card + instructions; **never executed** |
| `live_payment` | wallet, facilitator, AP2 role `required: true`, x402/MPP on a revenue route | card; applies only after `approve` |
| `customer_domain_publish` | any change on a domain that is not ours | card; applies only after `approve` |

Everything else (well-known JSON, DNS-AID records, the JWKS, skills index, MCP card)
is reversible and needs no card.

## Web Bot Auth

```python
from adk.toolpacks.agentic_web import signer
jwk = signer.generate_ed25519_jwk()          # store the private JWK in a vault, by name
hdrs = signer.sign_request("GET", "https://example.com/", {}, jwk, "https://your.site")
# -> Signature, Signature-Input (tag="web-bot-auth", keyid = RFC 7638 thumbprint),
#    Signature-Agent. Publish signer.public_jwk(jwk) via web_bot_auth_directory.
```

## Specs

- DNS-AID: `_a2a._agents.<domain>. SVCB 1 agent.<domain>. alpn="a2a" port=443 mandatory=alpn,port`;
  custom params are `keyNNNNN`; validated only when the zone is DNSSEC-signed.
- A2A: `/.well-known/agent-card.json` with `supportedInterfaces[{url, transport}]`;
  AP2 is `capabilities.extensions[{uri: https://github.com/google-agentic-commerce/AP2/tree/v0.1.0, params.roles, required: false}]`.
- ACP: `/.well-known/acp.json` -- `protocol{name: acp, version}`, absolute
  `api_base_url`, `transports[]`, `capabilities.services[]`.
- UCP: `/.well-known/ucp` -- `protocol_version`, `services`, `capabilities`, `endpoints`.
- x402: a 402 with `accepts[]` PaymentRequirements. The generator only emits test
  networks; live mode is a decision.
- MPP: `x-payment-info` on payable operations in `/openapi.json`.
