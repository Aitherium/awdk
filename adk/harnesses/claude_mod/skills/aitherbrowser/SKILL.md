---
name: aitherbrowser
description: >-
  Give an agent its own browser on your machine: enable AitherBrowser as an
  addon, confirm it is serving, then drive a real Chromium with the pilot_*
  tools — open a page with a goal, read what it renders, click and type, extract
  structure, keep a login, and close out with a recipe. Use when an agent must
  browse, scrape, fill a form, check a live site, or watch a page change, and
  when the pages it touches must not pass through anyone else's hardware.
---
<!-- Generated from the awskills pack (skills/aitherbrowser.md, github.com/Aitherium/awskills). Edit the pack, never this copy. -->

# aitherbrowser — an agent's own browser

AitherBrowser is a headless Chromium that an agent drives directly. It is not a
scraper library and not a remote browsing API: it opens real pages, renders them,
and lets an agent look, act and remember.

Enabling it as an addon on your own machine is what makes it yours. Without that
step an agent with browser tools quietly drives somebody else's browser, which
is exactly the thing to avoid when the page is behind a login.

## Before you start: this does not work from a plain `pip install` yet

Three known blockers, verified 2026-11-06:

1. The addon manifest is not shipped in the `awdk` wheel, so
   `adk addon enable aitherbrowser` raises `Unknown addon: aitherbrowser` on a
   pip install. It resolves inside a monorepo checkout only.
2. The container image is not anonymously pullable from any public registry.
3. `adk addon enable` starts the container but loads no tool pack, so no
   `pilot_*` tools are registered by enabling it.

If you hit `Unknown addon`, that is this, not your setup. The rest of this
procedure describes the intended path.

## Enable it

```bash
pip install awdk
adk addon enable aitherbrowser
```

The manager starts the container, waits for health, and records the endpoint.
Then verify — a container that reports `running` is not a page that renders:

```bash
curl -fsS http://127.0.0.1:8132/health
adk addon status aitherbrowser
```

If health fails, read the container's own log before touching configuration:

```bash
adk addon logs aitherbrowser
```

## Drive it

The loop is deliberate: look, then act, then look again. Do not batch actions
against a page you have not seen.

1. **Open.** `pilot_open` with the URL and a one-sentence goal. The goal is what
   later steps are judged against, so write it as an outcome.
2. **Observe.** `pilot_observe` returns what the page currently shows.
   `pilot_parse_ui` returns its controls as structure — prefer it over guessing
   selectors from HTML. `pilot_look` asks a vision model about the rendered page
   when the DOM is not the truth (canvas, charts, oddly-laid-out widgets).
3. **Act.** `pilot_click`, `pilot_fill`, `pilot_scroll`, `pilot_navigate`.
   After every action, observe again. A click that "succeeded" and a page that
   changed are two different claims.
4. **Extract.** `pilot_extract` pulls structured entities out of the page. Ask
   for the shape you need; do not ask for "everything".
5. **Remember.** `pilot_save_login` keeps a login for a domain so the next
   session does not start from zero. `pilot_done` closes the task and, when the
   flow worked, records it as a recipe.
6. **Close.** `pilot_close` when the session is finished. Leaving sessions open
   holds memory.

## Traps

- **A verification step is not optional.** An action returning success means the
  call was accepted, not that the outcome happened. Observe after acting.
- **Probe the scheme you started.** The service answers TLS when configured and
  plain HTTP on a bare node. Probing the wrong one reports a healthy service as
  down.
- **Memory is the usual cause of "flaky".** A couple of tabs sit near 1 GiB and
  spike on a heavy page; the addon declares 2 GiB on purpose. OOM kills look
  like intermittent crashes.
- **`/dev/shm` at the container default silently kills renderers** under load.
  The addon sets 512 MB; keep it if you override the run.
- **Do not recreate the container casually.** Saved logins and taught recipes
  live in a volume. Replacing it loses them without an error.
- **Headless is the default**, and correct on a server. Headful needs a real
  display, and the failure names X11 rather than the missing display.

## Entitlements

Self-hosting the container is free — it costs the platform nothing because it
runs on your hardware. Driving it is not. The `pilot_*` tools need
`can_use_computer_use`, which is a PROFESSIONAL+ entitlement, and the CRM and
HAR-mining tools need `can_use_untether`. Running the addon locally does not
lift those gates.

If a tool comes back unavailable, check two things before assuming it is broken:
whether the entitlement is on your account, and which browser the agent is
actually pointed at.
