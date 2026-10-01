---
name: safe-bulk-rename
description: Rename a token across hundreds or thousands of files in this shared worktree — a package, a directory, a product name — without corrupting load-bearing strings, without committing a peer's in-flight work, and without leaving dangling references. Load BEFORE the first sed of any rename touching more than ~20 files, before renaming a published package, and whenever a rename has to move a directory that other files reference by path. Do NOT load for a rename inside one file, or a symbol rename an LSP can do.
---

# Safe bulk rename

A bulk rename looks like `sed -i s/old/new/g` over a file list. It is not, and
every trap below cost real time on one two-package rename
(1267 files moved, 682 rewritten).

Read this with `concurrent-safe-git`, which owns the commit mechanics. This page
owns what is different about a *rename*: the blast radius is the whole tree, so
the usual "review the diff" does not scale and you need rules instead.

## 1. Some occurrences are LOAD-BEARING. Find them before you sed.

The single most dangerous edit in this class:

```python
material = f"{user}:{Path.home()}:oldpkg-name-secrets-v1"
```

That string is **key-derivation material**. Renaming it changes the derived key
and makes every existing locally-encrypted secret undecryptable. It is not a
name, it is a constant that happens to contain the name.

Enumerate composite tokens first and read every one:

```bash
git grep -I -oh "old-name[a-z0-9_-]*" | sort -u
```

Classes that recur, all real findings:

| what | why it must not move |
|---|---|
| KDF / HMAC / salt material | changes the derived key; silent data loss |
| a User-Agent a WAF rule keys on | the edge starts refusing you (a real incident here: the edge began refusing us) |
| a running container / service name | you orphan what is deployed, then deploy a twin |
| an externally published agent/protocol id | strangers' configs stop matching |
| memory slugs, debt-row ids, `[[wiki-links]]` | records of what happened; links break |
| the REDIRECT package's own name | it exists to keep the old name alive |

That last one bites hardest because it is created *during* the rename: a stub
package declaring `name = "old"` sits inside the newly-renamed tree, so the next
sweep rewrites it into a self-referential package that depends on itself. Put a
loud comment in the file AND keep it in the protected list.

**Mechanism, not discipline.** Do not hand-skip these. Protect them by
substitution so the rename cannot reach them:

```python
for i, tok in enumerate(PROTECTED):
    t = t.replace(tok, NUL + "P%d" % i + NUL)   # park it
t = t.replace(OLD, NEW)
for i, tok in enumerate(PROTECTED):
    t = t.replace(NUL + "P%d" % i + NUL, tok)   # restore it
```

Longest-token-first ordering matters: `old-name-fleet` must be parked before a
rule that would match `old-name`.

## 2. A near-identical spelling is usually a DIFFERENT thing

`oldpkg_name` (underscore) was not the import name of `oldpkg-name`; it was a
separate retired twin package, still imported by live code. Sweeping it would
have broken working imports and rewritten unrelated history. Before including a
variant spelling, grep its call sites and prove they are the same subject.

## 3. Leave the history alone, rewrite the live paths

Split on *what the text is for*, not on file type:

- **Records** — `TECH_DEBT*.md`, `CHANGELOG*`, `*.jsonl` chronicles — keep the
  old name. They describe what was true then.
- **Live references** — docs citing paths, install commands, doc-vault pages
  under a drift gate — must be rewritten, or the gate goes red and every agent
  reading them is sent to a dead path.

## 4. `git grep` reads the INDEX. After a `git mv` it is blind to the new tree.

The nastiest silent failure here. Once you `git mv old new` (especially through
a private `GIT_INDEX_FILE`), `git grep` finds **nothing** under `new/` because
the shared index has never heard of those paths. A re-run of your rewrite
reports "29 files fixed" and silently skips the 1100 files you just moved.

- Use `grep -rIl` on the moved trees, never `git grep`.
- Build a file list *after* the move, not before — a stale list is full of paths
  that no longer exist, and a `if not path.is_file(): continue` guard turns that
  into a silent skip.

## 5. Rename the FILES whose names contain the token

After rewriting content, references point at `new/skills/newname.md` while the
file on disk is still `oldname.md`. Sweep for it and fix both directions:

```bash
find <trees> -name "*old-name*"
```

Then re-check that every rewritten reference resolves.

## 6. Prove you swept nothing of anyone else's

Do not eyeball a 2000-file diff. Assert the property instead: for every file,
the working tree must equal *HEAD's blob with only your rename applied*.

```
expected = rename(git show HEAD:<old-path>)
safe     = normalise(worktree) == normalise(expected)
```

Anything else contains a peer's edits. Stage only `safe`. For a file that is
genuinely both yours and theirs, stage `rename(HEAD blob)` via
`git hash-object -w` + `git update-index --cacheinfo` — your rename lands, their
working-tree edits stay untouched and uncommitted.

Normalise line endings before comparing or CRLF files read as 100% changed.

## 7. Prove you broke no reference

A moved tree preserves resolution by construction, so the only new breakage
comes from files you renamed *within* it. Test it directly rather than trusting
that:

```
for each new-path reference that does not resolve:
    did its OLD equivalent resolve at HEAD~1?
        yes -> YOU broke it
        no  -> it was already dangling
```

Expect false positives from a naive path regex: prose lists (`newpkg-name / awnode`)
and GitHub URLs (`.../newpkg-name/blob/main/...`) both look like paths. The
"was it broken before?" test filters them without hand-triage.

## 8. Renaming a PUBLISHED package is not a rename

PyPI, npm and crates have no rename. You publish a new name; the old one exists
forever. So:

- Ship a **permanent redirect package** under the old name — no code, no console
  scripts, just a dependency on the new one. It is not a transitional artifact.
- Check the registry before choosing a version: the tree is often BEHIND what is
  published, and publishing that is a regression. `check_package_version_ahead_of_registry.py`
  asserts this.
- Make transitional globs match **both** names (`old-*.whl` *and* `new-*.whl`) —
  a committed artifact predates the rename while the next build produces the new
  name, and pinning either alone breaks one side.
- Console scripts and import names usually do NOT need to change. If they do
  not, say so loudly in the commit: it is the difference between "the install
  line changed" and "everything our users type changed".

## 9. Commit fast, in one commit, and re-verify after

A large uncommitted rename is the most fragile state this worktree supports. On
2026-08-19 a peer discarded the working-tree edits **twice** — once mid-rename
and once *after* the commit, leaving HEAD correct and the working tree reverted.

- Commit the whole rename at once. A half-renamed tree is worse than either end.
- Afterwards run `check_no_peer_revert.py`, and re-check that the working tree
  still matches HEAD for your paths.
- Restore a reverted file with `git checkout HEAD -- <path>` — pathspec-scoped,
  never `git checkout .`.
- Release your leases when done; holding a few thousand blocks every peer.

## Order of operations

1. Enumerate composite tokens; build the PROTECTED list.
2. Rewrite content (protected-token-aware) — filesystem grep, not `git grep`.
3. `git mv` the directories.
4. Re-run the rewrite over the moved trees (step 4 above).
5. Rename files whose names carry the token.
6. Classify safe vs peer-dirty; stage only safe + hand-built blobs.
7. Verify no newly-dangling reference.
8. Lease, commit, verify, release.
