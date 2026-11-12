# Aitherium for Claude Code

A Claude Code plugin that connects Claude to Aitherium and helps a new user get
set up.

It installs three things:

- **The Aitherium connector** (`https://mcp.aitherium.com/connector`), a remote
  MCP server. Three tools work before you sign in: `about_aitherium`,
  `check_local_model_fit` and `search_docs`. Four need an Aitherium account:
  `account_status`, `recall`, `list_services` and `search_platform`. Every tool
  is read-only.
- **A setup skill** (`aitherium:setup`) that checks which local model your
  machine can run, then installs the awdk agent toolkit and that model, asking
  before anything is downloaded.
- **The `/aitherium` command** (`/aitherium:aitherium`), with `status`, `fit`
  and `setup`.

## Install

```
/plugin marketplace add Aitherium/awdk
/plugin install aitherium@awsh
```

Then run `/aitherium`. To sign in or create a free account, run `/mcp`, choose
`aitherium` and authenticate in the browser. Sign-in uses OAuth; the plugin
stores no secret and ships no credential.

## What it reads

The connector reads only Aitherium's own services: the public documentation,
the published model catalogue, and, after you sign in, your own account's
memory and deployed services. It does not read Claude's memory, your
conversations or your files. Privacy policy: https://aitherium.com/privacy

## Links

- Connect guide: https://aitherium.com/docs/mcp
- Installer: https://aitherium.com/install
- Support: https://aitherium.com/support
- Licence: Business Source License 1.1 (see the repository `LICENSE`)
