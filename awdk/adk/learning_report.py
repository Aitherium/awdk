"""Report a completed task so the platform can learn from it. Opt-in.

WHY THIS IS OPT-IN AND STAYS OPT-IN.

An agent's task text, its assembled system prompt and its answer are the most
sensitive things it holds: on a customer's own machine they may contain their
data, their people and their business. Sending them anywhere is a decision that
belongs to whoever runs the agent, so this module does nothing at all unless a
reporting endpoint has been configured AND reporting is switched on. Unset means
off; there is no "helpful" default.

WHY IT EXISTS.

Agents built on this SDK produced work that reached no learning loop of any kind
-- their outcomes were invisible to the platform that serves them. Enabling this
lets an operator contribute their agent's successful task outcomes back, the
same way the platform's own agents do.

WHAT IT SENDS, AND WHAT IT NEVER SENDS.

Sent: the task, the assembled system prompt, the answer, a success flag, and a
coarse quality score. Not sent: tool arguments, memory contents, credentials, or
anything from the environment. If you would not paste it into a support ticket,
it does not belong in a training corpus either.

The receiving side tags these rows with their origin and quarantines them: an
SDK agent may be pointed at any model backend, so the platform cannot assume the
text came from a model it controls, and text from an uncontrolled producer must
never mix silently into a corpus.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

#: Switch. Unset or anything other than a truthy value means DO NOT REPORT.
#: Deliberately not `!= "false"`: a privacy-relevant send must require a
#: deliberate yes, never merely the absence of a no.
ENABLE_ENV = "AITHER_LEARNING_REPORT"

#: Where to POST. No default host: a module that guesses an endpoint can send a
#: customer's text somewhere they never chose.
URL_ENV = "AITHER_LEARNING_REPORT_URL"

#: A quality floor mirrors the platform's own: a failed or poor outcome is not
#: something to imitate, so it is not worth sending.
MIN_QUALITY = 0.7

_TRUTHY = {"1", "true", "yes", "on"}


def reporting_enabled() -> bool:
    """True only when an operator has explicitly switched reporting on."""
    if os.getenv(ENABLE_ENV, "").strip().lower() not in _TRUTHY:
        return False
    return bool(os.getenv(URL_ENV, "").strip())


def build_payload(
    task: str,
    answer: str,
    *,
    agent_name: str = "",
    system_prompt: str = "",
    success: bool = True,
    quality_score: float = 0.8,
    effort_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The row, built explicitly so what is sent is readable in one place.

    `system_prompt_captured` is TRUE here because an SDK agent assembles its own
    prompt and therefore genuinely knows it. That matters: a row whose prompt is
    unknown must never be recorded as having an empty one, or a corpus learns a
    distribution nothing ever sends.
    """
    return {
        "provenance": "adk",
        "agent": agent_name or "adk-agent",
        "task": task,
        "answer": answer,
        "system_prompt": system_prompt,
        "system_prompt_captured": bool(system_prompt),
        "success": bool(success),
        "quality_score": float(quality_score),
        "effort_plan": effort_plan or {},
    }


def report_outcome(
    task: str,
    answer: str,
    *,
    agent_name: str = "",
    system_prompt: str = "",
    success: bool = True,
    quality_score: float = 0.8,
    effort_plan: dict[str, Any] | None = None,
    timeout: float = 5.0,
) -> bool:
    """POST one completed task. Returns True only if it was accepted.

    Never raises. A learning report must not be able to fail the work it is
    reporting on -- an agent that crashes because telemetry was unreachable is
    strictly worse than one that learns nothing.
    """
    if not reporting_enabled():
        return False
    if not success or quality_score < MIN_QUALITY:
        return False
    if not (task or "").strip() or not (answer or "").strip():
        return False

    url = os.getenv(URL_ENV, "").strip().rstrip("/")
    payload = build_payload(
        task, answer, agent_name=agent_name, system_prompt=system_prompt,
        success=success, quality_score=quality_score, effort_plan=effort_plan,
    )
    req = urllib.request.Request(
        f"{url}/learning/agent-outcome",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    token = os.getenv("AITHER_API_KEY", "").strip()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # Debug, not warning: an operator who has not configured this correctly
        # should not get noise on every task, and the work itself succeeded.
        logger.debug("learning report not delivered: %s", exc)
        return False


def self_test() -> int:
    """Prove the switch is off by default and the payload is honest."""
    bad = 0

    def ck(cond: bool, what: str) -> None:
        nonlocal bad
        print(f"  {'ok  ' if cond else 'FAIL'} {what}")
        if not cond:
            bad += 1

    saved = {k: os.environ.get(k) for k in (ENABLE_ENV, URL_ENV)}
    try:
        os.environ.pop(ENABLE_ENV, None)
        os.environ.pop(URL_ENV, None)
        ck(not reporting_enabled(),
           "reporting is OFF when unset - a privacy-relevant send must require "
           "a deliberate yes, never the absence of a no")

        os.environ[ENABLE_ENV] = "1"
        ck(not reporting_enabled(),
           "and still OFF with no endpoint - a module that guesses a host can "
           "send a customer's text somewhere they never chose")

        os.environ[URL_ENV] = "https://example.invalid"
        ck(reporting_enabled(), "ON only once BOTH are set")

        os.environ[ENABLE_ENV] = "false"
        ck(not reporting_enabled(), "an explicit false is off")

        os.environ[ENABLE_ENV] = "1"
        ck(report_outcome("t", "a", success=False) is False,
           "a FAILED outcome is never sent - failure is not something to imitate")
        ck(report_outcome("t", "a", quality_score=0.1) is False,
           "and neither is a low-quality one")
        ck(report_outcome("", "a") is False and report_outcome("t", "") is False,
           "an empty task or answer is not a training row")

        p = build_payload("t", "a", system_prompt="S")
        ck(p["system_prompt_captured"] is True and p["provenance"] == "adk",
           "a known prompt is marked captured, and the row says where it came "
           "from")
        p2 = build_payload("t", "a")
        ck(p2["system_prompt_captured"] is False,
           "an ABSENT prompt is marked uncaptured rather than sent as empty - "
           "the two are different facts and only one is safe to train on")
        ck("memory" not in p and "tools" not in p,
           "the payload carries no memory or tool internals")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print()
    if bad:
        print(f"SELF-TEST FAILED ({bad})")
        return 1
    print("SELF-TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
