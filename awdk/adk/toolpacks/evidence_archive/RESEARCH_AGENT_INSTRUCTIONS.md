# Filing to the evidence archive — instructions for an agent

Point your agent at this file. It is the whole contract.

Base URL: `https://<your-archive-host>/api/research`
Every call is scoped to your tenant by your session; you cannot see or write
another archive.

---

## The one rule that matters

**Resolve before you create. Every time. No exceptions.**

```
GET /api/research/actors/resolve?name=Jane%20Doe
```

If `exists: true`, use the `actor_id` you were given.
If `exists: false`, look at `candidates` before creating anything.

An archive that ends up with two records for one person has **split its own
timeline in half**, and nothing will ever tell you. Every query keeps answering
— just with less than the truth. That failure is silent, permanent, and it is
the reason this endpoint exists.

`/actors/resolve` never writes. Calling it costs nothing.

---

## The four calls

### 1. Does this person already exist?

```http
GET /api/research/actors/resolve?name=Jane%20Doe
```

Matching is spelling-tolerant. All of these find the same record:

```
"Jane Doe"   "JANE_DOE"   "Doe, Jane"   "  jane   doe  "
```

It will **not** match abbreviations — `"some org"` does not resolve to
`"Some Organization"`. That is deliberate: auto-merging on a loose match means
attributing one party's actions to another. Near matches come back as
`candidates` for **you** to confirm.

### 2. Create an actor — only after resolve said `false`

```http
POST /api/research/actors
{ "name": "Jane Doe", "kind": "person",
  "aliases": ["J. Doe"], "source_url": "https://…" }
```

Returns **409** with the existing `actor_id` if the name resolves to someone
already there. That refusal is a feature — recover by using the id it hands you.

Add every spelling you have seen as an `alias`. An alias blocks a future
duplicate exactly as firmly as the canonical name does.

### 3. File a piece of evidence

```http
POST /api/research/evidence
{ "summary":     "One sentence: what is claimed",
  "occurred_at": "2026-03-01",        // when the EVENT happened, not today
  "grade":       "primary-document",  // from GET /schema
  "source_url":  "https://…",         // REQUIRED
  "actor_names": ["Jane Doe"],
  "archive_ref": "manifest row id, if archived",
  "lever":       "your organizing category, if any" }
```

- `source_url` is **required**. A claim whose provenance is optional becomes a
  claim with no provenance, and then the archive is just assertions.
- `occurred_at` is the date of the **event**, not the date you filed it. Getting
  this wrong is how a timeline stops being a timeline.
- Unknown `actor_names` are **reported back**, never created. Check
  `unresolved_actor_names` in the response: those did not link to anyone. Resolve
  each, create it deliberately, then re-file.

### 4. Everything about one actor, in one call

```http
GET /api/research/actors/{actor_id}
```

Returns the actor and its full timeline in chronological order. Use this rather
than fetching evidence separately — one hop, nothing forgotten.

---

## Corrections: supersede, never delete

There is **no delete**. Wrong entry? File a new one with:

```json
{ "supersedes": "<the old evidence_id>", ... }
```

Both remain readable. In an archive, "we removed it" and "it was never there"
must not be indistinguishable after the fact — that property is the difference
between a record and a claim about a record.

---

## Grades describe SOURCING, not conclusions

`GET /api/research/schema` returns the live vocabulary. The default set grades
**how well a claim is evidenced**:

```
primary-document · contemporaneous-report · secondary-report
single-source · disputed · unverified
```

They say nothing about whether something is true or what it means. Your
archive's own source-discipline document defines what each one requires; this
API only enforces that you picked one of them.

If you are unsure of the grade, the honest answer is usually `single-source` or
`unverified`. Over-grading is the one error the archive cannot detect for you.

---

## Proving the record was not tampered with

```http
GET /api/research/audit/verify
```

Every write appends to a hash-chained log. Removing or editing an entry breaks
the chain, and this endpoint says so. `status: "unavailable"` means it could not
judge — that is **not** a pass.

---

## A complete first session

```
GET  /api/research/schema                                → learn the grades
GET  /api/research/actors/resolve?name=Jane%20Doe        → exists: false
POST /api/research/actors    {"name":"Jane Doe"}         → actor_id
POST /api/research/evidence  {…, "actor_names":["Jane Doe"]}
GET  /api/research/actors/<actor_id>                     → actor + timeline
GET  /api/research/audit/verify                          → chain intact
```

---

## What to do when you are unsure

- **Two people might be the same** → do not merge. File under the one you can
  source, and note the ambiguity in `notes`.
- **A date is approximate** → use the earliest date you can source and say so in
  `notes`. Never invent precision.
- **No URL for a claim** → do not file it. Come back when you have the document.
- **The claim is contested** → grade it `disputed` and file the counter-source
  as its own entry.

The archive's value is its discipline. An entry you were not sure about, filed
anyway, costs more than the entry was worth.
