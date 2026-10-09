"""Scrub credentials out of log text before it leaves this machine.

One set of patterns for every place that hands log output to someone else: the
local admin API's log tail, and the ``appliance-logs`` / ``appliance-redeploy``
results a device reports to its owner. Stdlib only.
"""

from __future__ import annotations

import re
from typing import List, Pattern, Tuple

__all__ = ["LOG_REDACTIONS", "redact_log_line", "redact_text"]

#: (pattern, replacement), applied in order.
LOG_REDACTIONS: List[Tuple[Pattern[str], str]] = [
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=\-]+"), "Bearer [REDACTED]"),
    (re.compile(r"#k=[A-Za-z0-9._\-%]+"), "#k=[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9._\-]+"), "sk-[REDACTED]"),
    (re.compile(r"\baither_sk_[A-Za-z0-9._\-]+"), "aither_sk_[REDACTED]"),
    (re.compile(r"\b(?:ghp|ghs|gho|ghu|github_pat)_[A-Za-z0-9_]+"), "[REDACTED]"),
    # Credentials inside a URL (https://user:token@host -- git prints these).
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/@:]+:[^\s/@]+@"), r"\1[REDACTED]@"),
    # AWS access key ids (long-lived AKIA, temporary ASIA).
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED-AWS-KEY]"),
    # key=value / key: value / "key": "value" for any key naming a password, secret
    # or token (SECRET_KEY=, client_secret:, "access_token": ...). The value ends at
    # whitespace, a quote or a separator.
    (re.compile(r"(?i)([A-Za-z0-9_.\-]*(?:password|passwd|secret|token)[A-Za-z0-9_.\-]*)"
                r"(\"?\s*[=:]\s*[\"']?)([^\s\"'&,;]+)"), r"\1\2[REDACTED]"),
]


def redact_log_line(line: str) -> str:
    """``line`` with every known credential shape replaced."""
    for pat, repl in LOG_REDACTIONS:
        line = pat.sub(repl, line)
    return line


def redact_text(text: str) -> str:
    """Multi-line ``text``, redacted line by line (line breaks kept)."""
    return "".join(redact_log_line(ln) for ln in (text or "").splitlines(keepends=True))
