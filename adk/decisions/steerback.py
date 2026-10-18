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
from typing import Any

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


# ── waiters: every agent blocked on a card, and what happens when it closes ────
#
# One card can have several waiters (two agents asked for the same secret, and
# the second JOINED the first one's card). Each is recorded next to the card as
# ``<store>/waiters/<card_id>.json`` -- outside the ``d-*.json`` namespace every
# scan reads, so no listing ever mistakes it for a card. On close, EVERY waiter
# is delivered to: a call still blocking in its tool is left to return the
# answer itself; a live session gets the live tiers (else its mailbox, which a
# live tab drains); a session that is gone gets the answer posted on the card's
# #decisions message and is RESUMED, because a mailbox nobody will ever read is
# not delivery.

WAITERS_DIRNAME = "waiters"


def waiters_file(store: Any, card_id: str) -> Path:
    return Path(store.path) / WAITERS_DIRNAME / f"{card_id}.json"


def _read_waiters(store: Any, card_id: str) -> dict:
    try:
        raw = json.loads(waiters_file(store, card_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"waiters": []}
    if not isinstance(raw, dict) or not isinstance(raw.get("waiters"), list):
        return {"waiters": []}
    return raw


def _write_waiters(store: Any, card_id: str, data: dict) -> None:
    target = waiters_file(store, card_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, target)


def load_waiters(store: Any, card_id: str) -> list:
    return [w for w in _read_waiters(store, card_id)["waiters"] if isinstance(w, dict)]


def register_waiter(store: Any, card_id: str, waiter: dict) -> str:
    """Record (or refresh) one waiter on ``card_id``. Returns its key."""
    import time as _time

    key = (str(waiter.get("session_id") or "").strip()
           or f"pid-{int(waiter.get('session_pid') or 0)}-{int(_time.time() * 1000)}")
    with store._lock:  # noqa: SLF001 - same lock as the card it rides on
        data = _read_waiters(store, card_id)
        rows = [w for w in data["waiters"] if isinstance(w, dict) and w.get("key") != key]
        rows.append({**waiter, "key": key, "state": "waiting",
                     "registered_at": _time.time()})
        data["waiters"] = rows
        _write_waiters(store, card_id, data)
    return key


def mark_waiter(store: Any, card_id: str, key: str, state: str) -> None:
    with store._lock:  # noqa: SLF001
        data = _read_waiters(store, card_id)
        for row in data["waiters"]:
            if isinstance(row, dict) and row.get("key") == key:
                row["state"] = state
        _write_waiters(store, card_id, data)


def prompt_pid(store: Any, card_id: str) -> int:
    try:
        return int(_read_waiters(store, card_id).get("prompt_pid") or 0)
    except (TypeError, ValueError):
        return 0


def set_prompt_pid(store: Any, card_id: str, pid: int) -> None:
    with store._lock:  # noqa: SLF001
        data = _read_waiters(store, card_id)
        data["prompt_pid"] = int(pid)
        _write_waiters(store, card_id, data)


def pid_alive(pid: int) -> bool:
    try:
        from adk.decisions import winproc
    except ImportError:  # pragma: no cover
        return False
    try:
        return bool(winproc.pid_alive(int(pid or 0)))
    except (OSError, ValueError):
        return False


def _still_blocking(waiter: dict) -> bool:
    """The waiter's own tool call is still polling and will return the answer."""
    import time as _time

    if waiter.get("state") != "waiting":
        return False
    until = float(waiter.get("waiting_until") or 0)
    return until > _time.time() and pid_alive(int(waiter.get("waiter_pid") or 0))


def session_live(waiter: dict) -> bool:
    """Is anyone there to read a mailbox? A daemon session, or a live process."""
    session = str(waiter.get("session_id") or "").strip()
    if session:
        known, _why = _get(f"{harness_url()}/sessions/{session}", harness_token())
        if known:
            return True
    return pid_alive(int(waiter.get("session_pid") or 0))


def _daemon_post(path: str, body: dict) -> tuple[bool, dict]:
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(harness_url() + path, data=payload, method="POST")
    request.add_header("Content-Type", "application/json")
    token = harness_token()
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8", "replace")
            return 200 <= response.status < 300, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        return False, {"error": f"HTTP {exc.code}"}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, {"error": f"unreachable: {exc}"}


def resume_waiter(waiter: dict, text: str) -> tuple[bool, str]:
    """Bring a dead waiter back: resume its session, else spawn one with its task.

    The same two daemon calls ``awsh_resume`` / ``awsh_spawn`` make, then the
    answer is submitted as the session's next turn so it carries on by itself.
    """
    harness = str(waiter.get("harness") or "claude")
    cwd = str(waiter.get("cwd") or "")
    session = str(waiter.get("session_id") or "").strip()
    tried: list[str] = []
    if session:
        ok, out = _daemon_post("/sessions", {"harness": harness, "resume_session_id": session,
                                             "cwd": cwd, "title": "resumed: credential stored"})
        new_id = (out.get("id") or out.get("session_id")) if ok else ""
        if new_id:
            sent, detail = _daemon_post(f"/sessions/{new_id}/submit", {"text": text})
            if sent:
                return True, f"resumed {session[:12]} as {new_id}"
            tried.append(f"resume submit refused ({detail.get('error', '?')})")
        else:
            tried.append(f"resume refused ({out.get('error', 'no id')})")
    task = str(waiter.get("task") or "").strip()
    if not task:
        return False, "; ".join(tried + ["no task recorded to spawn from"])
    ok, out = _daemon_post("/sessions", {"harness": harness, "cwd": cwd,
                                         "title": "credential stored: continue"})
    new_id = (out.get("id") or out.get("session_id")) if ok else ""
    if not new_id:
        return False, "; ".join(tried + [f"spawn refused ({out.get('error', 'no id')})"])
    sent, detail = _daemon_post(f"/sessions/{new_id}/submit",
                                {"text": f"{text}\n\nThe task that needed it: {task}"})
    if sent:
        return True, f"spawned {new_id} to continue the task"
    return False, "; ".join(tried + [f"spawn submit refused ({detail.get('error', '?')})"])


def post_to_decisions(store: Any, card: DecisionCard, line: str) -> None:
    """Put the outcome on the card's own #decisions relay message (a fact + sync)."""
    try:
        updated = store.add_facts(card.id, [line[:240]])
    except Exception as exc:  # noqa: BLE001 - delivery must survive the post
        import sys as _sys

        _sys.stderr.write(f"[steerback] could not record on {card.id}: {exc}\n")
        return
    try:
        from adk.decisions.relay_post import sync_card
    except ImportError:  # pragma: no cover
        return
    sync_card(updated or card)


#: Injectable for tests: (waiter, text) -> (ok, why); (store, card, line) -> None.
RESUME_HOOK = resume_waiter
POST_HOOK = post_to_decisions


def waiter_text(card: DecisionCard) -> str:
    from adk.decisions.agent_tools import credential_result

    if (card.kind or "").strip().lower() == "credential":
        res = credential_result(card)
        return (f"Owner answered credential card {card.id}: stored={res['stored']} "
                f"secret={res['secret_name']} scope={res['scope']} "
                f"receipt_digest={res['receipt_digest'] or 'none'}. "
                + ("Read it from the vault (never print it) and continue."
                   if res["stored"] else "It was NOT stored -- do not wait for it."))
    return f"Owner answered decision card {card.id}: {card.answer}"


def deliver_waiters(store: Any, card: DecisionCard) -> list:
    """Deliver ``card``'s outcome to EVERY recorded waiter. Returns one row each."""
    from adk.decisions.store import write_steer

    text = waiter_text(card)
    rows: list = []
    waiters = load_waiters(store, card.id)
    source = (card.source.session_id or "").strip()
    if source and source not in {str(w.get("session_id") or "") for w in waiters}:
        # The card was raised elsewhere (the CLI, a hook) and an agent JOINED it:
        # its raiser is a waiter too, and must not lose the delivery it had.
        waiters.append({"key": source, "session_id": source, "state": "waiting",
                        "session_pid": card.source.session_pid,
                        "agent": card.source.agent, "cwd": card.source.cwd})
    for waiter in waiters:
        key = str(waiter.get("key") or "")
        if waiter.get("state") in ("returned", "delivered", "resumed"):
            continue
        if _still_blocking(waiter):
            rows.append({"key": key, "how": "blocking call returns it"})
            continue
        session = str(waiter.get("session_id") or "").strip()
        probe = DecisionCard(id=card.id, title=card.title, source=type(card.source)(
            session_id=session, session_pid=int(waiter.get("session_pid") or 0)))
        if session_live(waiter):
            landed, how = deliver(probe, text)
            state = "delivered"
            if not landed and session:
                write_steer(session, [f"# {text}", ""], suffix="answer",
                            sender="the owner", authority="owner",
                            origin_id=card.id, kind="decision_card")
                how = "mailbox (session live, drains at its next prompt)"
            elif not landed:
                state, how = "undeliverable", f"live process, no session id ({how})"
            mark_waiter(store, card.id, key, state)
            rows.append({"key": key, "how": how})
            continue
        ok, how = RESUME_HOOK(waiter, text)
        who = waiter.get("agent") or "an agent"
        POST_HOOK(store, card, f"{who} ({session[:12] or 'no session'}) had ended: "
                               + (how if ok else f"resume FAILED: {how}"))
        mark_waiter(store, card.id, key, "resumed" if ok else "resume_failed")
        rows.append({"key": key, "how": how, "resumed": ok})
    return rows


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
