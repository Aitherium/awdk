"""The bug reporter must not carry a secret off the machine.

`adk-bug` has two egress paths and until 2026-09-20 neither scrubbed anything:
a POST to gateway.aitherium.com/v1/bugs, and `_build_github_url`, which
pre-fills a PUBLIC issue on Aitherium/awdk. Both interpolate the user's free
text and `last_error` -- which is `str(exc_value)` plus three traceback frames,
and an exception message here routinely names a token-bearing URL or a home path.

Every test below drives the real awreport redactor, not a mock: the point is
whether a token SURVIVES the call, and a mock that returns "[REDACTED]" would
pass while the shipped path leaked. The literal below is a fake of the shape
`.claude/rules/secret-safety.md` lists; it is not a credential.
"""

from __future__ import annotations  # noqa: I001 - the isolated ruff run orders this differently

import asyncio

import pytest

from adk.bugreport import (
    ReportNotRedactableError,
    _build_github_url,
    build_report,
    redact_report,
    submit_bug_report,
)

# Shaped like a GitHub PAT so the redactor's pattern set has something real to
# match. Invented for this test; it authenticates nothing.
FAKE_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0" + "K1l2M3n4O5p6Q7r8S9t0"


def _report_carrying(secret: str) -> dict:
    report = build_report(description=f"it broke while using {secret}")
    report["last_error"] = {
        "type": "HTTPError",
        "message": f"401 from https://api.example.com/v1?access_token={secret}",
        "traceback": [f'  File "/home/dave/x.py", line 3\n    auth="{secret}"\n'],
    }
    return report


def test_the_token_does_not_survive_redaction():
    """The whole point: the literal must not appear anywhere in the output."""
    out = redact_report(_report_carrying(FAKE_TOKEN))
    assert FAKE_TOKEN not in repr(out)
    assert out["redacted"] is True


def test_the_scrubbed_error_is_not_quietly_the_original():
    """A redactor that returns the input unchanged would pass a `not in` check
    only by accident of the pattern set. Assert the field actually moved."""
    raw = _report_carrying(FAKE_TOKEN)
    out = redact_report(raw)
    assert out["last_error"]["message"] != raw["last_error"]["message"]
    assert FAKE_TOKEN not in out["last_error"]["message"]
    assert FAKE_TOKEN not in "".join(out["last_error"]["traceback"])


def test_the_public_issue_url_carries_nothing_secret():
    """_build_github_url is the path a human clicks. It must be fed the scrubbed
    report -- percent-encoding hides a leak from a plain substring check, so
    assert on the ENCODED url too."""
    import urllib.parse

    out = redact_report(_report_carrying(FAKE_TOKEN))
    url = _build_github_url(out["description"], out)
    assert FAKE_TOKEN not in url
    assert FAKE_TOKEN not in urllib.parse.unquote(url)


def test_a_dry_run_shows_the_redacted_payload_not_the_raw_one():
    """A dry run exists so someone can check what leaves. Printing the raw
    report would make the one safety affordance lie."""
    result = asyncio.run(submit_bug_report(
        description=f"token is {FAKE_TOKEN}", include_logs=False, dry_run=True))
    assert result["submitted"] is False
    assert FAKE_TOKEN not in repr(result["report"])
    assert result["report"]["redacted"] is True


def test_it_fails_closed_when_redaction_raises(monkeypatch):
    """No send, no local save, no url -- a report that cannot be scrubbed is
    refused. Patched where the name is LOOKED UP, not where it is defined."""
    from awreport import RedactionError

    def boom(**_kwargs):
        raise RedactionError("pattern set unavailable")

    monkeypatch.setattr("adk.bugreport.redact_feedback", boom)

    with pytest.raises(ReportNotRedactableError):
        redact_report(build_report(description="anything"))

    result = asyncio.run(submit_bug_report(description="anything", dry_run=False))
    assert result["submitted"] is False
    assert result["github_url"] is None
    assert result["local_path"] is None
    assert "redaction failed" in result["error"]
