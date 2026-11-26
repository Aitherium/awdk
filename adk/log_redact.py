"""Scrub credentials out of log text before it leaves this machine.

Delegates canonical secret detection to ``awreport.redact.Redactor`` (a hard
dependency of ``awdk``) so log tails, focus records, and appliance logs inherit
every prefix-anchored pattern — including truncated AWS/GitHub/Anthropic/OpenAI
keys, Slack/Stripe/Google tokens, PEM private key blocks, and high-entropy
secrets — instead of maintaining a drifted regex table.

Adds ADK-specific log rules (``#k=`` lockbox/UI URL fragments, ``aither_sk_``
tokens, fine-grained ``github_pat_`` tokens, credentials embedded in URLs,
JWTs, case-insensitive ``bearer`` headers, and bounded generic key/value
secrets) while preserving the public exports ``LOG_REDACTIONS``,
``redact_log_line``, and ``redact_text``.
"""

from __future__ import annotations

import re
from typing import List, Pattern, Tuple

from awreport.redact import Redactor

__all__ = ["LOG_REDACTIONS", "redact_log_line", "redact_text"]

_REDACTOR = Redactor()

#: ADK-specific rules not yet covered by ``awreport.redact.Redactor.PATTERNS``.
#: Applied before the generic ``sk-`` / ``password`` patterns so ``aither_sk_``,
#: ``#k=`` fragments, URL user:pass@, and case-insensitive Bearer tokens produce
#: clean, unambiguous placeholders.
_ADK_EXTRA_REDACTIONS: List[Tuple[Pattern[str], str]] = [
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=\-]+"), "Bearer [REDACTED]"),
    (re.compile(r"#k=[A-Za-z0-9._\-%]+"), "#k=[REDACTED]"),
    (re.compile(r"\baither_sk_[A-Za-z0-9._\-]+"), "aither_sk_[REDACTED]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]+"), "[REDACTED]"),
    # Credentials inside a URL (https://user:token@host -- git prints these).
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/@:]+:[^\s/@]+@"), r"\1[REDACTED]@"),
    # Compact JWTs (header.payload.signature).
    (re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "[REDACTED]"),
    # key=value / key: value / "key": "value" for any key naming a password, secret
    # or token (SECRET_KEY=, client_secret:, "access_token": ...). Bounded name runs
    # avoid quadratic backtracking on long word-character runs.
    (
        re.compile(
            r"(?i)([A-Za-z0-9_.\-]{0,64}(?:password|passwd|pwd|secret|token)"
            r"[A-Za-z0-9_.\-]{0,64})"
            r"(\"?\s*[=:]\s*[\"']?)([^\s\"'&,;]+)"
        ),
        r"\1\2[REDACTED]",
    ),
]

#: Combined ``(pattern, replacement)`` list exported for callers (e.g.
#: ``adk.contribute``) that iterate redaction rules directly. Sourced live from
#: ``awreport.redact.Redactor.PATTERNS`` plus ADK-specific log rules.
LOG_REDACTIONS: List[Tuple[Pattern[str], str]] = [
    *_ADK_EXTRA_REDACTIONS,
    *[(pat, "[REDACTED]") for pat in Redactor.PATTERNS.values()],
]


def _scrub(text: str) -> str:
    """Apply ADK extras + ``awreport.redact.Redactor`` without rewriting local paths."""
    if not text:
        return ""
    out = text
    for pat, repl in _ADK_EXTRA_REDACTIONS:
        out = pat.sub(repl, out)
    for pat in Redactor.PATTERNS.values():
        out = pat.sub("[REDACTED]", out)
    out = _REDACTOR._redact_high_entropy(out)
    return out


def redact_log_line(line: str) -> str:
    """``line`` with every known credential shape replaced."""
    return _scrub(line or "")


def redact_text(text: str) -> str:
    """Multi-line ``text`` with credentials scrubbed (including multi-line PEM blocks)."""
    if not text:
        return ""
    # Scrub multi-line private key blocks before per-line processing so PEM blocks
    # spanning multiple lines never escape line-by-line matching.
    pem_pat = Redactor.PATTERNS.get("private_key_block")
    scrubbed = pem_pat.sub("[REDACTED]", text) if pem_pat is not None else text
    return "".join(redact_log_line(ln) for ln in scrubbed.splitlines(keepends=True))
