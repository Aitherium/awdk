---
name: zeroauthy
description: >-
  Run zeroauthy — an Apache-2.0, clean-room identity provider you host yourself —
  from its published container image: generate the TLS material and master key it
  refuses to start without, bring it up on :8443, create the first admin, and pin,
  upgrade or roll back the image deliberately. Use when a person or a small
  organisation needs sign-in, OIDC, MFA or social and enterprise connections without
  renting an auth vendor.
---
<!-- Generated from the awskills pack (skills/zeroauthy.md, github.com/Aitherium/awskills). Edit the pack, never this copy. -->

# zeroauthy — an identity provider you run

[zeroauthy](https://github.com/occupysaas/zeroauthy) is built by the
[OccupySaaS](https://occupysaas.com) community: a clean-room Go implementation of the
identity-provider surface most teams actually use — hosted universal login, OIDC and
OAuth grants, MFA, passwordless, enterprise and social connections, an admin console
and a management API. It is Apache-2.0. Its own documentation is authoritative; this
skill is the operational path, not a second manual.

**You are running their product.** Nothing here reimplements it, and the skill grants
no right to host it on someone else's behalf.

## Before you start

- A container runtime. The commands below are `docker`; with `podman` they are
  the same arguments under a different binary, and on a SELinux host the certs
  mount needs `:z` on the volume flag.
- A TLS certificate pair. The server terminates TLS itself and will not start without
  one. A local development pair is fine for a first run; use a real one anywhere else.
- A master key. Generate it once, store it somewhere the container cannot lose, and
  back it up before you create the first user — losing it makes the stored data
  unreadable.

Their `docs/quickstart.md` covers generating both. Follow it rather than improvising;
the key formats matter.

## Run it

Pin the tag. The current public release is `1.0.0-rc.1`, a prerelease — write the
version down and do not substitute `latest`.

```bash
docker run -d --name zeroauthy -p 8443:8443 -v zeroauthy-data:/data \
  -v "$PWD/certs:/certs:ro" \
  -e ZEROAUTHY_SERVER__LISTEN=:8443 \
  -e ZEROAUTHY_SERVER__SYSTEM_HOST=system.localhost:8443 \
  -e ZEROAUTHY_SERVER__TLS_CERT_FILE=/certs/cert.pem \
  -e ZEROAUTHY_SERVER__TLS_KEY_FILE=/certs/key.pem \
  -e ZEROAUTHY_SECURITY__MASTER_KEY="$(cat master.key)" \
  ghcr.io/occupysaas/zeroauthy:1.0.0-rc.1
```

Everything mutable lives in the named volume at `/data`. The certificates are mounted
read-only and are not part of it.

## Verify it is actually up

A container that is `Running` is not a container that serves. Check the container's
own log, then make a TLS request from inside the network:

```bash
docker logs --tail 50 zeroauthy
docker exec zeroauthy wget -qO- --no-check-certificate https://localhost:8443/healthz || echo FAILED
```

If the log shows a certificate or master-key error, the process exited after starting;
fix the mount or the key rather than restarting in a loop.

## Create the first admin

```bash
docker exec zeroauthy zeroauthy admin create --email you@example.com
```

Then sign in at `https://system.localhost:8443/` — or at the system host you set.

## Use it as an OIDC provider

Create a tenant, an application and a client in the admin console, then point the
relying party at the issuer you configured. Take the exact discovery and endpoint
paths from their documentation and their management API rather than from memory: the
paths are theirs to change, and a wrong issuer is the most common integration failure.

If you are wiring an agent platform, the contract is ordinary OIDC — discovery, JWKS,
authorization code with PKCE, token, userinfo. Nothing here requires a proprietary
extension.

## Upgrade and roll back

Read their compatibility matrix before you upgrade, not after: it lists what the
current release does and does not cover, and the gaps are real.

```bash
# pull the next tag, then stop and recreate with the SAME volume and env
docker rm -f zeroauthy
# ... re-run the docker run block above with the new tag ...
```

Roll back the same way with the previous tag. The volume survives; that is why it is a
named volume and not a bind to the working directory.

## Traps

- **No certificate, no start.** The server terminates TLS itself. A missing
  `ZEROAUTHY_SERVER__TLS_CERT_FILE` reads as a dead container, not a config error.
- **The master key is not recoverable.** Back it up before the first user exists.
- **The tag is a prerelease.** Pin it explicitly. Pulling `latest` silently changes
  what you are running, and you will not be able to say which version produced a bug.
- **Do not host it for someone else.** Running it on your own machine is the point;
  offering it as a service to a third party is a different decision with its own
  licensing and trademark questions.

## Why this shape

An identity provider is the component every project re-implements badly and then
cannot migrate off. Running one yourself means the user table, the sessions and the
audit trail stay on hardware you control — and if you outgrow it, the export is an
ordinary OIDC migration rather than an account deletion.
