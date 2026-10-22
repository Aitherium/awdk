"""Judge the discovery index. Exit codes: 0 ok, 1 violation, 2 could not judge.

  CD001  a part of the index (user, or this cwd) is missing or older than max-age, or
         the refresh receipt (``last_refresh.json``, written by every full build) is
         missing, older than max-age or says the build failed -- i.e. nothing is
         refreshing the index. max-age defaults to 2x the hourly wake's cadence.
  CD002  a probe task returns no card that really matches it, misses a domain it
         must reach, or misses a card it is pinned to. A card counts only when it
         covers at least MIN_COVERAGE of the task's idf-weighted terms: per-domain
         normalisation lifts any domain's weak best match to ~0.6, so "shares one
         word" is not a hit.
  CD004  a source receipt says ok=false. Listed always; a violation when every
         source of a critical domain (tools: the gateway; skills: any skills source)
         is dead.
"""

from __future__ import annotations

import time
from typing import Optional

from adk.discovery import index, rank, sources

DEFAULT_MAX_AGE_S = 2 * index.REFRESH_EVERY_S
MIN_COVERAGE = 0.5
PIN_TOP = 3

#: (task, domains it must reach, card names that must be in the top PIN_TOP).
#: Generic: they hold on any awdk install with Claude Code skills. A checkout that
#: knows its own cards passes sharper probes (``check_session_discovery.py``).
DEFAULT_PROBES: tuple = (
    ("reverse engineer a desktop app", ("skills",), ()),
    ("search the web for a topic", ("tools",), ()),
)

_READ = object()


def parse_age(text: str) -> float:
    """``90s``, ``45m``, ``6h``, ``7d`` or a bare number of seconds."""
    t = str(text).strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(t[-1:], None)
    return float(t[:-1]) * mult if mult else float(t)


def _probe(task: str, want: tuple, pinned: tuple, merged: dict,
           current_fp: Optional[str]) -> "tuple[int, str]":
    got = rank.query(task, k=8, live=False, merged=merged, current_fp=current_fp)["cards"]
    real = [c for c in got if float(c.get("coverage") or 0) >= MIN_COVERAGE]
    domains = sorted({c["domain"] for c in real})
    missing = [d for d in want if d not in domains]
    top = [str(c["name"]) for c in real[:PIN_TOP]]
    absent = [n for n in pinned if n not in top]
    if not real:
        weak = ", ".join(f"{c['domain']}:{c['name']}({c.get('coverage', 0):.2f})"
                         for c in got[:3]) or "nothing"
        return 1, (f"CD002 probe {task!r}: no card covers >= {MIN_COVERAGE:.0%} of it "
                   f"(best: {weak})")
    if missing:
        return 1, f"CD002 probe {task!r} reached {domains}, missing {missing}"
    if absent:
        return 1, f"CD002 probe {task!r}: {absent} not in the top {PIN_TOP} ({top})"
    shown = ", ".join(f"{c['domain']}:{c['name']}" for c in real[:3])
    return 0, f"  probe {task!r}: {len(real)} matching cards ({shown})"


def check_index(cwd: Optional[str] = None, max_age_s: float = DEFAULT_MAX_AGE_S,
                probes: Optional[tuple] = None, merged: Optional[dict] = None,
                now: Optional[float] = None, refresh: object = _READ,
                current_fp: Optional[str] = None) -> "tuple[int, list]":
    """(rc, lines). ``refresh`` is the receipt dict (None = missing); by default it is
    read from disk, except when ``merged`` is passed and ``refresh`` is not (a fixture
    judges the index alone)."""
    t = time.time() if now is None else now
    from_disk = merged is None
    merged = merged if merged is not None else index.load_merged(cwd)
    if current_fp is None and not from_disk:
        current_fp = _fixture_fp(merged)
    parts = merged.get("parts") or {}
    lines: list = []
    bad = 0

    for part in ("user", "cwd"):
        meta = parts.get(part)
        if not meta:
            bad += 1
            lines.append(f"CD001 {part} index missing ({index.part_path(part, cwd)}) "
                         "-> adk discover build")
            continue
        # The older of build time and file mtime: a file restored from a backup or
        # touched back in time is as stale as its mtime says.
        stamps = [float(meta.get("built_at") or 0)]
        if meta.get("mtime"):
            stamps.append(float(meta["mtime"]))
        age = t - min(stamps)
        if age > max_age_s:
            bad += 1
            lines.append(f"CD001 {part} index is {age / 3600:.1f} h old "
                         f"(max {max_age_s / 3600:.1f} h) -> adk discover build")
        else:
            lines.append(f"  {part} index {age / 60:.0f} min old")

    if refresh is _READ and from_disk:
        refresh = index.read_receipt()
    if refresh is not _READ:
        bad += _judge_receipt(refresh, t, max_age_s, lines)

    dead: dict = {}
    for part, meta in parts.items():
        for sid, rec in ((meta or {}).get("sources") or {}).items():
            if isinstance(rec, dict) and not rec.get("ok"):
                dead[sid] = str(rec.get("error") or "ok=false")
                lines.append(f"  CD004 source {sid} ({part}) dead: {dead[sid][:160]}")
    present = {sid for meta in parts.values() for sid in ((meta or {}).get("sources") or {})}
    for domain, sids in sources.CRITICAL.items():
        known = [s for s in sids if s in present]
        if known and all(s in dead for s in known):
            bad += 1
            lines.append(f"CD004 every {domain} source is dead ({', '.join(known)})")

    counts: dict = {}
    for c in merged.get("cards") or []:
        counts[c.get("domain")] = counts.get(c.get("domain"), 0) + 1
    lines.append("  cards: " + ", ".join(f"{d}={counts.get(d, 0)}" for d in rank.DOMAINS))

    for probe in (DEFAULT_PROBES if probes is None else probes):
        task, want = probe[0], tuple(probe[1])
        pinned = tuple(probe[2]) if len(probe) > 2 else ()
        rc, line = _probe(task, want, pinned, merged, current_fp)
        bad += rc
        lines.append(line)
    return (1 if bad else 0), lines


def _judge_receipt(rec: object, t: float, max_age_s: float, lines: list) -> int:
    where = index.receipt_path()
    if not isinstance(rec, dict):
        lines.append(f"CD001 no refresh receipt ({where}): nothing is refreshing the index "
                     "-> schedule it hourly (awrise add --name discovery --every 1h --detach "
                     "--run \"adk discover build --all-cwds\") or run adk discover build")
        return 1
    age = t - float(rec.get("ts") or 0)
    if age > max_age_s:
        lines.append(f"CD001 last refresh {age / 3600:.1f} h ago (max {max_age_s / 3600:.1f} h)"
                     ": the refresh wake is not running")
        return 1
    if rec.get("exit_code") not in (0, "0"):
        lines.append(f"CD001 last refresh failed (exit {rec.get('exit_code')}): "
                     f"{rec.get('error') or rec.get('critical_dead')}")
        return 1
    lines.append(f"  last refresh {age / 60:.0f} min ago")
    return 0


def _fixture_fp(merged: dict) -> Optional[str]:
    """A fixture is judged as the caller it was built for (no bearer on disk)."""
    for meta in (merged.get("parts") or {}).values():
        caller = (meta or {}).get("caller")
        if isinstance(caller, dict) and caller.get("fingerprint"):
            return str(caller["fingerprint"])
    return None


__all__ = ["DEFAULT_MAX_AGE_S", "DEFAULT_PROBES", "MIN_COVERAGE", "check_index",
           "parse_age"]
