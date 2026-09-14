"""Senses — the aw* perception bricks, exposed as agent tools.

WHAT THIS IS FOR

Five bricks were built and were reachable only from a shell. An agent could not
use them, which made them libraries rather than capabilities. This pack is the
seam: one import per brick, done LAZILY, so a machine with none of them
installed still loads the pack and simply reports which tools are unavailable.

THE ONE RULE THAT MATTERS HERE

A missing brick must say WHICH brick and HOW to get it. It must never return an
empty string, an empty list, or a cheerful null -- an agent reads "" as "there
was nothing to see", which is a different and wrong fact from "I cannot see".
That distinction is the whole reason awvision refuses an empty 200 instead of
returning it, and this layer must not undo it by catching the refusal.

So every tool here returns a dict with an explicit `ok` field. A failure
carries `ok: False` and a `hint` naming the package, and never pretends.

NOTHING HERE REACHES A HOSTED API. Each brick talks to loopback or the local
filesystem. awscreen sends a picture of your desktop, so that property is not
incidental -- see its finder.py for why it was rewritten off a hosted backend.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _missing(brick: str, exc: Exception) -> dict[str, Any]:
    """The refusal shape. Names the brick and the remedy, every time.

    Deliberately NOT an exception: a tool call that raises inside an agent loop
    is often swallowed by the harness and reported as "the tool failed", losing
    the one piece of information that would fix it -- which package is absent.
    """
    return {
        "ok": False,
        "error": f"{brick} is not available: {exc}",
        "hint": (
            f"Install it: pip install {brick}  (or, from a monorepo checkout, "
            f"pip install -e AitherOS/packages/{brick})"
        ),
    }


# ── awvision ────────────────────────────────────────────────────────────────

def see_image(image_path: str, question: str) -> dict[str, Any]:
    """Ask a question about an image and get an answer.

    Uses a LOCAL vision endpoint (AWVISION_URL). Returns {'ok': True, 'answer':
    str} or a refusal naming what is missing.
    """
    try:
        from awvision import ask_vision
    except Exception as exc:  # noqa: BLE001
        return _missing("awvision", exc)
    try:
        return {"ok": True, "answer": ask_vision(image_path, question)}
    except Exception as exc:  # noqa: BLE001
        # awvision raises a written-for-a-human message when the model has no
        # vision or the endpoint is absent. Pass it through verbatim rather
        # than replacing it with a generic failure.
        return {"ok": False, "error": str(exc)}


def describe_image(image_path: str) -> dict[str, Any]:
    """Describe an image in one paragraph, via a LOCAL vision endpoint."""
    try:
        from awvision import describe_image as _describe
    except Exception as exc:  # noqa: BLE001
        return _missing("awvision", exc)
    try:
        return {"ok": True, "description": _describe(image_path)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


# ── awvoice ─────────────────────────────────────────────────────────────────

def transcribe_audio(audio_path: str) -> dict[str, Any]:
    """Turn speech into text, via a LOCAL STT endpoint (AWVOICE_STT_URL)."""
    try:
        from awvoice import VoiceClient
    except Exception as exc:  # noqa: BLE001
        return _missing("awvoice", exc)
    try:
        return {"ok": True, "text": VoiceClient().transcribe(audio_path)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def speak_text(text: str, out_path: str) -> dict[str, Any]:
    """Turn text into speech, via a LOCAL TTS endpoint (AWVOICE_TTS_URL)."""
    try:
        from awvoice import VoiceClient
    except Exception as exc:  # noqa: BLE001
        return _missing("awvoice", exc)
    try:
        VoiceClient().synthesize(text, out_path)
        return {"ok": True, "path": out_path}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


# ── awscreen ────────────────────────────────────────────────────────────────

def see_screen(out_path: str = "screen.png") -> dict[str, Any]:
    """Capture this machine's screen to a file."""
    try:
        from awscreen import capture
    except Exception as exc:  # noqa: BLE001
        return _missing("awscreen", exc)
    try:
        return {"ok": True, "path": str(capture(out_path))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def find_on_screen(image_path: str, description: str) -> dict[str, Any]:
    """Find something on a screenshot by describing it. Returns coordinates.

    LOCATES ONLY -- it does not click. A tool that both finds and clicks turns
    a wrong coordinate into a wrong action with nothing in between.
    """
    try:
        from awscreen import Finder
    except Exception as exc:  # noqa: BLE001
        return _missing("awscreen", exc)
    try:
        f = Finder()
        shot = f.load_image(image_path)
        found = f.find(shot, description)
        return {
            "ok": True,
            "count": len(found),
            "elements": [
                {"x": e.x, "y": e.y, "width": e.width, "height": e.height,
                 "confidence": e.confidence, "description": e.description}
                for e in found
            ],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


# ── awwall ──────────────────────────────────────────────────────────────────

def egress_check(host: str) -> dict[str, Any]:
    """May this workload reach that host? Default DENY.

    `allowed` is False on every error path on purpose: a policy question that
    cannot be answered must not answer 'yes'.
    """
    try:
        from awwall import Policy
    except Exception as exc:  # noqa: BLE001
        d = _missing("awwall", exc)
        d["allowed"] = False
        return d
    try:
        allowed, rule = Policy.load_default().check(host)
        return {"ok": True, "allowed": bool(allowed),
                "rule": getattr(rule, "pattern", None)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "allowed": False, "error": str(exc)}


def egress_explain(host: str) -> dict[str, Any]:
    """Which rule decided this host, or that no rule matched (so: denied)."""
    try:
        from awwall import Policy
    except Exception as exc:  # noqa: BLE001
        d = _missing("awwall", exc)
        d["allowed"] = False
        return d
    try:
        allowed, rule = Policy.load_default().check(host)
        return {
            "ok": True,
            "allowed": bool(allowed),
            "reason": (f"matched {rule.pattern}" if allowed and rule
                       else "no rule matched -- default deny"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "allowed": False, "error": str(exc)}


# ── awrise ──────────────────────────────────────────────────────────────────

def schedule_add(name: str, every: str, run: str) -> dict[str, Any]:
    """Wake something on a schedule. `every` is like 15m / 2h / 1d."""
    try:
        from awrise.cli import cmd_add, parse_interval
    except Exception as exc:  # noqa: BLE001
        return _missing("awrise", exc)
    try:
        parse_interval(every)  # refuse an unparseable interval BEFORE writing
    except ValueError as exc:
        return {"ok": False, "error": f"bad interval {every!r}: {exc}"}
    try:
        import argparse
        rc = cmd_add(argparse.Namespace(name=name, every=every, run=run))
        return {"ok": rc == 0, "name": name} if rc == 0 else {
            "ok": False, "error": f"awrise refused (exit {rc})"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def schedule_run_due() -> dict[str, Any]:
    """Run whatever is due, once. Idempotent within a window."""
    try:
        from awrise.cli import cmd_run_due
    except Exception as exc:  # noqa: BLE001
        return _missing("awrise", exc)
    try:
        import argparse
        rc = cmd_run_due(argparse.Namespace())
        return {"ok": rc == 0, "exit_code": rc}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


_TOOLS = [
    (see_image, "see_image", "Ask a question about an image (local vision model)."),
    (describe_image, "describe_image", "Describe an image (local vision model)."),
    (transcribe_audio, "transcribe_audio", "Speech to text (local STT)."),
    (speak_text, "speak_text", "Text to speech (local TTS)."),
    (see_screen, "see_screen", "Capture this machine's screen to a file."),
    (find_on_screen, "find_on_screen",
     "Find something on a screenshot by description; returns coordinates. Does not click."),
    (egress_check, "egress_check", "May this workload reach that host? Default deny."),
    (egress_explain, "egress_explain", "Which rule allowed or denied a host."),
    (schedule_add, "schedule_add", "Wake something on a schedule (15m / 2h / 1d)."),
    (schedule_run_due, "schedule_run_due", "Run whatever is due, once."),
]


def register(registry) -> int:
    """Register the senses tools on the agent's tool registry.

    Registers every tool even when its brick is absent, deliberately. A tool
    that disappears when a package is missing leaves the agent unable to learn
    WHY it cannot see -- it simply has no eyes and no explanation. Present and
    honestly refusing beats absent and silent.
    """
    n = 0
    for fn, name, desc in _TOOLS:
        try:
            registry.register(fn, name=name, description=desc)
            n += 1
        except TypeError:
            # Older registries take only the fn (name from __name__).
            try:
                registry.register(fn)
                n += 1
            except Exception as exc:  # noqa: BLE001 — one bad tool must not block the pack
                logger.debug("senses pack: skip %s (%s)", name, exc)
        except Exception as exc:  # noqa: BLE001
            logger.debug("senses pack: skip %s (%s)", name, exc)
    logger.info("Senses pack registered %d tools", n)
    return n
