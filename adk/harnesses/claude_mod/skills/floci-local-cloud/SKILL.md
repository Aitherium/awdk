---
name: floci-local-cloud
description: Exercise cloud automation (AWS/Azure/GCP/OCI) against local MIT-licensed emulators instead of real accounts — no credentials, no credit burn, no live instance at risk. Load before writing or testing anything that calls a cloud SDK: rental/provisioning code, S3 artifact sync, IAM or secrets paths, or any agent tool that touches a cloud API. Also load when a cloud probe "returns nothing" and you need to know whether that means empty or unreachable.
---

# Floci — any cloud, locally

Four emulators, one container each, MIT, no auth tokens and no feature gates:

| cloud | image | port |
|---|---|---|
| AWS | `floci/floci` | 4566 |
| Azure | `floci/floci-az` | 4577 |
| GCP | `floci/floci-gcp` | 4588 |
| OCI | `floci/floci-oci` | 4599 |

Point an existing SDK at the endpoint and keep the workflow. Upstream:
`github.com/floci-io`.

## Why this matters here specifically

Cloud automation on this platform has one expensive property: **the failure
modes are only visible against a real account, and a real account bills.** The
2026-08-18 session lost an 8xA100 with 2.5 TB of local data to a teardown path
that had never been exercised, and idled another box for 2 days 21 hours
(~$1,094) because nothing was watching. Both were code paths nobody could
rehearse.

An emulator makes them rehearsable. The rule this earns:

> **Exercise a cloud code path locally before it runs against an account.**
> Same reason the corpus fitness gate runs before GPU time rather than after —
> the cheapest place to find a defect is before it costs anything.

Free AWS credits make this *more* important, not less: nothing bills you into
noticing a mistake, so the feedback that normally arrives as an invoice never
arrives at all.

## Wiring it into code you already have

Most SDK wrappers already read an endpoint override from the environment.
Point yours at the emulator and the rest of the code does not change:

```bash
# Run the real rental gate against emulated EC2
AWS_ENDPOINT_URL=http://<floci-host>:4566 \
  <your own reaper/ledger check for rented instances>
```

Unset in production, where boto3's own resolution applies. With the override
set and no `AWS_SECRET_ACCESS_KEY` in the vault, the probe uses a placeholder —
an emulator accepts anything, and refusing would make the local path untestable
for exactly the people who have no AWS credential yet.

That is the pattern to copy for the rest: **an endpoint override, defaulting to
real, never a separate code path.** A "test mode" that runs different code
proves nothing about the code that will run.

## 🚨 The reachability trap — measured, and it bites immediately

If your container engine runs inside a VM or WSL distro, a published
port lands on the DISTRO's network, not Windows'. Measured 2026-08-18:

```
curl http://127.0.0.1:4566/     from Windows  ->  hangs
curl http://127.0.0.1:4566/     inside the container ->  200
```

This is [[drive-letter-paths-strand-files]]'s networking cousin, and the same
shape as the Proton Bridge problem in reverse (there, a Windows-loopback service
was unreachable from the fleet; here, a fleet service is unreachable from
Windows). So:

- **Reach Floci from inside the fleet** — a container on the same network, or
  the distro itself. That is also where the code under test usually runs.
- From Windows, forward it the way the mail hop does
  (`bridge_smtp_forwarder.py` is the worked example), or run the test in the
  distro.
- A hang here is a ROUTING fact, not an emulator fault. Do not conclude Floci
  is broken from a Windows curl.

```bash
# start (from the distro)
podman run -d --rm --name floci-aws -p 4566:4566 docker.io/floci/floci:latest
podman exec floci-aws sh -lc 'curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:4566/'
```

## Where it belongs next

The integration is deliberately one env var so far. The obvious extensions, in
the order they pay off:

1. **S3 artifact sync** — the thing that would have saved the 42.5-hour capture.
   Develop the sync against Floci's S3, then flip the endpoint. This is the
   highest-value next step and needs no AWS account to build.
2. **An adk tool pack** — `floci_up` / `floci_down` / `floci_status` so an agent
   can stand up a cloud sandbox for a task and tear it down after, the same way
   it takes a git lease.
3. **AitherZero provisioning scripts** — rehearse `0xxx` cloud-provision steps
   against an emulator in CI, where no runner has cloud credentials.

## What it is NOT

Not a substitute for a real integration test before production. An emulator
proves the CALL SHAPE — arguments, pagination, error handling, teardown order.
It cannot prove IAM policy, quota, region capacity, or price. Treat a green
local run as "the code path executes correctly", never as "this will work on the
account", and keep the doctrine that a probe which cannot judge is DEAD rather
than passing.
