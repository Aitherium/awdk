"""``adk learn`` -- Aither Learn in a terminal (the child's side).

A thin, light client of the family tutor's learner routes (``/api/v1/tutor/me/*``)
for a phone terminal (the Pixel "Linux terminal" Debian VM, or any Linux/macOS
shell). It needs only ``httpx``, which the base ``awdk`` install already carries:
no model, no GPU, no torch. The tutor on the server owns every rule -- grading,
hints, breaks, the daily cap -- so this file only shows and sends.

    adk learn            -- print the /learn link and say hello (minutes left today)
    adk learn --open     -- also open the link in a browser, when there is one
    adk learn play       -- practice one quest right here, one item at a time

Sign-in is ``adk login`` (device flow). On a child's phone the grown-up approves
the short code from the parent console, so the token belongs to the CHILD and the
server scopes every /me call to that child. Nothing here names a learner, user or
tenant; the server derives all of it from the bearer.

Kid UX (the same rules as the PWA): warm and short, no red X, no timer, no score,
no streak. A miss shows the worked steps and the same item comes back.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

#: The Veil origin that serves /learn and the /api/tutor proxy. The apex
#: (aitherium.com) is API-less GitHub Pages, so it can never be the API base.
DEFAULT_LEARN_ORIGIN = "https://app.aitherium.com"
API_PREFIX = "/api/tutor"
QUIT_WORDS = ("q", "quit", "stop", "bye", "exit")
MAX_ANSWER = 16  # AnswerPayload.answer max_length on the server

_log = logging.getLogger("adk.learn")

LOOK_LINE = "Let's look at it together."
SPRITE_LINE = "Your sprite learned something new!"
OOPS_LINE = "Let's try that again in a little bit."
LISTEN_LINE = "The next puzzle needs sound. Let's play it in the Learn app!"

#: The terminal asks for the math strand: every math item carries its question in
#: prompt_text and its picture as a text ten-frame. Several reading items keep the
#: question only in tts_text (blend sounds, "which letter says /m/", "tap the word
#: you hear"), which a terminal cannot play -- and printing tts_text would give the
#: answer away.
TERMINAL_STRAND = "math"

#: Tap visuals whose question lives in the audio. When prompt_text differs from
#: tts_text on one of these, the item cannot be answered in text.
AUDIO_ONLY_VISUALS = ("picture_choice", "letter_choice", "word_choice")


# --------------------------------------------------------------------------- config


def learn_origin(explicit: str = "") -> str:
    """Where /learn lives: ``--url``, then ``AITHER_LEARN_URL``, then config, then default."""
    if explicit:
        return explicit.rstrip("/")
    env = os.environ.get("AITHER_LEARN_URL", "").strip()
    if env:
        return env.rstrip("/")
    try:
        from adk.config import load_saved_config

        saved = load_saved_config().get("learn_url") or ""
    except Exception:  # noqa: BLE001 -- a config read never blocks the link
        saved = ""
    return (str(saved) or DEFAULT_LEARN_ORIGIN).rstrip("/")


def learn_token() -> str:
    """The signed-in bearer: ``AITHER_LEARN_TOKEN``, then the ``adk login`` token."""
    env = os.environ.get("AITHER_LEARN_TOKEN", "").strip()
    if env:
        return env
    try:
        from adk.config import load_saved_config

        cfg = load_saved_config()
        tok = str(cfg.get("api_key") or cfg.get("access_token") or "")
        if tok:
            return tok
    except Exception:  # noqa: BLE001
        _log.debug("learn: best-effort step skipped", exc_info=True)
    try:
        bearer = Path.home() / ".aither" / "session-bearer"
        if bearer.is_file():
            return bearer.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        _log.debug("learn: best-effort step skipped", exc_info=True)
    return ""


# --------------------------------------------------------------------------- client


class LearnClient:
    """``/api/tutor/me/*`` over httpx. ``transport`` is for tests (httpx.MockTransport)."""

    def __init__(self, origin: str, token: str, transport: Any = None, timeout: float = 20.0):
        import httpx

        try:
            from adk._tls import tls_verify

            verify: Any = tls_verify()
        except Exception:  # noqa: BLE001
            verify = True
        kwargs: Dict[str, Any] = {"timeout": timeout, "verify": verify}
        if transport is not None:
            kwargs["transport"] = transport
        self._http = httpx.Client(**kwargs)
        self.base = origin.rstrip("/") + API_PREFIX
        self.token = token

    def call(self, path: str, method: str = "GET", body: Optional[dict] = None) -> Tuple[int, Any]:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.token}"}
        if method != "GET":
            # The proxy refuses a write that is not JSON (its CSRF rule).
            headers["Content-Type"] = "application/json"
        try:
            res = self._http.request(
                method, self.base + path, headers=headers, json=body if method != "GET" else None
            )
        except Exception:  # noqa: BLE001 -- offline reads as "try later", never a trace
            return 0, None
        try:
            data = res.json()
        except Exception:  # noqa: BLE001
            data = None
        return res.status_code, data

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:  # noqa: BLE001
            _log.debug("learn: best-effort step skipped", exc_info=True)


# --------------------------------------------------------------------------- render


def _choice_text(choice: Dict[str, Any]) -> str:
    """What a terminal can show for a choice. Picture choices have no label on
    purpose (the child reads the picture, not the word): show the emoji, and only
    fall back to the picture name when there is no emoji."""
    for key in ("label", "emoji", "picture", "value"):
        val = choice.get(key)
        if val:
            return str(val)
    return "?"


def _frame(filled: int) -> str:
    """A ten-frame as text: two rows of five, ``o`` filled and ``.`` empty."""
    n = max(0, min(10, int(filled)))
    cells = ["o"] * n + ["."] * (10 - n)
    return f"[{' '.join(cells[:5])}] [{' '.join(cells[5:])}]"


def ten_frames(visual: Any) -> List[str]:
    """Text ten-frames for the number visuals (ten_frame, ten_frame_pair, two_ten_frames)."""
    if not isinstance(visual, dict):
        return []
    kind = visual.get("kind")
    try:
        if kind == "ten_frame":
            return [_frame(visual.get("filled", 0))]
        if kind in ("ten_frame_pair", "two_ten_frames"):
            return [_frame(visual.get("a", 0)), _frame(visual.get("b", 0))]
    except (TypeError, ValueError):
        return []
    return []


def text_answerable(item: Dict[str, Any]) -> bool:
    """Can a child answer this item from what a terminal shows? False for a tap
    item whose question is only in the audio. Such an item is never answered here
    (a guess would be recorded as a real miss); the quest ends kindly instead."""
    visual = item.get("visual")
    kind = visual.get("kind") if isinstance(visual, dict) else None
    if kind in AUDIO_ONLY_VISUALS:
        return (item.get("prompt_text") or "") == (item.get("tts_text") or "")
    return bool(item.get("prompt_text") or item.get("tts_text"))


def render_item(item: Dict[str, Any], out: Callable[[str], None]) -> List[str]:
    """Print one item; return the choice values (empty for a typed answer)."""
    out("")
    out(f"  {item.get('prompt_text') or item.get('tts_text') or ''}")
    for line in ten_frames(item.get("visual") or {}):
        out(f"    {line}")
    values: List[str] = []
    choices = item.get("choices") or []
    for n, choice in enumerate(choices, 1):
        if isinstance(choice, dict):
            values.append(str(choice.get("value", "")))
            out(f"    {n}) {_choice_text(choice)}")
    if values:
        out("  Type the number of your choice.")
    else:
        out("  Type your answer.")
    return values


def read_answer(raw: str, values: List[str]) -> Optional[str]:
    """Map what was typed to an answer. A number picks a choice; a choice's own
    text also works. ``None`` means "ask again" (empty or not one of the choices)."""
    text = (raw or "").strip()
    if not text:
        return None
    if values:
        if text.isdigit() and 1 <= int(text) <= len(values):
            return values[int(text) - 1][:MAX_ANSWER]
        for v in values:
            if text.lower() == v.lower():
                return v[:MAX_ANSWER]
        return None
    return text[:MAX_ANSWER]


# --------------------------------------------------------------------------- commands


def _say_status(status: int, data: Any, out: Callable[[str], None]) -> None:
    """A kind line for any non-2xx reply. Never a stack trace, never 'error'."""
    say = data.get("say") if isinstance(data, dict) else None
    if say:
        out(f"  {say}")
    elif status == 401:
        out("  You are not signed in yet. Ask your grown-up to run 'adk login' with you.")
    elif status in (403, 404):
        out("  This account is not set up for Aither Learn yet. Ask your grown-up.")
    elif status == 429:
        out("  That's enough practice for today. See you tomorrow!")
    else:
        out(f"  {OOPS_LINE}")


def cmd_learn_info(client: LearnClient, origin: str, out: Callable[[str], None]) -> int:
    out("")
    out(f"  Aither Learn: {origin}/learn")
    status, me = client.call("/me")
    if status != 200 or not isinstance(me, dict):
        _say_status(status, me, out)
        return 0 if status in (401, 403, 404) else 1
    name = me.get("alias") or "friend"
    out(f"  Hi {name}!")
    left = me.get("today_minutes_left")
    if isinstance(left, (int, float)):
        if left > 0:
            out("  There is time for a quest today. Type: adk learn play")
        else:
            out("  Practice is done for today. Rest up!")
    return 0


def play_quest(
    client: LearnClient,
    ask: Callable[[str], str],
    out: Callable[[str], None],
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """One quest, item by item, until the server says it is done or the child stops."""
    status, started = client.call("/me/quest/start", "POST", {"strand": TERMINAL_STRAND})
    if status not in (200, 201) or not isinstance(started, dict):
        _say_status(status, started, out)
        return 0 if status in (401, 403, 404, 429) else 1
    qid = str(started.get("quest_id") or "")
    item = started.get("item") or {}
    total = started.get("items_total")
    out("")
    out(f"  Quest time!{f' {total} little puzzles.' if total else ''} Type q to stop and save.")
    redo = False
    while item:
        if not text_answerable(item):
            out("")
            out(f"  {LISTEN_LINE}")
            return _end(client, qid, out)
        values = render_item(item, out)
        shown = clock()
        answer: Optional[str] = None
        while answer is None:
            try:
                raw = ask("  > ")
            except (EOFError, KeyboardInterrupt):
                raw = "q"
            if raw.strip().lower() in QUIT_WORDS:
                return _end(client, qid, out)
            answer = read_answer(raw, values)
            if answer is None:
                out("  Pick one of the numbers." if values else "  Type an answer, or q to stop.")
        latency = max(0, int((clock() - shown) * 1000))
        status, res = client.call(
            f"/me/quest/{qid}/answer",
            "POST",
            {
                "item_id": item.get("item_id", ""),
                "answer": answer,
                "latency_ms": latency,
                "redo": redo,
            },
        )
        if status == 429 and isinstance(res, dict):
            out(f"  {res.get('say') or 'Time to rest. See you tomorrow!'}")
            return 0
        if status != 200 or not isinstance(res, dict):
            _say_status(status, res, out)
            return 1
        if res.get("sprite_event"):
            out(
                "  * " + ((res["sprite_event"] or {}).get("say") or SPRITE_LINE)
            )
        if res.get("done"):
            out(f"  {res.get('say') or 'All done. Great work today!'}")
            return 0
        nxt = res.get("next_item")
        if res.get("feedback") == "lets_look":
            out(f"  {res.get('say') or LOOK_LINE}")
            for step in res.get("hint_steps") or []:
                if isinstance(step, str) and step:
                    out(f"   - {step}")
        else:
            out(f"  {res.get('say') or 'Yay!'}")
        if res.get("break"):
            out("  Wiggle break! Stand up and stretch.")
            try:
                ask("  Press Enter when you are ready. ")
            except (EOFError, KeyboardInterrupt):
                return _end(client, qid, out)
        if nxt:
            item, redo = nxt, False
        elif res.get("feedback") == "lets_look":
            redo = True  # the same item comes back; the server caps redos
        else:
            return _end(client, qid, out)
    return _end(client, qid, out)


def _end(client: LearnClient, qid: str, out: Callable[[str], None]) -> int:
    if qid:
        status, res = client.call(f"/me/quest/{qid}/end", "POST", {})
        if status == 200 and isinstance(res, dict) and res.get("say"):
            out(f"  {res['say']}")
            return 0
    out("  Saved. See you next time!")
    return 0


# --------------------------------------------------------------------------- CLI


def register_parser(sub: Any) -> None:
    p = sub.add_parser(
        "learn",
        help="Aither Learn in the terminal: the /learn link, and practice a quest",
        description="Aither Learn (family tutor) for a signed-in child account. "
        "Sign in first with 'adk login'; a grown-up approves the code.",
    )
    p.add_argument(
        "learn_action",
        nargs="?",
        choices=["play"],
        default=None,
        help="play: practice one quest right here",
    )
    p.add_argument("--open", action="store_true", help="Also open the /learn link in a browser")
    p.add_argument("--url", default="", help=f"Learn origin (default: {DEFAULT_LEARN_ORIGIN})")


def cmd_learn(
    args: argparse.Namespace,
    transport: Any = None,
    ask: Optional[Callable[[str], str]] = None,
    out: Optional[Callable[[str], None]] = None,
) -> int:
    say = out or (lambda s: print(s, flush=True))
    origin = learn_origin(getattr(args, "url", "") or "")
    if getattr(args, "open", False):
        try:
            import webbrowser

            webbrowser.open(f"{origin}/learn")
        except Exception:  # noqa: BLE001 -- a phone VM has no browser; the link is printed
            _log.debug("learn: best-effort step skipped", exc_info=True)
    token = learn_token()
    if not token:
        say("")
        say(f"  Aither Learn: {origin}/learn")
        say("  Not signed in yet. Run: adk login  (your grown-up approves the code)")
        return 0
    client = LearnClient(origin, token, transport=transport)
    try:
        if getattr(args, "learn_action", None) == "play":
            return play_quest(client, ask or input, say)
        return cmd_learn_info(client, origin, say)
    finally:
        client.close()


__all__ = [
    "DEFAULT_LEARN_ORIGIN",
    "LearnClient",
    "cmd_learn",
    "learn_origin",
    "learn_token",
    "play_quest",
    "read_answer",
    "register_parser",
    "render_item",
]
