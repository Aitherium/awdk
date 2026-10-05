---
name: awshare
description: "Publish a directory as a verifiable bundle and fetch it back byte-checked; keep many backups while paying for unchanged bytes once (dedupe, incremental snapshots). Use to hand a folder over with integrity proof, or when backups or copies fill a disk."
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

## Many copies, bytes paid once (0.2.0)

```bash
awshare dedupe /backups/* --dry-run        # what hard-linking identical files would save
awshare dedupe /backups/*                  # do it: content-verified, nothing deleted
awshare snapshot ./data --store ./objs --name day2 --previous day1   # stores changed files only
awshare restore day1 --store ./objs --dest ./restored              # every digest verified
```

Python: `awshare.link_tree(src, dest, link_dest=prev)` copies a tree, hard-linking files
unchanged since the previous copy (rsync `--link-dest`). `awrecover snapshot --incremental`
uses the same object store, and `awrecover remote push` then sends only new objects.

**Measured 2026-10-03:** platform backups were full 35 GB copies; `dedupe` on the mirror
freed 1.17 TiB and deleted nothing. **Dry-run across two COMPLETE copies first:** a partial
copy makes dedupe look useless (1 GB vs 59 GB on the same data).

**Trap:** hard-linked files share bytes; writing one changes all. Use on write-once trees
(backups, snapshots), never on a working tree.
