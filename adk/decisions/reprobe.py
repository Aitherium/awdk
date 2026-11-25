"""Re-probe: the check an ACTION card names, run when the owner answers it.

WHY (owner, 2026-11-23)
-----------------------
A card that says "switch the power cycle, then tell me" reached a dead end: the
owner clicked Done and nothing woke up to look again, so the card reported a
resolution the ask never got. The answer was recorded, the session was steered —
but the THING the session was waiting on (a device back online, a port listening,
a service healthy) was never re-measured.

So a card may name a CHECK. The name is a key in a host-local registry
(``~/.aither/decisions/checks.json``, override ``AITHER_DECISION_CHECKS``); the
card carries only the NAME, never a command, so a card JSON — writable by any
agent on the box — can never become a code-execution primitive. The registry is
the operator's, one hop away from whoever writes cards.

The run happens in the store's answer TRANSITION (``reprobe.run_and_record``),
exactly like ``card_recipes.apply_answer``: every answer path (window, phone,
chat DM, terminal reply, deadline) goes through it without knowing it exists.

Two facts are written back: a NOTE on the card (so every surface renders it) and
a STEER to the raising session (so the agent acts on the result instead of
re-asking). A failed check never un-answers the card — the answer is the owner's
word; the check is the measurement.

Stdlib only: this module ships inside ``awask``, which must import with nothing
but the standard library installed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

#: Answer keys that mean "it is done — look again". A re-probe runs only on one of
#: these, so an answer of "not yet" records and closes without probing.
DEFAULT_ON = ("done", "did_it", "fixed", "retry", "check_now")

#: How long a probe may take. A wedged device must not hold the answer's caller
#: open; the result is "no answer in N s", which is itself a measurement.
PROBE_TIMEOUT = float(os.getenv("AITHER_DECISION_PROBE_TIMEOUT", "10"))

#: A command probe is an argv list from the REGISTRY, never from a card.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: Loopback/tailnet hosts a probe may reach. A card naming a check cannot point
#: the platform's own fetch at an arbitrary internet host.
_PRIVATE_PREFIXES = ("127.", "10.", "192.168.", "172.16.", "172.17.", "172.18.",
                     "172.19.", "172.2", "172.30.", "172.31.", "100.64.", "localhost")


def registry_path() -> Path:
    override = (os.getenv("AITHER_DECISION_CHECKS") or "").strip()
    if override:
        return Path(override)
    base = os.getenv("AITHER_DECISIONS_DIR") or str(Path.home() / ".aither" / "decisions")
    return Path(base) / "checks.json"


def load_registry(path: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    """The named checks, or {} when the file is absent/unreadable.

    A missing registry is not an error: most cards name no check, and a card that
    names an unregistered one is reported on the card, never silently skipped.
    """
    target = path or registry_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    checks = raw.get("checks") if isinstance(raw, dict) else None
    return {str(k): v for k, v in (checks or {}).items() if isinstance(v, dict)}


def _host_allowed(url: str) -> bool:
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return bool(host) and host.startswith(_PRIVATE_PREFIXES)


def _http_probe(url: str, expect: str) -> dict[str, Any]:
    import urllib.error
    import urllib.request

    if not _host_allowed(url):
        return {"ok": False, "detail": f"refused: {url} is not a loopback/tailnet host"}
    started = time.time()
    try:
        req = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "aither-decisions-reprobe"})
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:  # noqa: S310
            body = resp.read(4096).decode("utf-8", "replace")
            status = int(getattr(resp, "status", 0) or 0)
    except urllib.error.HTTPError as exc:
        body = exc.read(4096).decode("utf-8", "replace") if exc.fp else ""
        status = int(exc.code or 0)
    except Exception as exc:  # noqa: BLE001 - a probe is a measurement, never a crash
        return {"ok": False, "status": 0,
                "detail": f"{exc.__class__.__name__}: {exc}",
                "ms": int((time.time() - started) * 1000)}
    ms = int((time.time() - started) * 1000)
    want = (expect or "2xx").strip().lower()
    if want in ("2xx", "ok", ""):
        ok = 200 <= status < 300
    elif want.isdigit():
        ok = status == int(want)
    elif want.startswith("contains:"):
        ok = status < 400 and want.split(":", 1)[1].strip().lower() in body.lower()
    else:
        ok = 200 <= status < 300
    return {"ok": ok, "status": status, "ms": ms,
            "detail": f"HTTP {status} in {ms} ms" + ("" if ok else f" (want {want})")}


def _command_probe(argv: list[Any], expect_exit: int) -> dict[str, Any]:
    args = [str(a) for a in argv if str(a)]
    if not args:
        return {"ok": False, "detail": "refused: empty argv"}
    binary = args[0]
    # An argv from the registry still has to resolve to a real file, and never
    # through a shell: a name that is not absolute is a path dependency this
    # module will not carry (the silent no-op class).
    if not os.path.isabs(binary) or not os.path.isfile(binary):
        return {"ok": False, "detail": f"refused: {binary!r} is not an absolute existing file"}
    started = time.time()
    try:
        done = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=PROBE_TIMEOUT,
                              creationflags=_CREATE_NO_WINDOW, shell=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": f"no exit within {int(PROBE_TIMEOUT)} s"}
    except OSError as exc:
        return {"ok": False, "detail": f"{exc.__class__.__name__}: {exc}"}
    ms = int((time.time() - started) * 1000)
    tail = " ".join((done.stdout or done.stderr or "").split())[-200:]
    ok = done.returncode == int(expect_exit)
    return {"ok": ok, "exit": done.returncode, "ms": ms,
            "detail": f"exit {done.returncode} in {ms} ms" + (f": {tail}" if tail else "")}


def run_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Run one registry entry. Returns {ok, detail, ...}; never raises."""
    kind = str(spec.get("kind") or "").strip().lower()
    try:
        if kind == "http":
            url = str(spec.get("url") or "").strip()
            if not url:
                return {"ok": False, "detail": "refused: http check has no url"}
            return _http_probe(url, str(spec.get("expect") or "2xx"))
        if kind == "command":
            argv = spec.get("argv")
            if not isinstance(argv, list) or not argv:
                return {"ok": False, "detail": "refused: command check has no argv"}
            return _command_probe(argv, int(spec.get("expect_exit") or 0))
        return {"ok": False, "detail": f"refused: unknown check kind {kind!r}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"{exc.__class__.__name__}: {exc}"}


def run_named(name: str, path: Optional[Path] = None) -> dict[str, Any]:
    """Look a check up by name and run it."""
    checks = load_registry(path)
    spec = checks.get((name or "").strip())
    if spec is None:
        return {"ok": False, "registered": False,
                "detail": f"no check named {name!r} in {registry_path()} — "
                          f"register it there to make the answer re-measure"}
    result = run_spec(spec)
    result["registered"] = True
    result["label"] = str(spec.get("label") or name)
    return result


def describe(name: str, result: dict[str, Any]) -> str:
    """One owner-readable line for the card."""
    label = str(result.get("label") or name)
    verdict = "PASS" if result.get("ok") else "FAIL"
    return f"Re-check '{label}' -> {verdict}: {result.get('detail') or 'no detail'}"


def run_and_record(store: Any, card: Any) -> Optional[str]:
    """Run the card's named check and write the result back. Returns the line.

    ``store`` is a DecisionStore; kept generic so the self-test can pass a stub.
    Never raises: a failed probe must not undo an answer already recorded.
    """
    spec = getattr(card, "action_check", None)
    if not isinstance(spec, dict):
        return None
    name = str(spec.get("name") or "").strip()
    if not name:
        return None
    on = spec.get("on") or DEFAULT_ON
    answer = str(getattr(card, "answer", "") or "").strip().lower()
    if answer not in {str(k).strip().lower() for k in on}:
        return None
    result = run_named(name)
    line = describe(name, result)
    try:
        from adk.decisions.store import DecisionNote  # noqa: F401  (public API check)

        store.add_facts(card.id, [line])
        store.add_note(card.id, line, via="reprobe")
    except AttributeError as exc:
        # An older store in another installed copy: the fact is the measurement
        # that matters; skip the note rather than lose both.
        try:
            store.add_facts(card.id, [line])
        except Exception as write_exc:  # noqa: BLE001
            print(f"[decisions] reprobe fact write failed for {card.id}: "
                  f"{write_exc!r} (after {exc!r})", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - the answer must survive a broken writer
        print(f"[decisions] reprobe note write failed for {card.id}: {exc!r}",
              file=sys.stderr)
    # Wake the raiser: the whole point is that a PASS resumes it and a FAIL tells
    # it to keep waiting rather than re-ask the owner.
    try:
        store.deliver_steer(
            card,
            f"The re-check you asked to run on this card has run: {line}. "
            "Act on that result now — do not re-ask the owner what it already answers.",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[decisions] reprobe steer failed for {card.id}: {exc!r}", file=sys.stderr)
    return line


# ── self-test ─────────────────────────────────────────────────────────────────


def _self_test() -> int:
    import tempfile

    failures = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal failures
        print(("ok   " if cond else "FAIL ") + name + (f" {detail}" if detail and not cond else ""))
        failures += 0 if cond else 1

    with tempfile.TemporaryDirectory() as tmp:
        reg = Path(tmp) / "checks.json"
        reg.write_text(json.dumps({"checks": {
            "always-exits-3": {
                "kind": "command",
                "argv": [sys.executable, "-c", "raise SystemExit(3)"],
                "expect_exit": 3, "label": "self-test command"},
            "always-fails": {
                "kind": "command",
                "argv": [sys.executable, "-c", "raise SystemExit(1)"],
                "expect_exit": 0},
            "bad-http": {"kind": "http", "url": "https://example.com/", "expect": "2xx"},
        }}), encoding="utf-8")
        check("a command check that exits as expected PASSES",
              run_named("always-exits-3", reg).get("ok") is True)
        check("a command check that exits wrong FAILS",
              run_named("always-fails", reg).get("ok") is False)
        check("an http check pointed off-host is REFUSED",
              run_named("bad-http", reg).get("ok") is False,
              str(run_named("bad-http", reg)))
        missing = run_named("nope", reg)
        check("an unregistered name says so and fails",
              missing.get("ok") is False and missing.get("registered") is False)
        check("describe names the verdict",
              describe("x", {"ok": True, "detail": "fine"}).startswith("Re-check 'x' -> PASS"))

        # run_and_record: only an affirmative answer probes.
        class StubStore:
            def __init__(self):
                self.facts, self.notes, self.steers = [], [], []

            def add_facts(self, _id, facts):
                self.facts.extend(facts)

            def add_note(self, _id, text, via=""):
                self.notes.append((text, via))

            def deliver_steer(self, _card, text):
                self.steers.append(text)

        class Card:
            id = "d-test"
            answer = "done"
            action_check = {"name": "always-exits-3"}

        s = StubStore()
        prev = os.environ.get("AITHER_DECISION_CHECKS")
        os.environ["AITHER_DECISION_CHECKS"] = str(reg)
        try:
            line = run_and_record(s, Card())
        finally:
            if prev is None:
                os.environ.pop("AITHER_DECISION_CHECKS", None)
            else:
                os.environ["AITHER_DECISION_CHECKS"] = prev
        check("an affirmative answer runs the check", bool(line) and "PASS" in (line or ""),
              str(line))
        check("the result lands as a fact", bool(s.facts))
        check("the result lands as a note", any(via == "reprobe" for _t, via in s.notes))
        check("the raiser is steered", bool(s.steers))

        class NotYet(Card):
            answer = "not_yet"

        s2 = StubStore()
        check("a non-affirmative answer does NOT probe", run_and_record(s2, NotYet()) is None)

    return 1 if failures else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(_self_test())
    if len(sys.argv) > 1:
        print(json.dumps(run_named(sys.argv[1]), indent=2))
        raise SystemExit(0)
    raise SystemExit("usage: python -m adk.decisions.reprobe <check-name> | --self-test")
