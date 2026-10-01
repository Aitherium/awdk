---
name: awbrowse
description: "Render a web page through a browser service and print its text or save a screenshot. Use when curl gets a JS shell instead of content, or you need a page image."
---

# awbrowse — rendered page text and screenshots

```bash
export AWBROWSE_URL=https://<origin>  AWBROWSE_TOKEN=<bearer>   # or --url/--token
awbrowse get https://example.com [--wait 2000]         # rendered text
awbrowse shot https://example.com -o page.png
awbrowse get URL --engine obscura --obscura-url URL    # alternate engine
awbrowse --self-test                                   # offline contract check
```

`--url/--token` are top-level flags — before the verb.

**Exit codes:** 0 rendered · non-zero on service/transport error.

**Trap:** there is no default origin — it refuses to guess where to send your pages.
For live debugging (console, network, HAR) use the aitherbrowser skill instead; the
observed-API buffer DRAINS on read, so a second call returns nothing.
