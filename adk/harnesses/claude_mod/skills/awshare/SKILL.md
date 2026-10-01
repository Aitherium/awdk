---
name: awshare
description: "Publish a directory as a verifiable bundle (optionally sealed) and fetch it back byte-checked. Use to hand a folder to another machine or person with integrity proof."
---

# awshare — publish a directory, fetch it back verified

```bash
awshare publish ./results --out ./bundle [--name NAME] [--seal]
awshare inspect ./bundle/<name>.awshare.json      # what is in it, without extracting
awshare fetch ./bundle/<name>.awshare.json --dest ./restored [--key HEX]
awshare --self-test
```

`--seal` encrypts with awseal (must be installed); the fetch side then needs `--key`.

**Exit codes:** `fetch` 0 verified · 1 verification FAILED (bytes differ) ·
2 could not check at all (missing archive, unknown manifest version, sealed bundle and
no awseal).

**Trap:** treat 2 as "unverified", never as success — collapsing the two is exactly
how "I could not check" becomes "it checked out".
