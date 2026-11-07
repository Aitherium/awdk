---
name: salescompel
description: >-
  Build and run SalesCompel — an open-source, Apache-2.0 Salesforce replacement in
  Go and PostgreSQL — keeping its two database principals separate: migrate as the
  operator DSN, serve as the runtime DSN, create the first user from environment
  variables, and serve under TLS the moment the listener leaves loopback. Use when
  a team wants a CRM they own, is evaluating a Salesforce migration, or needs to
  stand up a record system on its own hardware.
---
<!-- Generated from the awskills pack (skills/salescompel.md, github.com/Aitherium/awskills). Edit the pack, never this copy. -->

# salescompel — a CRM you own

[SalesCompel](https://github.com/occupysaas/salescompel) is an open-source
Salesforce replacement from the [OccupySaaS](https://occupysaas.com) community:
a Go/PostgreSQL candidate, a Vue record workflow, OAuth authorization code with
PKCE, and a compatibility laboratory that records how much of the Salesforce
surface it actually reproduces.

It is early. Their compatibility matrix is the authority on coverage, and no
Salesforce certification is claimed. Read it before you promise anyone a
migration.

## Build it

There is no published image and no release tag. Build from source:

```bash
git clone https://github.com/occupysaas/salescompel
cd salescompel
go build -o salescompel ./cmd/salescompel
```

You need Go 1.25 or newer, and PostgreSQL.

## Two principals, deliberately

The project separates the credential that shapes the schema from the credential
that reads and writes records:

| Variable | Used by | Principal |
|---|---|---|
| `MIGRATION_DATABASE_URL` | `migrate`, `schema`, `user`, `oauth-app`, `provision` | operator |
| `DATABASE_URL` | `serve` — records | runtime |
| `AUTH_DATABASE_URL` | `serve` — identity | runtime |

`serve` opens the runtime principal and refuses owner privileges. Do not collapse
these into one login to make setup easier; that single change removes the
boundary the design exists to create.

Nothing upstream creates the database, the logins or the grants — you do. Example:

```bash
createdb salescompel
psql -d salescompel -c "CREATE ROLE salescompel_op  LOGIN PASSWORD 'change-me';"
psql -d salescompel -c "CREATE ROLE salescompel_app LOGIN PASSWORD 'change-me';"
psql -d salescompel -c "CREATE ROLE salescompel_id  LOGIN PASSWORD 'change-me';"
```

Then point the three DSNs at them. Grant the runtime logins what the schema
needs; the repository's own docs are the authority on which privileges.

## Migrate, then create the first user

Migrations are **forward-only and never run at startup** — a server that migrates
itself on boot is a server you cannot roll back.

```bash
./salescompel migrate status
./salescompel migrate up
```

`user create` reads environment variables rather than flags:

```bash
SALESCOMPEL_TENANT=acme \
SALESCOMPEL_USERNAME=admin@example.com \
SALESCOMPEL_PASSWORD='choose-one' \
SALESCOMPEL_ROLE=owner \
./salescompel user create
```

## Serve it

```bash
./salescompel serve -addr 127.0.0.1:8080
```

- `-assets` defaults to `web/dist`, the built Vue bundle. Build it with Node
  22.12 or newer (`cd web && npm ci && npm run build`), or pass `-assets ""` to
  serve the API alone.
- **A non-loopback listener is refused without TLS.** Binding `0.0.0.0` requires
  `-tls-cert` and `-tls-key`, both or neither. This is not a warning to work
  around; it is the default posture.
- `-public-url` sets the OAuth origin when it differs from the listen address,
  which it does behind any proxy.

## Verify it is actually serving

A process that started is not a service that answers:

```bash
curl -fsS http://127.0.0.1:8080/ || echo FAILED
```

Then sign in through the OAuth flow with the user you created, rather than
assuming the record API and the identity plane agree — they use different DSNs
and can be pointed at different databases.

## Traps

- **Two DSNs for one process.** Pointing `DATABASE_URL` and `AUTH_DATABASE_URL`
  at the same place works, and quietly removes the identity/records split.
- **`migrate up` on a shared database is not reversible.** Take a backup, or run
  `migrate status` first and know what is pending.
- **The UI is built assets.** A `web/` directory in the repo is source; if
  `web/dist` does not exist, `serve` has no UI to serve.
- **Read the compatibility laboratory** before trusting a feature list. It
  records partial support explicitly, and the gaps are the ones a migration hits.
