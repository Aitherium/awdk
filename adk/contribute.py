"""Volunteer your harness logs, skills, tools and packs to make shared models better.

``adk contribute on|off|status|preview|delete|upload``. The rules this module keeps:

* **Consent is per category and defaults OFF.** Nothing is collected, staged or
  sent for a category until ``adk contribute on <category>``.
* **Redaction happens HERE, before anything leaves the machine.** Secrets,
  e-mails, phone numbers, IP addresses, internal hostnames, this machine's
  hostname and user name, home paths, tenant ids and every term in your
  denylist are replaced. Files that are secrets by name (``.env``, keys,
  certificates) and files your ``.gitignore`` excludes are never read.
* **An audit gates sending.** After redaction each item is re-checked: if the
  redactor would still change it, or awmine's credential-shape scan sees a
  surviving secret, the item is dropped. If the audit cannot run, nothing is
  sent.
* **``preview`` shows the exact payload** that ``upload`` would send.
* **Uploads fail closed** until contributor terms are published
  (:data:`TERMS_VERSION`) and an intake endpoint is configured. With every
  category off, ``upload`` returns before any network code is imported.

Stdlib only, except ``httpx`` inside the one function that sends.
"""

from __future__ import annotations

import copy
import fnmatch
import getpass
import hashlib
import json
import os
import re
import socket
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .log_redact import LOG_REDACTIONS

__all__ = [
    "CATEGORIES", "CONSENT_VERSION", "DEFAULT_LICENCE", "LICENCES", "LICENCE_SUMMARY",
    "TERMS_VERSION", "ContributeError",
    "UploadResult", "audit_obj", "audit_text", "build_batch", "contribute_home",
    "delete_all", "load_consent", "redact_obj", "redact_text", "set_consent",
    "upload",
]

#: What a contributor can volunteer, each with its own switch.
CATEGORIES: Tuple[str, ...] = ("harness_logs", "skills", "tools", "packs")
CONSENT_VERSION = 2
#: What a contributor lets us do with a category, chosen per category. The default
#: is the narrowest: train models, never share the raw data. Nothing is paid or
#: credited in return under any tier.
LICENCES: Tuple[str, ...] = ("train-only", "cc-by-4.0", "cc0-1.0")
DEFAULT_LICENCE = "train-only"
LICENCE_SUMMARY = {
    "train-only": "Aitherium may train models on it; the raw data is never shared",
    "cc-by-4.0": "also published in an open dataset, credited to you (CC BY 4.0)",
    "cc0-1.0": "public domain: anyone may use it, no credit needed (CC0 1.0)",
}
#: The contributor terms in force (docs/CONTRIBUTOR-TERMS.md). ``None`` would
#: refuse every upload (fail closed). Setting this is a release, never a runtime
#: flag a caller can flip; a new version makes every contributor re-accept with
#: ``adk contribute on`` before anything more is sent.
TERMS_VERSION: Optional[str] = "2026-10-24"
#: Bumped whenever the redaction vocabulary changes; recorded on every item so
#: intake can re-scrub rows produced by an older client.
SCRUB_VERSION = 1

MAX_FILE_BYTES = 256 * 1024
BATCH_ITEMS = 50

#: Files that are secrets by NAME. Never opened.
_SECRET_FILE_GLOBS = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*",
    "id_ed25519*", "id_ecdsa*", "credentials*.json", "*secret*", "*.kdbx",
    ".netrc", ".npmrc", ".pypirc", "*.sig.key", "credentials*", "*credential*",
    "config.json", "*.keystore", "*.jks", "*token*",
)
_TEXT_SUFFIXES = {".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".py"}


class ContributeError(Exception):
    """A contribution step refused; the message says why."""


# --------------------------------------------------------------------------- paths

def contribute_home() -> Path:
    """``$AITHER_CONTRIBUTE_HOME`` or ``~/.aither/contribute``."""
    env = os.environ.get("AITHER_CONTRIBUTE_HOME", "").strip()
    return Path(env) if env else Path.home() / ".aither" / "contribute"


def _awmine_out() -> Path:
    env = os.environ.get("AWMINE_OUT", "").strip()
    return Path(env) if env else Path.home() / ".aither" / "awmine"


def _consent_path() -> Path:
    return contribute_home() / "consent.json"


def _outbox() -> Path:
    return contribute_home() / "outbox"


# --------------------------------------------------------------------------- consent

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _default_consent() -> Dict[str, Any]:
    return {
        "consent_version": CONSENT_VERSION,
        "contributor_id": "",
        "categories": {c: False for c in CATEGORIES},
        "licences": {c: DEFAULT_LICENCE for c in CATEGORIES},
        "credit_name": "",
        "terms_version": None,
        "updated_at": None,
        "history": [],
    }


def load_consent() -> Dict[str, Any]:
    """The local consent record. Missing or unreadable = everything OFF."""
    rec = _default_consent()
    p = _consent_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return rec
    if not isinstance(raw, dict):
        return rec
    cats = raw.get("categories") if isinstance(raw.get("categories"), dict) else {}
    # Only an explicit JSON true turns a category on; anything else is OFF.
    rec["categories"] = {c: cats.get(c) is True for c in CATEGORIES}
    lic = raw.get("licences") if isinstance(raw.get("licences"), dict) else {}
    # An unknown or missing licence falls back to the narrowest tier, never a wider one.
    rec["licences"] = {c: lic.get(c) if lic.get(c) in LICENCES else DEFAULT_LICENCE
                       for c in CATEGORIES}
    credit = raw.get("credit_name")
    rec["credit_name"] = credit.strip()[:80] if isinstance(credit, str) else ""
    for k in ("contributor_id", "terms_version", "updated_at"):
        if k in raw:
            rec[k] = raw[k]
    if isinstance(raw.get("history"), list):
        rec["history"] = raw["history"][-200:]
    return rec


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _resolve_categories(names: Iterable[str]) -> List[str]:
    names = [n.strip().lower().replace("-", "_") for n in names if n and n.strip()]
    if not names or names == ["all"]:
        return list(CATEGORIES)
    bad = [n for n in names if n not in CATEGORIES]
    if bad:
        raise ContributeError(
            f"unknown category {', '.join(bad)} (choose from: {', '.join(CATEGORIES)})")
    return names


def _resolve_licence(licence: Optional[str]) -> Optional[str]:
    if licence is None:
        return None
    lic = licence.strip().lower()
    aliases = {"train": "train-only", "private": "train-only", "cc-by": "cc-by-4.0",
               "ccby": "cc-by-4.0", "open": "cc-by-4.0", "cc0": "cc0-1.0",
               "public-domain": "cc0-1.0"}
    lic = aliases.get(lic, lic)
    if lic not in LICENCES:
        raise ContributeError(f"unknown licence {licence!r} (choose from: {', '.join(LICENCES)})")
    return lic


def set_consent(names: Iterable[str], on: bool, licence: Optional[str] = None,
                credit_name: Optional[str] = None) -> Dict[str, Any]:
    """Turn categories on or off (no names = all), optionally choosing their licence
    tier. Records version + timestamp."""
    cats = _resolve_categories(names)
    lic = _resolve_licence(licence)
    rec = load_consent()
    if on and not rec.get("contributor_id"):
        # A random pseudonym, never derived from the user, host or account.
        rec["contributor_id"] = "ctb_" + uuid.uuid4().hex
    for c in cats:
        rec["categories"][c] = bool(on)
        if lic:
            rec["licences"][c] = lic
    if credit_name is not None:
        rec["credit_name"] = credit_name.strip()[:80]
    if (lic or not on) and _outbox().is_dir():
        # A batch staged under another licence or consent must not go out as is.
        for f in _outbox().glob("batch-*.json"):
            f.unlink()
    rec["consent_version"] = CONSENT_VERSION
    rec["terms_version"] = TERMS_VERSION
    rec["updated_at"] = _now()
    rec["history"].append({"ts": rec["updated_at"], "action": "on" if on else "off",
                           "categories": cats, "licence": lic,
                           "consent_version": CONSENT_VERSION,
                           "terms_version": TERMS_VERSION})
    _write_json(_consent_path(), rec)
    return rec


def delete_all() -> Dict[str, Any]:
    """Withdraw: every category OFF, staged batches removed, deletion request queued.

    The deletion request names only the pseudonymous contributor id; intake
    turns it into corpus tombstones. It is sent by the next ``upload`` that is
    allowed to reach the network, and kept until then.
    """
    rec = load_consent()
    cid = rec.get("contributor_id") or ""
    removed = 0
    ob = _outbox()
    if ob.is_dir():
        for f in ob.glob("batch-*.json"):
            f.unlink()
            removed += 1
    if cid:
        _write_json(ob / "deletion.json", {"contributor_id": cid, "requested_at": _now(),
                                           "consent_version": CONSENT_VERSION})
    for c in CATEGORIES:
        rec["categories"][c] = False
    # The server tombstones this id; a later opt-in starts under a fresh one.
    rec["contributor_id"] = ""
    rec["updated_at"] = _now()
    rec["history"].append({"ts": rec["updated_at"], "action": "delete",
                           "categories": list(CATEGORIES)})
    _write_json(_consent_path(), rec)
    return {"contributor_id": cid, "staged_removed": removed,
            "deletion_queued": bool(cid)}


# --------------------------------------------------------------------------- redaction

def _placeholder(kind: str) -> str:
    return f"[REDACTED:{kind}]"


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24}")
_PHONE = re.compile(
    r"(?<![\w.])(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}(?![\w.])")
_IPV4 = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}"
                   r"(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")
#: Needs a hex LETTER or ``::`` so a clock time (``12:30:45``) is not an address.
_IPV6 = re.compile(r"(?i)(?<![0-9a-f:])(?=[0-9a-f:]*(?:[a-f]|::))"
                   r"(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{1,4}(?![0-9a-f:])")
#: A JSON key that names a credential: its whole value goes, whatever it looks like.
_SECRET_KEY = re.compile(r"(?i)(?:password|passwd|passphrase|secret|token|api[_-]?key|"
                         r"access[_-]?key|private[_-]?key|session[_-]?key|credential|"
                         r"authorization|cookie|bearer|^auth$|^pwd$)")
#: A JSON key that names personal data: its value goes too, even as a bare number.
_PII_KEY = re.compile(r"(?i)(?:phone|mobile|^tel$|telephone|^fax$|ssn|birth|^dob$|"
                      r"street|postal|zip[_-]?code|e-?mail)")
#: Path segments that hold secrets whatever the file inside is called.
_SECRET_DIRS = {".git", ".ssh", ".aws", ".gnupg", ".aither", ".azure", ".kube", ".docker",
                "secrets", "secret", "credentials", "private"}
#: Names that only resolve inside someone's network. Label count and length are
#: BOUNDED: an unbounded ``(?:\.label)*`` before a failing suffix is quadratic
#: (measured: 64 KB of ``a.a.a.`` took 125 s).
_INTERNAL_HOST = re.compile(
    r"(?i)(?:[a-z0-9-]{1,63}\.){1,12}"
    r"(?:local|lan|internal|intranet|corp|home|localdomain|ts\.net)\b")
_TENANT = re.compile(r"\b(?:tnt|tenant|ws|org)_[A-Za-z0-9][A-Za-z0-9_-]{2,}")
_HOME = [
    re.compile(r"(?i)[A-Z]:[\\/]Users[\\/][^\\/\s\"'<>|]+"),
    re.compile(r"/home/[^/\s\"'<>|]+"),
    re.compile(r"/Users/[^/\s\"'<>|]+"),
    re.compile(r"/root(?=/|\b)"),
]
_PRIVKEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S)


def _awmine_redact():
    try:
        from awmine import redact as mod  # type: ignore[import-not-found]
    except ImportError:
        return None
    return mod


def _local_terms() -> List[str]:
    """This machine's own identifiers: they are PII of the contributor."""
    terms = []
    for fn in (getpass.getuser, socket.gethostname):
        try:
            v = (fn() or "").strip()
        except Exception:  # noqa: BLE001 - an unknown user/host just adds no term
            v = ""
        if len(v) >= 3:
            terms.append(v)
            if "." in v:
                terms.append(v.split(".", 1)[0])
    return terms


def load_denylist() -> List[str]:
    """Customer names, project codenames: ``<contribute home>/denylist.txt``, one per line."""
    terms: List[str] = []
    f = contribute_home() / "denylist.txt"
    if f.is_file():
        # Fail closed: a denylist that exists but cannot be read raises.
        terms.extend(f.read_text(encoding="utf-8").splitlines())
    env = os.environ.get("AITHER_CONTRIBUTE_DENY", "")
    terms.extend(re.split(r"[;,]", env))
    terms.extend(_local_terms())
    seen: Dict[str, None] = {}
    for t in terms:
        t = t.strip()
        if t and not t.startswith("#") and len(t) >= 3:
            seen[t.lower()] = None
    return sorted(seen, key=len, reverse=True)


def _deny_pattern(term: str) -> "re.Pattern[str]":
    head = r"(?<![A-Za-z0-9])" if term[0].isalnum() else ""
    tail = r"(?![A-Za-z0-9])" if term[-1].isalnum() else ""
    return re.compile(head + re.escape(term) + tail, re.I)


#: Redaction runs per chunk of at most this many characters. Several patterns
#: (ours and the vendored secret vocabulary) are quadratic in the length of one
#: word-character run; chunking bounds the cost to O(n). A single unbroken run
#: this long is not useful text and may be an encoded secret: it is dropped whole.
CHUNK_CHARS = 4096
_WS_SPLIT = re.compile(r"(\s+)")


def redact_text(text: str, deny: Optional[List[str]] = None) -> Tuple[str, Dict[str, int]]:
    """Redact one string. Returns ``(text, hits_by_kind)``."""
    hits: Dict[str, int] = {}
    if not isinstance(text, str) or not text:
        return text, hits
    deny = load_denylist() if deny is None else deny
    if len(text) <= CHUNK_CHARS:
        return _redact_chunk(text, deny)
    text, n = _PRIVKEY.subn(_placeholder("privkey"), text)
    if n:
        hits["privkey"] = n
    chunks: List[str] = []
    cur: List[str] = []
    size = 0
    for tok in _WS_SPLIT.split(text):
        if len(tok) >= CHUNK_CHARS and not tok.isspace():
            tok = _placeholder("blob")
            hits["blob"] = hits.get("blob", 0) + 1
        if size + len(tok) > CHUNK_CHARS and cur:
            chunks.append("".join(cur))
            cur, size = [], 0
        cur.append(tok)
        size += len(tok)
    if cur:
        chunks.append("".join(cur))
    out = []
    for c in chunks:
        r, h = _redact_chunk(c, deny)
        out.append(r)
        for k, v in h.items():
            hits[k] = hits.get(k, 0) + v
    text = "".join(out)
    # A denylisted multi-word name can straddle a chunk boundary; the literal
    # patterns are linear, so run them once more over the whole text.
    for term in deny:
        text, n = _deny_pattern(term).subn(_placeholder("name"), text)
        if n:
            hits["name"] = hits.get("name", 0) + n
    return text, hits


def _redact_chunk(text: str, deny: List[str]) -> Tuple[str, Dict[str, int]]:
    hits: Dict[str, int] = {}

    def _sub(kind: str, pat: "re.Pattern[str]", repl: Optional[str] = None) -> None:
        nonlocal text
        text, n = pat.subn(repl if repl is not None else _placeholder(kind), text)
        if n:
            hits[kind] = hits.get(kind, 0) + n

    _sub("privkey", _PRIVKEY)
    mod = _awmine_redact()
    if mod is not None:
        text, h = mod.redact_text(text, None)
        for k, v in h.items():
            hits[k] = hits.get(k, 0) + v
    for pat, repl in LOG_REDACTIONS:
        text, n = pat.subn(repl, text)
        if n:
            hits["secret"] = hits.get("secret", 0) + n
    for pat in _HOME:
        _sub("home", pat, "<HOME>")
    _sub("email", _EMAIL)
    _sub("host", _INTERNAL_HOST)
    _sub("ip", _IPV4)
    _sub("ip", _IPV6)
    _sub("phone", _PHONE)
    _sub("tenant", _TENANT)
    for term in deny:
        _sub("name", _deny_pattern(term))
    return text, hits


def redact_obj(obj: Any, deny: Optional[List[str]] = None) -> Tuple[Any, Dict[str, int]]:
    """Redact every string (values AND keys) in a JSON-shaped object."""
    deny = load_denylist() if deny is None else deny
    hits: Dict[str, int] = {}

    def _merge(h: Dict[str, int]) -> None:
        for k, v in h.items():
            hits[k] = hits.get(k, 0) + v

    def _walk(o: Any) -> Any:
        if isinstance(o, str):
            out, h = redact_text(o, deny)
            _merge(h)
            return out
        if isinstance(o, (int, float)) and not isinstance(o, bool):
            # A phone number or PIN stored as a number is still PII.
            out, h = redact_text(str(o), deny)
            _merge(h)
            return out if h else o
        if isinstance(o, dict):
            res = {}
            for k, v in o.items():
                # Whatever the value's type: a credential-named key loses it.
                if (isinstance(k, str) and _SECRET_KEY.search(k)
                        and v not in (None, "", [], {}) and not isinstance(v, bool)):
                    v = _placeholder("kv")
                    _merge({"kv": 1})
                elif (isinstance(k, str) and _PII_KEY.search(k)
                      and isinstance(v, (int, float, str)) and not isinstance(v, bool)
                      and str(v).strip() and not str(v).startswith("[REDACTED:")):
                    v = _placeholder("pii")
                    _merge({"pii": 1})
                if isinstance(k, str):
                    k, h = redact_text(k, deny)
                    _merge(h)
                res[k] = _walk(v)
            return res
        if isinstance(o, list):
            return [_walk(v) for v in o]
        return o

    return _walk(copy.deepcopy(obj)), hits


@dataclass
class Audit:
    judged: bool
    clean: bool
    findings: List[str] = field(default_factory=list)


def audit_text(text: str, deny: Optional[List[str]] = None) -> Audit:
    """Is already-redacted ``text`` safe to send?

    Two independent questions: would the redactor still change it (self-
    consistency), and does awmine's vocabulary-free scan see a credential-shaped
    assignment (names only -- a finding never quotes the value). Without awmine
    the second question cannot be asked: ``judged`` is False and the caller
    must not send.
    """
    again, hits = redact_text(text, deny)
    findings = [f"residual:{k}" for k in sorted(hits)] if again != text else []
    mod = _awmine_redact()
    if mod is None:
        return Audit(judged=False, clean=False, findings=findings + ["audit-unavailable"])
    findings += [f"suspect:{n}" for n in mod.suspect_hits(text)]
    return Audit(judged=True, clean=not findings, findings=findings)


def audit_obj(obj: Any, deny: Optional[List[str]] = None) -> Audit:
    """:func:`audit_text` over every string (values and keys) of a redacted object."""
    deny = load_denylist() if deny is None else deny
    judged, findings = True, []

    def _strings(o: Any) -> Iterable[str]:
        if isinstance(o, str):
            yield o
        elif isinstance(o, (int, float)) and not isinstance(o, bool):
            yield str(o)
        elif isinstance(o, dict):
            for k, v in o.items():
                if isinstance(k, str):
                    yield k
                    if (_SECRET_KEY.search(k) and v not in (None, "", [], {})
                            and v != _placeholder("kv") and not isinstance(v, bool)):
                        findings.append(f"credential-key:{k[:40]}")
                yield from _strings(v)
        elif isinstance(o, list):
            for v in o:
                yield from _strings(v)

    for s in _strings(obj):
        a = audit_text(s, deny)
        judged = judged and a.judged
        findings += [f for f in a.findings if f not in findings]
    return Audit(judged=judged, clean=judged and not findings, findings=findings)


# --------------------------------------------------------------------------- collection

def _gitignore_patterns(root: Path) -> List[str]:
    pats: List[str] = []
    for gi in [root / ".gitignore", *root.rglob(".gitignore")]:
        try:
            lines = gi.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for ln in lines:
            ln = ln.strip()
            if ln and not ln.startswith("#") and not ln.startswith("!"):
                pats.append(ln.rstrip("/"))
    return pats


def _git_ignored(root: Path, rels: List[str]) -> Optional[set]:
    """Paths under ``root`` that git ignores (every .gitignore up the tree and
    ``.git/info/exclude``). ``None`` when ``root`` is not in a work tree or git
    is missing; the in-folder parser still applies then."""
    if not rels:
        return set()
    import subprocess

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(["git", "-C", str(root), "check-ignore", "--stdin", "-z"],
                           input="\0".join(rels) + "\0", capture_output=True,
                           encoding="utf-8", timeout=30, creationflags=flags)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode not in (0, 1):  # 128 = not a git work tree
        return None
    return {p for p in r.stdout.split("\0") if p}


def _skip_reason(path: Path, root: Path, ignore: List[str]) -> Optional[str]:
    if path.is_symlink():
        return "symlink"
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return "outside-root"
    name = path.name.lower()
    if any(fnmatch.fnmatch(name, g) for g in _SECRET_FILE_GLOBS):
        return "secret-file"
    rel = path.relative_to(root).as_posix()
    parts = rel.split("/")
    if any(seg.lower() in _SECRET_DIRS for seg in parts[:-1]):
        return "secret-dir"
    for pat in ignore:
        p = pat.lstrip("/")
        if fnmatch.fnmatch(rel, p) or any(fnmatch.fnmatch(seg, p) for seg in parts):
            return "gitignored"
    if path.suffix.lower() not in _TEXT_SUFFIXES:
        return "not-text"
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return "too-large"
    except OSError:
        return "unreadable"
    return None


def _read_tree(root: Path) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
    files: Dict[str, str] = {}
    dropped: List[Dict[str, str]] = []
    ignore = _gitignore_patterns(root)
    if root.is_symlink():
        return files, [{"file": ".", "reason": "symlink"}]
    paths = [p for p in sorted(root.rglob("*"))
             if (p.is_file() or p.is_symlink()) and p.name != ".gitignore"]
    git_ign = _git_ignored(root, [p.relative_to(root).as_posix() for p in paths]) or set()
    for p in paths:
        rel = p.relative_to(root).as_posix()
        why = _skip_reason(p, root, ignore) or ("gitignored" if rel in git_ign else None)
        if why:
            dropped.append({"file": rel, "reason": why})
            continue
        try:
            files[rel] = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            dropped.append({"file": rel, "reason": "unreadable"})
    return files, dropped


def _jsonl(path: Path, kind: str, limit: int) -> Iterable[Dict[str, Any]]:
    if not path.is_file():
        return
    n = 0
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            yield {"kind": kind, "source": path.name, "payload": row}
            n += 1
            if n >= limit:
                return


def _raw_items(category: str, limit: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    out = _awmine_out()
    home = contribute_home()
    items: List[Dict[str, Any]] = []
    dropped: List[Dict[str, str]] = []
    if category == "harness_logs":
        for name, kind in (("outcomes.jsonl", "outcome"), ("lessons.jsonl", "lesson"),
                           ("turns.jsonl", "turn"), ("procedures.jsonl", "procedure"),
                           ("exports/harvest.jsonl", "harvest")):
            items.extend(_jsonl(out / name, kind, limit))
    elif category in ("skills", "packs"):
        roots: List[Path] = []
        if category == "skills":
            roots += [d for d in sorted((out / "exports" / "skills").glob("*")) if d.is_dir()]
        roots += [d for d in sorted((home / category).glob("*")) if d.is_dir()]
        for d in roots:
            files, drops = _read_tree(d)
            dropped += [{"source": d.name, **x} for x in drops]
            if not files:
                continue
            payload: Dict[str, Any] = {"name": d.name, "files": files}
            if category == "packs":
                # Intake only accepts packs that carry a signature; the client
                # reports it, it does not judge it.
                payload["signed"] = (d / "pack.sig").is_file()
                if payload["signed"]:
                    payload["signature"] = (d / "pack.sig").read_text(
                        encoding="utf-8", errors="replace").strip()
            items.append({"kind": category[:-1], "source": d.name, "payload": payload})
    elif category == "tools":
        f = out / "exports" / "toolpack_candidates.json"
        if f.is_file():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                data = None
            rows = data if isinstance(data, list) else (data or {}).get("candidates", [])
            for row in rows if isinstance(rows, list) else []:
                items.append({"kind": "tool", "source": f.name, "payload": row})
    return items[:limit], dropped


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode("utf-8")).hexdigest()


def build_batch(categories: Optional[Iterable[str]] = None, *, limit: int = 200,
                respect_consent: bool = True) -> Dict[str, Any]:
    """Collect, redact and audit. The returned ``items`` are EXACTLY what would be sent.

    ``respect_consent=False`` is for ``preview --category X`` before a user opts
    in: it reads and redacts locally and never sends.
    """
    consent = load_consent()
    wanted = _resolve_categories(categories or [])
    if respect_consent:
        wanted = [c for c in wanted if consent["categories"].get(c)]
    deny = load_denylist()
    items: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    audit_judged = True
    for cat in wanted:
        raw, drops = _raw_items(cat, limit)
        dropped += [{"category": cat, **d} for d in drops]
        for it in raw:
            red, hits = redact_obj(it, deny)
            a = audit_obj(red, deny)
            audit_judged = audit_judged and a.judged
            if a.judged and not a.clean:
                dropped.append({"category": cat, "source": red.get("source", ""),
                                "reason": "audit", "findings": a.findings})
                continue
            items.append({"category": cat, "kind": red["kind"], "source": red["source"],
                          "payload": red["payload"], "redactions": hits,
                          "licence": consent["licences"][cat],
                          "scrub_version": SCRUB_VERSION, "digest": _digest(red["payload"])})
    return {"categories": wanted, "items": items, "dropped": dropped,
            "audit": {"judged": audit_judged,
                      "engine": "awmine" if _awmine_redact() else "unavailable"},
            "contributor_id": consent.get("contributor_id") or "",
            "licences": {c: consent["licences"][c] for c in wanted},
            "consent_version": CONSENT_VERSION, "terms_version": TERMS_VERSION}


# --------------------------------------------------------------------------- upload

@dataclass
class UploadResult:
    status: str  # consent_off | refused | sent | partial
    reason: str = ""
    sent: int = 0
    batches: int = 0


#: Where uploads go unless $AITHER_CONTRIBUTE_URL points elsewhere (a self-hosted intake).
DEFAULT_ENDPOINT = "https://contribute.aitherium.com"


def _endpoint() -> str:
    return (os.environ.get("AITHER_CONTRIBUTE_URL", "").strip() or DEFAULT_ENDPOINT).rstrip("/")


def _token() -> Optional[str]:
    tok = os.environ.get("AITHER_AUTH_TOKEN", "").strip()
    if tok:
        return tok
    try:
        cfg = json.loads((Path.home() / ".aither" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return (cfg.get("auth_token") or "").strip() or None


def _sent_ledger() -> Path:
    return contribute_home() / "sent.jsonl"


def _already_sent() -> set:
    p = _sent_ledger()
    if not p.is_file():
        return set()
    return {ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()}


def upload() -> UploadResult:
    """Send consented, redacted, audited items. Fails closed at every gate.

    Gate order matters: consent is checked FIRST and returns before any
    network module is imported, so a user with every category off cannot
    cause a single packet (``test_consent_off_sends_nothing`` asserts this).
    """
    consent = load_consent()
    pending_delete = (_outbox() / "deletion.json").is_file()
    if not any(consent["categories"].values()) and not pending_delete:
        return UploadResult("consent_off", "every category is off")
    if TERMS_VERSION is None:
        return UploadResult("refused", "contributor terms are not published yet; "
                                       "nothing is sent until they are")
    if consent.get("terms_version") != TERMS_VERSION:
        return UploadResult("refused", f"run `adk contribute on` to accept terms "
                                       f"{TERMS_VERSION}")
    url = _endpoint()
    if not url.startswith("https://"):
        return UploadResult("refused", "no https intake endpoint configured "
                                       "(AITHER_CONTRIBUTE_URL)")
    tok = _token()
    if not tok:
        return UploadResult("refused", "not signed in (adk login)")
    batch = build_batch()
    if not batch["audit"]["judged"]:
        return UploadResult("refused", "redaction audit unavailable: pip install awmine")
    done = _already_sent()
    todo = [it for it in batch["items"] if it["digest"] not in done]
    return _send(url, tok, consent, todo, pending_delete)


def _send(url: str, tok: str, consent: Dict[str, Any], todo: List[Dict[str, Any]],
          pending_delete: bool) -> UploadResult:
    import httpx  # only here: nothing above this line touches the network

    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    ob = _outbox()
    ob.mkdir(parents=True, exist_ok=True)
    meta = {"contributor_id": consent["contributor_id"], "consent_version": CONSENT_VERSION,
            "terms_version": TERMS_VERSION, "scrub_version": SCRUB_VERSION}
    with httpx.Client(timeout=60.0) as client:
        if any(consent["categories"].values()):
            # The server keeps its own consent record and refuses items in a
            # category it has no record for; send the current one every time.
            r = client.post(f"{url}/v1/contribute/consent", headers=headers,
                            json={**meta, "categories": consent["categories"],
                                  "licences": consent["licences"],
                                  "credit_name": consent.get("credit_name") or ""})
            if r.status_code >= 300:
                return UploadResult("refused", f"consent record refused: HTTP {r.status_code}")
        if pending_delete:
            body = json.loads((ob / "deletion.json").read_text(encoding="utf-8"))
            r = client.post(f"{url}/v1/contribute/delete", json=body, headers=headers)
            if r.status_code >= 300:
                return UploadResult("refused", f"deletion request failed: HTTP {r.status_code}")
            (ob / "deletion.json").unlink()
        # Batches staged by an earlier run were built under the consent and denylist
        # of THAT run; a category turned off since must not go out. Drop them and
        # re-stage from the fresh build -- the sent-ledger is what makes a rerun
        # resume, so nothing already delivered is sent twice.
        for f in ob.glob("batch-*.json"):
            f.unlink()
        # Stage every batch to disk first: an interrupted send leaves them for review.
        for i in range(0, len(todo), BATCH_ITEMS):
            chunk = todo[i:i + BATCH_ITEMS]
            bid = _digest([it["digest"] for it in chunk])[:32]
            _write_json(ob / f"batch-{bid}.json", {"batch_id": bid, **meta, "items": chunk})
        sent = batches = 0
        for f in sorted(ob.glob("batch-*.json")):
            body = json.loads(f.read_text(encoding="utf-8"))
            r = client.post(f"{url}/v1/contribute/batches", json=body,
                            headers={**headers, "Idempotency-Key": body["batch_id"]})
            if r.status_code >= 300:
                return UploadResult("partial", f"HTTP {r.status_code} on {f.name}; "
                                               "rerun to resume", sent, batches)
            with _sent_ledger().open("a", encoding="utf-8") as fh:
                for it in body["items"]:
                    fh.write(it["digest"] + "\n")
            f.unlink()
            sent += len(body["items"])
            batches += 1
    return UploadResult("sent", "", sent, batches)
