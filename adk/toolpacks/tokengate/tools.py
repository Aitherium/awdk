"""TokenGate toolpack tools — a long document in, the answer plus the exact passages out.

SELF-CONTAINED BY CONTRACT. awdk is the public SDK: this module imports the
stdlib and ``adk.docconvert`` (its own package) and nothing from the AitherOS
monorepo. The lane is reached over plain HTTP so a stranger can point it at any
server that speaks the contract below.

THE LANE CONTRACT (see README.md):
  POST {base}/ask  {"document", "question", "max_kept_tokens"?, "max_new_tokens"?,
                    "evidence"?}
    -> 200 {"answer", "evidence": [{"text", "start", "end", "kept"}, ...],
            "kept_tokens", "state_tokens", "max_kept_tokens", "prefill_ms",
            "request_ms", "model", "checkpoint"}
    -> 413 {"detail": {"reason", "state_tokens", "max_state_tokens"}}   too long
    -> 503 {"detail": {"reason", "retry_after_s"?}}                      not loaded
  GET {base}/health -> 200 {"status", "loaded", "checkpoint", "lease", ...}

A 413 is NEVER worked around by truncating the document here: the gate's value is
that it reads ALL of it, and a silent trim would answer from the wrong text while
looking successful. The caller gets both counts and decides.

Design contract, same as every awdk tool pack:
  * Each tool returns a dict the agent can relay and NEVER raises. A dead lane, a
    file that cannot be read, a bad argument: a typed error dict, not a crash.
  * Every failure names HOW it failed (``error_kind``), so "the lane refused this
    document" never reads the same as "the lane is down".
  * ``AITHER_TOKENGATE_URL`` overrides the base (default http://127.0.0.1:8172).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

DEFAULT_URL = "http://127.0.0.1:8172"
URL_ENV = "AITHER_TOKENGATE_URL"
ASK_TIMEOUT_S = 300.0
HEALTH_TIMEOUT_S = 5.0
# A document the lane can hold is ~120k characters; anything this large is not a
# document and reading it into memory only to upload it is pure waste.
MAX_FILE_BYTES = 32 * 1024 * 1024
# Formats adk.docconvert (the `docs` extra: docling / pypdf) can turn into text.
DOCUMENT_SUFFIXES = frozenset({".pdf", ".docx", ".pptx", ".xlsx"})
_TEXT_SUFFIXES = frozenset({
    "", ".txt", ".text", ".md", ".markdown", ".rst", ".org", ".tex", ".log",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".csv", ".tsv", ".xml", ".html", ".htm", ".xhtml", ".css", ".scss", ".sass",
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    ".go", ".rs", ".java", ".kt", ".kts", ".scala", ".swift", ".m", ".mm", ".c",
    ".h", ".cc", ".cpp", ".cxx", ".hpp", ".cs", ".rb", ".php", ".pl", ".lua", ".r",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".psm1", ".bat", ".cmd", ".sql",
    ".graphql", ".proto", ".diff", ".patch", ".ipynb",
})

# Files whose whole purpose is to hold a credential. This tool's job is to ship the
# document OFF this process to AITHER_TOKENGATE_URL, so a glob-expanded `.env` must
# not leave the machine by accident. `text=` stays available for a deliberate send.
_SECRET_NAMES = frozenset({
    "credentials.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    ".netrc", ".pgpass", ".npmrc", ".pypirc", ".htpasswd",
})
_SECRET_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".ppk"})

_TOOL_NAMES = ["tokengate_ask", "tokengate_status"]


class _LaneError(Exception):
    """A typed refusal from (or on the way to) the lane; rendered, never raised out."""

    def __init__(self, kind: str, message: str, status: int = 0,
                 detail: Any = None, hint: str = ""):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.detail = detail
        self.hint = hint


def _base_url() -> str:
    """The lane's base URL. ``AITHER_TOKENGATE_URL`` wins; no other guessing."""
    env = (os.environ.get(URL_ENV) or "").strip().rstrip("/")
    return env or DEFAULT_URL


def _opener() -> urllib.request.OpenerDirector:
    """An opener that NEVER uses a proxy.

    The lane binds loopback (or the podman bridge) and speaks plain HTTP by
    contract. On Windows, ``urllib`` reads the WinINET proxy from the registry on
    every call and would send a 127.0.0.1 request through a corporate proxy that
    cannot reach it, so an explicit empty ProxyHandler is both correct and faster
    (it skips the registry probe).
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _request(method: str, url: str, *, body: Optional[dict] = None,
             timeout: float = 30.0) -> Tuple[int, Dict[str, Any], Any]:
    """One HTTP call. Returns ``(status, headers, payload)`` or raises _LaneError."""
    data = None
    headers = {"Accept": "application/json", "User-Agent": "awdk-tokengate/1.0"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener().open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return int(getattr(resp, "status", 200)), dict(resp.headers or {}), _json(raw)
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 — an unreadable error body is still an error
            raw = ""
        return int(exc.code), dict(exc.headers or {}), _json(raw)
    except (TimeoutError, OSError) as exc:
        # socket.timeout is a TimeoutError on 3.10+; a refused connection is OSError.
        if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
            raise _LaneError(
                "timeout", "%s: no answer after %ss" % (url, timeout), hint=(
                    "the lane holds one forward at a time; a long document can "
                    "queue behind another request. Retry, or check /health."))
        raise _LaneError(
            "unreachable", "%s: %s: %s" % (url, type(exc).__name__, exc), hint=(
                "is the TokenGate lane running? Default %s; override with %s."
                % (DEFAULT_URL, URL_ENV)))


def _json(raw: str) -> Any:
    try:
        return json.loads(raw or "null")
    except ValueError:
        return {"raw": raw[:2000]}


def _detail(payload: Any) -> Dict[str, Any]:
    """The lane's error detail as a dict, whichever envelope it used."""
    if isinstance(payload, dict):
        d = payload.get("detail", payload)
        if isinstance(d, dict):
            return d
        return {"reason": str(d)}
    return {"reason": str(payload)[:500]} if payload is not None else {}


def _refusal(status: int, headers: Dict[str, Any], payload: Any) -> _LaneError:
    """Map the lane's non-200 answer onto the typed taxonomy, counts preserved."""
    d = _detail(payload)
    reason = str(d.get("reason") or d.get("error") or "the lane refused the request")
    if status == 413:
        return _LaneError(
            "document_too_long", reason, status, d, hint=(
                "the document is over the lane's measured-safe token ceiling. "
                "Split it, or ask about a chapter at a time; do NOT assume the "
                "whole document was read."))
    if status == 503:
        retry = headers.get("Retry-After") or d.get("retry_after_s")
        if retry is not None:
            d = {**d, "retry_after_s": retry}
        return _LaneError(
            "lane_unavailable", reason, status, d, hint=(
                "the lane is not loaded or is waiting for a GPU lease. Retry "
                "after the delay it names (Retry-After), or check /health."))
    return _LaneError(
        "lane_refused", reason, status, d, hint=(
            "the lane answered %s. Check the request against the contract in the "
            "toolpack README." % status))


# --------------------------------------------------------------------------- #
# document input
# --------------------------------------------------------------------------- #
def _decode_text(data: bytes) -> Tuple[str, str]:
    """Bytes -> (text, encoding actually used). ``utf-8-sig`` strips a BOM."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise _LaneError(
        "unsupported_document",
        "the file does not decode as UTF-8 or cp1252; it looks binary",
        hint="convert it to text first, or pass `text=` directly.")


def _looks_like_a_secret(path: Path) -> bool:
    """True for a path whose name says "credential store" — see _SECRET_NAMES."""
    name = path.name.lower()
    return (name.startswith(".env") or name in _SECRET_NAMES
            or path.suffix.lower() in _SECRET_SUFFIXES)


def _read_document(raw_path: str) -> Tuple[str, Dict[str, Any]]:
    """Read *raw_path* as text. PDF/DOCX/PPTX/XLSX go through adk.docconvert.

    Returns ``(text, meta)`` or raises _LaneError with a visible kind.
    """
    path = Path(os.path.expanduser(raw_path))
    if not path.exists():
        raise _LaneError("file_not_found", "no such file: %s" % path)
    if not path.is_file():
        raise _LaneError("bad_input", "not a regular file: %s" % path)
    if _looks_like_a_secret(path):
        raise _LaneError(
            "secret_file_refused",
            "%s looks like a credential store; refusing to send it to the lane" % path,
            hint="this tool ships the file's bytes to AITHER_TOKENGATE_URL. If the "
                 "question genuinely needs this text, pass it as `text=` yourself.")
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise _LaneError(
            "document_too_large",
            "%s is %d bytes; this tool reads at most %d"
            % (path, size, MAX_FILE_BYTES),
            hint="the lane's own ceiling is ~120k characters — extract just the "
                 "relevant section.")
    suffix = path.suffix.lower()
    meta: Dict[str, Any] = {"document_path": str(path), "document_bytes": size}

    if suffix in DOCUMENT_SUFFIXES:
        try:
            from adk.docconvert import available_converters, convert_document
        except Exception as exc:  # noqa: BLE001 — an import problem is reported
            raise _LaneError(
                "extraction_unavailable",
                "document conversion is not importable: %s: %s" % (type(exc).__name__, exc))
        if not available_converters():
            raise _LaneError(
                "extraction_unavailable",
                "no %s extractor is installed" % suffix.lstrip("."),
                hint="install one: pip install 'awdk[docs]' (docling + pypdf), "
                     "or convert the file to text and pass `text=`.")
        try:
            text = convert_document(path)
        except Exception as exc:  # noqa: BLE001
            text = None
            meta["extraction_error"] = "%s: %s" % (type(exc).__name__, exc)
        if not text or not text.strip():
            raise _LaneError(
                "extraction_failed",
                "the %s extractor produced no text from %s"
                % (suffix.lstrip("."), path),
                detail=meta,
                hint="try docling (`pip install 'awdk[docs]'`) or convert the "
                     "file to text yourself and pass `text=`.")
        meta["document_encoding"] = "converted:%s" % suffix.lstrip(".")
        return text, meta

    data = path.read_bytes()
    text, encoding = _decode_text(data)
    meta["document_encoding"] = encoding
    if suffix not in _TEXT_SUFFIXES:
        # Unknown extension that decoded cleanly as text: keep it, and say so.
        meta["document_note"] = ("unknown extension %r read as text" % suffix)
    return text, meta


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
def tokengate_ask(question: str, path: Optional[str] = None, text: Optional[str] = None,
                  max_kept_tokens: Optional[int] = None) -> Dict[str, Any]:
    """Ask a question about a LONG document; get the answer plus the exact passages.

    Reads the WHOLE document through a gated model, so it works on input that
    would not fit a context window. Pass exactly one of ``path`` (a local file:
    txt/md/code, or pdf/docx/pptx/xlsx with the awdk[docs] extra) or ``text``
    (the document itself).

    Args:
        question: what to answer from the document.
        path: a local file to read as the document.
        text: the document text itself.
        max_kept_tokens: optional cap on tokens the gate keeps (lane default when None).
    """
    return _ask(question, path, text, max_kept_tokens)


def _ask(question: str, path: Optional[str] = None, text: Optional[str] = None,
         max_kept_tokens: Optional[int] = None) -> Dict[str, Any]:
    url = _base_url() + "/ask"
    try:
        if not isinstance(question, str) or not question.strip():
            raise _LaneError("bad_input", "`question` must be a non-empty string")
        has_path = isinstance(path, str) and bool(path.strip())
        has_text = isinstance(text, str) and bool(text.strip())
        if has_path and has_text:
            raise _LaneError(
                "bad_input", "pass exactly one of `path` or `text`, not both")
        if not has_path and not has_text:
            raise _LaneError(
                "bad_input", "pass the document as `path` (a local file) or `text`")
        meta: Dict[str, Any] = {}
        if has_path:
            document, meta = _read_document(path.strip())
            source = "path"
        else:
            document = text  # type: ignore[assignment] — has_text guarantees str
            source = "text"
        keep: Optional[int] = None
        if max_kept_tokens is not None:
            try:
                keep = int(max_kept_tokens)
            except (TypeError, ValueError):
                raise _LaneError(
                    "bad_input", "`max_kept_tokens` must be an integer")
            if keep < 1:
                raise _LaneError(
                    "bad_input", "`max_kept_tokens` must be >= 1 (got %d)" % keep)

        body: Dict[str, Any] = {"document": document, "question": question}
        if keep is not None:
            body["max_kept_tokens"] = keep
        status, headers, payload = _request("POST", url, body=body, timeout=ASK_TIMEOUT_S)
        if status != 200:
            raise _refusal(status, headers, payload)
        if not isinstance(payload, dict) or "answer" not in payload:
            raise _LaneError(
                "lane_refused", "the lane answered 200 without an answer",
                status, payload)
        out: Dict[str, Any] = {"ok": True, "url": url,
                               "document_source": source,
                               "document_chars": len(document)}
        out.update(meta)
        out.update(payload)
        return out
    except _LaneError as exc:
        return _error_dict(exc, url)


def tokengate_status() -> Dict[str, Any]:
    """Is the TokenGate document lane up, and has it loaded a model / a GPU lease."""
    url = _base_url() + "/health"
    try:
        status, headers, payload = _request("GET", url, timeout=HEALTH_TIMEOUT_S)
        if status != 200:
            raise _refusal(status, headers, payload)
        return {"ok": True, "reachable": True, "url": url, "health": payload}
    except _LaneError as exc:
        return _error_dict(exc, url)


def _error_dict(exc: _LaneError, url: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "ok": False,
        "error_kind": exc.kind,
        "error": exc.message,
        "url": url,
    }
    if exc.status:
        out["status"] = exc.status
    detail = exc.detail
    if isinstance(detail, dict):
        # Both counts on a 413 are the point of the typed error; never drop them.
        for key in ("state_tokens", "max_state_tokens", "retry_after_s", "reason"):
            if key in detail and key not in out:
                out[key] = detail[key]
        # Keep any other lane-provided fields visible without overwriting ours.
        for key, value in detail.items():
            out.setdefault(key, value)
    if exc.hint:
        out["hint"] = exc.hint
    return out
