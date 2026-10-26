# TokenGate toolpack

Ask a question about a document that is too long for any context window, and get
back a short answer plus **the exact passages it came from**.

The lane does not summarise or chunk-and-retrieve: a gate at an early model layer
scores every document token, keeps the top-K, and the layers above read only
those. So the answer costs a few hundred tokens of model context while the whole
document was genuinely read.

Registered tools:

| tool | what it does |
|---|---|
| `tokengate_ask(question, path=None, text=None, max_kept_tokens=None)` | send a document and a question; returns the answer, ranked evidence with character offsets, and the measured budget |
| `tokengate_status()` | is the lane up, loaded, and holding a GPU lease |

Both return a dict and never raise. Failures are typed by `error_kind` (see below).

## Using it from an agent

`tokengate_ask` takes the document **either** as a local file or as text — pass
exactly one:

* `path="docs/handbook.md"` — read as text. Plain text, Markdown and source code
  are decoded directly (UTF-8, then cp1252; the encoding used is reported as
  `document_encoding`). PDF, DOCX, PPTX and XLSX go through `adk.docconvert`,
  which needs the `docs` extra: `pip install 'awdk[docs]'`. Without an extractor
  the tool returns `extraction_unavailable`, never an empty document. A file
  that looks like a credential store (`.env*`, `*.pem`, `*.key`, `id_rsa`,
  `credentials.json`, …) is refused with `secret_file_refused`: this tool's job
  is to ship the bytes to the lane, and a glob-expanded `.env` must not leave
  the machine by accident. Pass `text=` if you truly mean to send it.
* `text="..."` — the document itself, when the caller already holds it.
* `max_kept_tokens` — optional cap on the tokens the gate keeps. Omit to use the
  lane's default.

A successful result is the lane's answer merged with what the client knows:

```json
{
  "ok": true,
  "answer": "Retention is 30 days for deleted projects.",
  "evidence": [
    {"text": "Deleted projects are retained for 30 days.", "start": 1204, "end": 1252, "kept": 11}
  ],
  "kept_tokens": 812,
  "state_tokens": 41233,
  "max_kept_tokens": 2000,
  "prefill_ms": 182.4,
  "request_ms": 940.1,
  "model": "Qwen3-1.7B",
  "checkpoint": "liana313/tokengate-niah-qwen3-1.7b",
  "url": "http://127.0.0.1:8172/ask",
  "document_source": "path",
  "document_path": "docs/handbook.md",
  "document_chars": 118002,
  "document_encoding": "utf-8-sig"
}
```

`evidence[].start`/`end` are character offsets into **the document that was
sent** — the decoded file content or the exact `text` — so they stay valid for
the caller's own copy. `document_chars` says how much text that was.

### Typed errors

| `error_kind` | meaning | fields |
|---|---|---|
| `document_too_long` | HTTP 413 — over the lane's token ceiling | `state_tokens`, `max_state_tokens`, `reason` |
| `lane_unavailable` | HTTP 503 — model not loaded, or waiting for a GPU lease | `retry_after_s`, `reason` |
| `lane_refused` | any other non-200 | `status`, `reason` |
| `unreachable` | no process at the URL (connection refused/DNS) | `url`, `error` |
| `timeout` | the lane accepted the request but did not answer in time | `url`, `error` |
| `file_not_found` / `bad_input` / `unsupported_document` / `document_too_large` / `secret_file_refused` | the input is wrong, before any request | `error`, `hint` |
| `extraction_unavailable` / `extraction_failed` | a PDF/DOCX/… could not be turned into text | `error`, `hint` |

A 413 carries **both** counts on purpose, and the client never truncates the
document to squeeze under the ceiling: a silent trim would answer from the wrong
text and look successful. Split the document, or ask about one section at a time.

## Configuration

| env | default | meaning |
|---|---|---|
| `AITHER_TOKENGATE_URL` | `http://127.0.0.1:8172` | lane base URL; a trailing `/` is fine |

The client never uses a proxy (`urllib` with an empty `ProxyHandler`): the lane is
loopback by contract, and on Windows `urllib` would otherwise read the WinINET
proxy from the registry and send 127.0.0.1 through it.

## The wire contract

Any server that speaks this works; the toolpack depends on nothing else.

```
POST {base}/ask
  {"document": "<whole document>", "question": "<what to answer>",
   "max_kept_tokens": 2000,        // optional
   "max_new_tokens": 128,          // optional (lane default)
   "evidence": 5}                  // optional: how many passages to rank

200 -> {"answer": "...",
        "evidence": [{"text": "...", "start": 0, "end": 0, "kept": 0}, ...],
        "kept_tokens": 0, "state_tokens": 0, "max_kept_tokens": 0,
        "prefill_ms": 0.0, "request_ms": 0.0, "model": "...", "checkpoint": "..."}

413 -> {"detail": {"reason": "...", "state_tokens": 0, "max_state_tokens": 0}}
503 -> {"detail": {"reason": "...", "retry_after_s": 0}}   // Retry-After header may also be set

GET {base}/health
200 -> {"status": "ok", "loaded": true, "checkpoint": "...", "lease": {...}, ...}
```

`evidence` is the document sentences the gate kept, ranked, with character
offsets into the `document` that was sent.

## Self-hosting the lane

The gate model and its inference code come from the **upstream TokenGate project,
Apache-2.0**. The checkpoint the reference lane was measured on is
`liana313/tokengate-niah-qwen3-1.7b` (a Qwen3-1.7B base with the gate added). To
run it yourself:

1. Follow the upstream TokenGate README to install the gate and load the
   checkpoint (a single CUDA GPU is enough; the reference measurement used an
   RTX 5090).
2. Put a small HTTP wrapper in front of it that implements the two routes above.
   The two non-obvious pieces are:
   * **Chapter the evidence honestly.** The gate returns kept-token positions;
     map them back to character spans in the document and attach the sentence or
     list item that contains them. Do not synthesise quotes.
   * **Refuse over-ceiling documents with 413.** The reference lane's ceiling is
     32,768 state tokens (~120k characters). This is not politeness: an
     over-ceiling request was measured taking the whole machine down. Report
     `state_tokens` and `max_state_tokens` in the detail.
3. Serve it on loopback (or the podman bridge) in plain HTTP. The lane holds no
   credentials; keep it that way and reach it from untrusted networks only
   through a tunnel you control.
4. Point the tools at it: `export AITHER_TOKENGATE_URL=http://127.0.0.1:8172`.

On a fleet with a GPU lease door, loading the weights without a lease competes
with every other model on the card. The reference implementation leases the
footprint through MicroScheduler, heartbeats while loaded, and unloads when idle;
if you share a GPU, do the same before you load 8 GB of weights.

Documents are sent to whatever `AITHER_TOKENGATE_URL` names. Keep it loopback
unless you intend the text to leave the machine.

## Tests

`awdk/tests/test_toolpack_tokengate.py` runs the client against a real local
HTTP server: a 200 answer, a 413 with both counts, a 503 with `Retry-After`, an
unreachable lane, path and text inputs, and the input-validation errors.
