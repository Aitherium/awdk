"""ContextWell — ambient context computed in the background, drawn from in O(1).

THIS IS A CONSOLIDATION, NOT A NEW ENGINE. READ THIS BEFORE ADDING TO IT.
------------------------------------------------------------------------
The platform already has this idea, several times over, and all of it is good:

  * ``lib/core/AitherGraph.py`` — ``context_for(agent_id, intent, message, max_tokens)``
    is literally "one-call context assembly for agents", and its own docstring says it
    "Replaces ContextPipeline stages 3.5 + 5.5". ``system_briefing()`` is the
    pre-rendered summary that "replaces 10+ HTTP calls with a single in-process query".
  * ``lib/core/ContextPipeline.py`` — the 17-stage assembly (classify → neuron scaling →
    warm cache → Flux state → Will/Spirit/Affect → neuron fan-out → knowledge graph →
    MemoryBus → conv recall → tenant RAG → RLM refinement → weed → surgical eviction →
    quality score).
  * ``AitherContextAssembler``, ``jarvis_brain.py``, ``AitherSpirit``/persona — the
    affect and identity half of the same picture.

So this module does NOT invent a sixth context system. It does two things the existing
ones structurally cannot:

  1. **It speaks their vocabulary.** :meth:`ContextWell.render_context` emits the SAME
     tagged sections ``AitherGraph.context_for`` emits — ``[SYSTEM] [SERVICES] [ALERTS]
     [CODE] [GRAPH_MEMORY]`` — so anything already consuming that format keeps working,
     and adds host-only sections (``[REPO] [LEASES] [ROOMS]``) that a process inside a
     container cannot see.
  2. **It survives the fleet.** Every source here is a plain host resource. When the
     fleet IS up, the fleet half is FETCHED from the existing engine
     (``GET /graphs/unified/briefing`` → ``AitherGraph.system_briefing()``), never
     reimplemented. The well is a projection and a cache, not a rival.

WHAT IT ADDS ON ITS OWN
-----------------------
An agent starting work in a repo needs the same handful of facts every time: what branch
am I on, what is already changed, who else is editing these files, what was I doing last,
what is stuck. Every agent recomputes that from scratch at the top of every session —
shelling out to git, listing sessions, guessing — and pays it again on every turn. The
daemon computes it CONTINUOUSLY on a background thread and serves the last good snapshot
instantly. Agents draw from the well; they do not dig one.

HOST TIER FIRST, ON PURPOSE
---------------------------
Measured while writing this: Docker was wedged (API 500, 0 containers) and the entire
container-side cognition plane — ContextPipeline, AitherGraph, the neuron cache — was
unreachable, while this daemon answered in milliseconds. Fleet sources are an ENRICHMENT
that raises the tier; they are never a dependency, because the moment you most need to
know what your agents were doing is the moment the fleet is broken.

DEGRADES BY NAMING WHAT IT LOST
-------------------------------
Every snapshot carries a per-source status. A well that quietly returns less when git is
slow or the lease store is missing is worse than one that errors: the caller cannot tell
"nothing is happening" from "I could not look", and that ambiguity is the exact failure
class this whole subsystem exists to end. `sources` is part of the contract, not debug.

Blocking I/O and the event loop
-------------------------------
Everything here is blocking I/O — subprocess, file reads, directory walks. It runs on a
dedicated thread and the HTTP handler only ever reads an already-built dict. A blocking
call on the event loop is not "slow", it is an outage for every concurrent request for
its full duration.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from adk.harnesses.rooms import RoomRegistry, default_registry

#: How often the background thread rebuilds. Cheap enough to be fresh, slow enough that
#: a dozen agents polling it never costs more than one rebuild.
REFRESH_INTERVAL = 5.0

#: A git call that has not answered in this long is reported as degraded rather than
#: waited on. A wedged git (index.lock held by a peer session) must not stall the well.
GIT_TIMEOUT = 5.0

#: Cap on any list we return, so one enormous repo state cannot make the snapshot
#: expensive to serialise on every read.
MAX_ITEMS = 40


def _lease_store_path() -> Path:
    """Mirror awgit.data_root.vcs_data_root() WITHOUT importing awgit.

    awdk ships to PyPI and must not depend on the monorepo. The resolution order
    is copied deliberately and kept narrow: env override, then the documented default.
    If awgit ever moves its store, this degrades to "unavailable" and SAYS so, which is
    the correct failure — silently reporting zero leases would tell every agent the
    coast is clear while a peer is mid-edit.
    """
    override = os.environ.get("VCS_DATA_ROOT")
    if override:
        return Path(override) / "leases.json"
    return Path.home() / ".aither" / "awgit" / "data" / "leases.json"


def _parse_iso(value: str) -> Optional[float]:
    """ISO-8601 (with the store's ``+00:00`` offset) -> epoch seconds, or None.

    Returns None rather than a guess: a lease whose expiry cannot be read is neither
    provably live nor provably dead, and picking either would be an invention.
    """
    if not value:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _run_git(args: List[str], cwd: str) -> Optional[str]:
    """Run one git command. None on any failure — the caller records the degradation."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def git_state(cwd: str) -> Dict[str, Any]:
    """Branch, head, dirty paths and recent commits for one working tree."""
    if not cwd or not Path(cwd).is_dir():
        return {"ok": False, "reason": f"not a directory: {cwd!r}"}

    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    if branch is None:
        return {"ok": False, "reason": "git unavailable, not a repo, or timed out"}

    head = _run_git(["rev-parse", "--short", "HEAD"], cwd) or ""
    porcelain = _run_git(["status", "--porcelain"], cwd) or ""
    log = _run_git(["log", "--oneline", "-8"], cwd) or ""

    dirty = [ln[3:] for ln in porcelain.splitlines() if ln.strip()]
    return {
        "ok": True,
        "branch": branch,
        "head": head,
        "dirty_count": len(dirty),
        # The FULL count is above; this list is capped. Reporting a truncated list as
        # if it were everything is how "only 40 files changed" becomes a wrong premise.
        "dirty_sample": dirty[:MAX_ITEMS],
        "dirty_truncated": len(dirty) > MAX_ITEMS,
        "recent_commits": log.splitlines()[:8],
    }


def lease_state() -> Dict[str, Any]:
    """Who is holding which files right now, from the awgit store.

    This is the single most useful thing one agent can know about another: it answers
    "is someone else in this file" BEFORE the edit rather than after the collision.
    """
    path = _lease_store_path()
    if not path.is_file():
        return {"ok": False, "reason": f"no lease store at {path}"}
    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {"ok": False, "reason": f"lease store unreadable: {exc}"}

    entries = raw.get("leases") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return {"ok": False, "reason": "lease store has an unexpected shape"}

    # Field names are READ FROM THE STORE, not guessed: lease_id / target /
    # expires_ts / status. The first draft assumed id/path/until/state, which meant
    # the status filter matched nothing and the path rendered blank — and because the
    # filter defaulted to "active", EVERY record passed, expired ones included. The
    # store held 644 records of which almost none were live, so the well was about to
    # tell every agent that 644 files were locked. A confidently wrong answer is worse
    # than an unavailable one: an agent that believes a file is contended backs off
    # from work nobody is doing.
    now = time.time()
    active: List[Dict[str, Any]] = []
    expired = 0
    unparsable = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("status", "")).lower() != "active":
            expired += 1
            continue
        expires = _parse_iso(str(entry.get("expires_ts", "")))
        if expires is None:
            # Cannot tell whether it is live. Counted, never assumed live OR dead.
            unparsable += 1
            continue
        if expires <= now:
            expired += 1
            continue
        active.append({
            "lease_id": entry.get("lease_id", ""),
            "actor": entry.get("actor", ""),
            "target": entry.get("target", ""),
            "expires_ts": entry.get("expires_ts", ""),
            "reason": entry.get("reason", ""),
        })
    return {
        "ok": True,
        "count": len(active),
        "leases": active[:MAX_ITEMS],
        # Reported so "0 active out of 644 records" reads as a swept store rather
        # than a broken reader.
        "expired_or_released": expired,
        "unparsable": unparsable,
        "checked_at": now,
    }


def fleet_briefing(timeout: float = 3.0) -> Dict[str, Any]:
    """Fetch the FLEET half from the engine that already owns it.

    ``GET /graphs/unified/briefing`` returns ``AitherGraph.system_briefing()`` — the
    same in-process summary that "replaces 10+ HTTP calls". Calling it is the whole
    point: the fleet's system/services/alerts view is not reimplemented here, it is
    borrowed, so there is exactly one definition of it and the well cannot drift from
    the platform's own answer.

    A short timeout and an honest failure, because this runs on a schedule against a
    fleet that is frequently down. Genesis on the host is plain HTTP via the nginx LB;
    in-network it is TLS, which is why this is host-side only.
    """
    base = os.environ.get("AITHER_GENESIS_URL", "http://localhost:8001").rstrip("/")
    url = f"{base}/graphs/unified/briefing"
    try:
        import urllib.request

        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001 - any failure is "fleet unavailable"
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    text = (body or {}).get("briefing") or ""
    if not text or "not available" in text.lower() or "no data" in text.lower():
        return {"ok": False, "reason": f"briefing empty or unavailable: {text[:80]!r}"}
    return {"ok": True, "briefing": text}


class ContextWell:
    """Builds and serves the ambient snapshot."""

    def __init__(
        self,
        registry: Optional[RoomRegistry] = None,
        session_lister=None,
        interval: float = REFRESH_INTERVAL,
    ) -> None:
        self.registry = registry or default_registry()
        self._session_lister = session_lister
        self.interval = interval
        self._lock = threading.Lock()
        self._snapshot: Dict[str, Any] = {"ready": False}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.builds = 0
        self.last_build_ms = 0.0
        #: cwd -> git state, so a well serving several repos does not rebuild all of
        #: them for one caller.
        self._roots: Dict[str, Dict[str, Any]] = {}

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="aeon-well", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.rebuild()
            except Exception as exc:  # noqa: BLE001
                # If this thread dies the well silently freezes at its last snapshot
                # and every agent keeps drinking stale context without being told.
                sys.stderr.write(f"[aeon-well] rebuild failed: {exc}\n")
            self._stop.wait(self.interval)

    # ── building ────────────────────────────────────────────────────────────

    def known_roots(self) -> List[str]:
        """Working trees worth tracking: whatever the live sessions are sitting in."""
        roots: List[str] = []
        env_root = os.environ.get("AITHER_WELL_ROOTS", "")
        for part in env_root.split(os.pathsep):
            if part.strip():
                roots.append(part.strip())
        if self._session_lister:
            try:
                for session in self._session_lister():
                    cwd = getattr(session, "cwd", "") or (
                        session.get("cwd", "") if isinstance(session, dict) else ""
                    )
                    if cwd and cwd not in roots:
                        roots.append(cwd)
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write(f"[aeon-well] session discovery failed: {exc}\n")
        return roots[:8]

    def rebuild(self) -> Dict[str, Any]:
        started = time.time()
        sources: Dict[str, str] = {}

        roots = self.known_roots()
        repos: Dict[str, Any] = {}
        for root in roots:
            state = git_state(root)
            repos[root] = state
            if not state.get("ok"):
                sources[f"git:{root}"] = state.get("reason", "unavailable")
        sources["git"] = "ok" if roots and any(r.get("ok") for r in repos.values()) else (
            "no working trees discovered" if not roots else "degraded"
        )

        leases = lease_state()
        sources["leases"] = "ok" if leases.get("ok") else leases.get("reason", "unavailable")

        rooms: List[Dict[str, Any]] = []
        try:
            rooms = self.registry.list_rooms()
            sources["rooms"] = "ok"
        except Exception as exc:  # noqa: BLE001
            sources["rooms"] = f"unavailable: {exc}"

        # Fleet enrichment: borrow the platform's own answer rather than compute a
        # second one. Off by default per-call cost is a 3s timeout against a fleet that
        # is often down, so it is skipped entirely when explicitly disabled.
        fleet: Dict[str, Any] = {"ok": False, "reason": "disabled"}
        if os.environ.get("AITHER_WELL_FLEET", "1").lower() not in ("0", "false", "no"):
            fleet = fleet_briefing()
        sources["fleet"] = "ok" if fleet.get("ok") else fleet.get("reason", "unavailable")

        snapshot = {
            "ready": True,
            "built_at": started,
            # host+fleet only when the fleet actually answered. Claiming a tier you did
            # not reach is how a consumer renders a confident, empty briefing.
            "tier": "host+fleet" if fleet.get("ok") else "host",
            "fleet": fleet,
            # The contract: every consumer can see which sources answered. A caller
            # that renders the well without this cannot distinguish a quiet platform
            # from a broken probe.
            "sources": sources,
            "repos": repos,
            "leases": leases,
            "rooms": rooms,
        }

        with self._lock:
            self._snapshot = snapshot
            self.builds += 1
            self.last_build_ms = (time.time() - started) * 1000.0
        return snapshot

    # ── reading ─────────────────────────────────────────────────────────────

    def draw(self, cwd: str = "", actor: str = "") -> Dict[str, Any]:
        """O(1) read of the last good snapshot, focused for one caller.

        Never rebuilds inline. A caller that arrives before the first build gets
        ``ready: false`` and is told to retry, rather than paying for a build on the
        request path — which would reintroduce exactly the stall this exists to remove.
        """
        with self._lock:
            snap = dict(self._snapshot)
            builds = self.builds
            build_ms = self.last_build_ms

        if not snap.get("ready"):
            return {
                "ready": False,
                "reason": "well has not completed its first build yet",
                "tier": "host",
            }

        out: Dict[str, Any] = {
            "ready": True,
            "tier": snap.get("tier", "host"),
            "built_at": snap.get("built_at"),
            "age_seconds": round(time.time() - float(snap.get("built_at") or 0), 2),
            "sources": snap.get("sources", {}),
            "builds": builds,
            "last_build_ms": round(build_ms, 1),
        }

        repos = snap.get("repos", {})
        if cwd:
            match = repos.get(cwd)
            if match is None:
                # Longest-prefix: an agent working in a subdirectory still gets its repo.
                best = ""
                for root in repos:
                    if cwd.replace("\\", "/").lower().startswith(
                        root.replace("\\", "/").lower()
                    ) and len(root) > len(best):
                        best = root
                match = repos.get(best) if best else None
            out["repo"] = match or {"ok": False, "reason": f"no tracked repo for {cwd!r}"}
        else:
            out["repos"] = repos

        leases = snap.get("leases", {})
        out["leases"] = leases
        if actor and leases.get("ok"):
            held = [i for i in leases.get("leases", []) if i.get("actor") == actor]
            others = [i for i in leases.get("leases", []) if i.get("actor") != actor]
            out["your_leases"] = held
            # The question an agent actually asks: is someone ELSE in my files.
            out["contended_by_others"] = others

        out["rooms"] = [
            {"id": r.get("id"), "last_seq": r.get("last_seq"), "pillars": r.get("pillars")}
            for r in snap.get("rooms", [])
        ]
        return out

    def render_context(self, cwd: str = "", actor: str = "", max_chars: int = 4000) -> str:
        """Render the well in ``AitherGraph.context_for``'s tagged-section format.

        SAME VOCABULARY ON PURPOSE. ``context_for`` emits ``[SYSTEM] [SERVICES]
        [ALERTS] [CODE] [GRAPH_MEMORY]``; anything already parsing or prompting on that
        shape keeps working, and the sections a container cannot see — the working tree,
        who holds which file, what the agents are actually doing — are added alongside
        rather than in a second format nobody else reads.

        Returns '' when the well is not ready. An empty string is a correct, checkable
        answer; a fabricated section is not.
        """
        snap = self.draw(cwd=cwd, actor=actor)
        if not snap.get("ready"):
            return ""

        parts: List[str] = []

        def add(tag: str, body: str) -> None:
            body = (body or "").strip()
            if body:
                parts.append(f"[{tag}]\n{body}\n[/{tag}]")

        # Fleet half, borrowed verbatim from AitherGraph.system_briefing().
        fleet = (self._snapshot or {}).get("fleet") or {}
        if fleet.get("ok"):
            add("SYSTEM", str(fleet.get("briefing", ""))[:1200])
        else:
            # Say the fleet is missing rather than omit the section silently — an
            # absent [SYSTEM] reads as "the system is fine".
            add("SYSTEM", f"fleet briefing unavailable ({fleet.get('reason', 'unknown')})")

        repo = snap.get("repo") or {}
        if repo.get("ok"):
            lines = [
                f"branch {repo.get('branch')} @ {repo.get('head')}",
                f"{repo.get('dirty_count', 0)} uncommitted path(s)"
                + (" (sample truncated)" if repo.get("dirty_truncated") else ""),
            ]
            recent = repo.get("recent_commits") or []
            if recent:
                lines.append("recent: " + " | ".join(recent[:4]))
            add("REPO", "\n".join(lines))
        elif repo:
            add("REPO", f"unavailable: {repo.get('reason', 'unknown')}")

        leases = snap.get("leases") or {}
        if leases.get("ok"):
            others = snap.get("contended_by_others")
            if others is None:
                others = leases.get("leases", [])
            if others:
                held = "\n".join(
                    f"{item.get('target')} — held by {item.get('actor')}" for item in others[:10]
                )
                add("LEASES", f"{len(others)} file(s) held by other agents:\n{held}")
            else:
                add("LEASES", "no files held by other agents")
        else:
            add("LEASES", f"lease store unavailable: {leases.get('reason', 'unknown')}")

        rooms = snap.get("rooms") or []
        if rooms:
            lines = []
            for room in rooms[:3]:
                pillars = room.get("pillars") or {}
                busy = ", ".join(f"{k} {v}" for k, v in pillars.items() if v)
                lines.append(f"{room.get('id')}: seq {room.get('last_seq')}"
                             + (f" — {busy}" if busy else " — quiet"))
            add("ROOMS", "\n".join(lines))

        add("WELL", f"tier {snap.get('tier')} · age {snap.get('age_seconds')}s · "
                    f"sources " + ", ".join(f"{k}={v}" for k, v in
                                            (snap.get("sources") or {}).items()))

        rendered = "\n".join(parts)
        return rendered if len(rendered) <= max_chars else rendered[: max_chars - 1] + "…"

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            ready = bool(self._snapshot.get("ready"))
        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "ready": ready,
            "builds": self.builds,
            "last_build_ms": round(self.last_build_ms, 1),
            "interval": self.interval,
        }


_well: Optional[ContextWell] = None
_well_lock = threading.Lock()


def default_well(session_lister=None) -> ContextWell:
    global _well
    with _well_lock:
        if _well is None:
            _well = ContextWell(session_lister=session_lister)
        return _well
