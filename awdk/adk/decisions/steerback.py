"""Get the owner's words back INTO the session that raised the card.

The store always writes the answer to the session's steering mailbox, and that
is the channel that never fails. What it is not, is *immediate*: an interactive
Claude Code tab drains its mailbox on ``UserPromptSubmit``, so a card answered
while the session sits idle reaches the agent only when the owner goes to the
terminal and types something — which is the trip the card was raised to save.

So there are tiers, tried in order, and the caller is told which one landed:

1. **harness PTY** — a session the ``adk harness`` daemon owns is a real pty we
   can write to. This is the strongest form: the text arrives as if typed.
2. **in-flight adk turn** — ``POST /chat/steer`` injects between tool
   iterations of a running turn (``adk.steering``), so a long-running agent
   reacts before its next LLM call.
3. **console typing** — Windows-only, opt-in, for an interactive TUI with no
   IPC (see ``adk.decisions.terminal``).

Everything here is best-effort by design and NOTHING here raises. A live tier
that is down must cost the answer nothing, because the mailbox already holds it.
The one thing that is never done is claiming success: the return value
distinguishes "the agent has this now" from "it is queued for whenever the
session next looks", and every surface renders that distinction.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from adk.decisions.store import DecisionCard

#: How long any single live attempt may take. A steer is a nicety layered on top
#: of a mailbox write that already succeeded; it must never hold up a click.
TIMEOUT_SECONDS = float(os.getenv("AITHER_DECISIONS_STEER_TIMEOUT", "3"))


def harness_url() -> str:
    explicit = os.getenv("AITHER_HARNESS_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    host = os.getenv("AITHER_HARNESS_HOST", "127.0.0.1")
    port = os.getenv("AITHER_HARNESS_PORT", "8362")
    # 127.0.0.1, never localhost: measured on this box, ::1 refuses after
    # 2120 ms while IPv4 connects in 3 ms.
    return f"http://{host}:{port}"


def harness_token() -> str:
    """The daemon bearer, from the environment or the file the daemon writes."""
    token = os.getenv("AITHER_HARNESS_TOKEN", "").strip()
    if token:
        return token
    path = Path.home() / ".aither" / "harness_token"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _post(url: str, body: dict, token: str = "") -> tuple[bool, str]:
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method="POST")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            if 200 <= response.status < 300:
                return True, f"HTTP {response.status}"
            return False, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"unreachable: {exc}"


def _get(url: str, token: str = "") -> tuple[bool, str]:
    request = urllib.request.Request(url, method="GET")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"unreachable: {exc}"


def try_harness(session_id: str, text: str) -> tuple[bool, str]:
    """Write into a daemon-owned PTY session. ``(landed, why)``."""
    if not session_id:
        return False, "no session id"
    token = harness_token()
    base = harness_url()
    known, why = _get(f"{base}/sessions/{session_id}", token)
    if not known:
        # A 404 here is the COMMON case (an interactive Claude Code tab is not a
        # daemon session), so it is reported as a miss, never as an error.
        return False, f"not a harness session ({why})"
    ok, detail = _post(f"{base}/sessions/{session_id}/input", {"text": text + "\n"}, token)
    return ok, ("wrote to the harness pty" if ok else f"harness refused ({detail})")


def try_chat_steer(session_id: str, text: str) -> tuple[bool, str]:
    """Inject into an in-flight ``/chat/stream`` turn. ``(landed, why)``."""
    if not session_id:
        return False, "no session id"
    base = os.getenv("AITHER_DECISIONS_STEER_URL", "").strip()
    if not base:
        return False, "no /chat/steer endpoint configured"
    ok, detail = _post(
        f"{base.rstrip('/')}/chat/steer",
        {"session_id": session_id, "message": text, "action": "append"},
    )
    return ok, ("injected into the running turn" if ok else f"steer refused ({detail})")


def try_console(session_pid: int, text: str) -> tuple[bool, str]:
    """Type into the session's console. Opt-in; see ``adk.decisions.terminal``."""
    if not session_pid:
        return False, "no session process recorded"
    try:
        from adk.decisions import terminal
    except ImportError as exc:  # pragma: no cover
        return False, f"terminal module unavailable: {exc}"
    if not terminal.console_input_enabled():
        return False, "console typing is off"
    return terminal.type_into_console(session_pid, text)


def deliver(card: DecisionCard, text: str) -> tuple[bool, str]:
    """Best live tier that accepts ``text``. ``(reached_the_agent_now, how)``.

    Returning False is a normal, expected outcome — it means the mailbox is the
    channel and the agent will see this at its next prompt. It is NOT an error,
    and callers must not render it as one; they must also not render it as
    delivery, which is the whole reason this returns a bool instead of None.
    """
    session = (card.source.session_id or "").strip()
    attempts: list[str] = []

    landed, why = try_harness(session, text)
    attempts.append(f"harness: {why}")
    if landed:
        return True, why

    landed, why = try_chat_steer(session, text)
    attempts.append(f"chat-steer: {why}")
    if landed:
        return True, why

    landed, why = try_console(card.source.session_pid, text)
    attempts.append(f"console: {why}")
    if landed:
        return True, why

    return False, "; ".join(attempts)


def _self_test() -> int:
    """Prove every tier MISSES cleanly, because missing is the normal case.

    There is deliberately no "it delivered" assertion: that needs a live daemon,
    and a self-test that silently passes when the daemon is absent would assert
    nothing at all. What is asserted is the property that matters — a tier that
    cannot deliver returns False WITH a reason, and never True.
    """
    from adk.decisions.store import DecisionSource

    problems: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if condition else 'FAIL'} {name}{'' if condition else ' ' + detail}")
        if not condition:
            problems.append(name)

    landed, why = try_harness("", "hello")
    check("no session id cannot land on the harness", not landed and bool(why), why)

    landed, why = try_harness("definitely-not-a-session", "hello")
    check("an unknown session is a miss, not a claim", not landed, why)

    previous = os.environ.pop("AITHER_DECISIONS_STEER_URL", None)
    landed, why = try_chat_steer("s", "hello")
    check("chat-steer with no endpoint is a miss", not landed and "configured" in why, why)
    os.environ["AITHER_DECISIONS_STEER_URL"] = "http://127.0.0.1:1"  # nothing listens
    landed, why = try_chat_steer("s", "hello")
    check("chat-steer against a dead port is a miss", not landed, why)
    if previous is None:
        os.environ.pop("AITHER_DECISIONS_STEER_URL", None)
    else:
        os.environ["AITHER_DECISIONS_STEER_URL"] = previous

    landed, why = try_console(0, "hello")
    check("console typing at no pid is a miss", not landed, why)

    card = DecisionCard(id="d-test", title="t",
                        source=DecisionSource(session_id="nope-not-real"))
    landed, why = deliver(card, "hello")
    check("deliver() reports every tier it tried", not landed and why.count(";") >= 1, why)
    check("deliver() never claims a tier it did not use", "harness" in why and "console" in why)

    print()
    if problems:
        print(f"steerback self-test FAILED — {', '.join(problems)}")
        return 1
    print("steerback self-test passed - every tier misses honestly")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
