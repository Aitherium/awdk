"""adk intel -- contribute open-source intel to the community board from YOUR machine.

    adk intel scout --feeds feeds.txt [--once] [--interval 900] [--dry-run]
    adk intel scout --feeds https://example.org/rss.xml,https://example.com/atom
    adk intel schedule --feeds feeds.txt --every 15m

``scout`` reads the RSS/Atom feeds YOU list, politely (robots.txt, a minimum
interval per host, User-Agent ``AitherIntelScout/0.1``), keeps a link plus a short
plain-text headline and summary per item, and posts batches to
``/intel/contribute`` with your saved awdk credential (``adk login``). An item
reaches the public board only once a second, independent contributor sends the
same link. ``schedule`` registers the scout as an awrise wake.

Your provider keys (for future X/Reddit adapters) stay in your local keyring; see
``adk.intel.sources``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import httpx

from adk.intel import USER_AGENT
from adk.intel.fetch import DEFAULT_MIN_INTERVAL_S, FetchRefusedError, PoliteFetcher
from adk.intel.schema import Observation
from adk.intel.sources import SOURCES, Source

logger = logging.getLogger("adk.intel")

CONTRIBUTE_PATH = "/intel/contribute"
BATCH_SIZE = 50                 # the server's per-request cap
DEFAULT_INTERVAL_S = 900
WAKE_NAME = "intel-scout"
SENT_CAP = 5000                 # remembered keys (links) so a re-run does not resend


def contribute_url() -> str:
    """``<genesis data base>/intel/contribute`` -- direct override or the portal bridge."""
    from adk.control_plane import genesis_data_base
    return genesis_data_base().rstrip("/") + CONTRIBUTE_PATH


def _state_path() -> Path:
    return Path(os.environ.get("AITHER_INTEL_STATE")
                or Path.home() / ".aither" / "intel" / "sent.json")


def _load_sent(path: Path) -> List[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [str(x) for x in data][-SENT_CAP:] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_sent(path: Path, sent: List[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sent[-SENT_CAP:]), encoding="utf-8")
    except OSError as exc:
        logger.warning("could not save scout state %s: %s", path, exc)


def parse_feed_args(values: Iterable[str]) -> List[str]:
    """Feed URLs from ``--feeds`` values: each is a file (one URL per line, ``#``
    comments) or a comma/space separated URL list."""
    urls: List[str] = []
    for value in values:
        v = (value or "").strip()
        if not v:
            continue
        p = Path(v).expanduser()
        if not v.lower().startswith(("http://", "https://")) and p.is_file():
            lines = p.read_text(encoding="utf-8").splitlines()
            parts = [ln.split("#", 1)[0].strip() for ln in lines]
        else:
            parts = v.replace(",", " ").split()
        for u in parts:
            if u and u.lower().startswith(("http://", "https://")) and u not in urls:
                urls.append(u)
    return urls


def build_sources(feed_urls: Sequence[str]) -> List[Source]:
    return [SOURCES["rss"](u) for u in feed_urls]


def collect(sources: Sequence[Source], fetcher: PoliteFetcher) -> List[Observation]:
    """Every source's observations, de-duplicated by link. A failing source is
    logged and skipped; it never stops the others."""
    seen: set = set()
    out: List[Observation] = []
    for src in sources:
        try:
            items = src.collect(fetcher)
        except FetchRefusedError as exc:
            print(f"  skip {src.name}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - one bad feed must not stop the run
            print(f"  fail {src.name}: {exc}")
            continue
        for obs in items:
            if obs.source_url not in seen:
                seen.add(obs.source_url)
                out.append(obs)
    return out


def _headers() -> Dict[str, str]:
    from adk.pack_remote import auth_headers
    h = auth_headers()
    h["User-Agent"] = USER_AGENT
    return h


def upload(observations: Sequence[Observation], url: Optional[str] = None,
           client: Optional[httpx.Client] = None) -> Dict[str, int]:
    """POST in batches of BATCH_SIZE. Returns accepted/duplicate/promoted totals.

    Raises:
        PermissionError: no saved credential, or the server answered 401/403.
        RuntimeError: rate limited (429) or any other non-2xx.
    """
    headers = _headers()
    if "Authorization" not in headers:
        raise PermissionError("not signed in: run 'adk login' first")
    url = url or contribute_url()
    totals = {"accepted": 0, "duplicate": 0, "promoted": 0}
    own = client is None
    if own:
        from adk._tls import tls_verify
        client = httpx.Client(timeout=30.0, verify=tls_verify())
    try:
        for i in range(0, len(observations), BATCH_SIZE):
            batch = [o.to_payload() for o in observations[i:i + BATCH_SIZE]]
            resp = client.post(url, json={"items": batch}, headers=headers)
            if resp.status_code in (401, 403):
                raise PermissionError(f"the server refused the credential ({resp.status_code})")
            if resp.status_code == 429:
                raise RuntimeError("rate limited; retry after "
                                   f"{resp.headers.get('Retry-After', '?')} s")
            if resp.status_code >= 300:
                raise RuntimeError(f"contribute answered {resp.status_code}: {resp.text[:200]}")
            body = resp.json()
            totals["accepted"] += int(body.get("accepted", 0))
            totals["duplicate"] += int(body.get("duplicate", 0))
            totals["promoted"] += len(body.get("promoted") or [])
    finally:
        if own:
            client.close()
    return totals


def scout_once(feed_urls: Sequence[str], fetcher: PoliteFetcher, dry_run: bool = False,
               state_path: Optional[Path] = None) -> int:
    """One pass: collect, drop links already sent, upload. Returns an exit code."""
    state_path = state_path or _state_path()
    sent = _load_sent(state_path)
    already = set(sent)
    observations = [o for o in collect(build_sources(feed_urls), fetcher)
                    if o.source_url not in already]
    print(f"intel scout: {len(observations)} new item(s) from {len(feed_urls)} feed(s)")
    if dry_run or not observations:
        for o in observations[:20] if dry_run else []:
            print(f"  {o.published_at}  {o.title[:90]}  <{o.source_url}>")
        return 0
    try:
        totals = upload(observations)
    except PermissionError as exc:
        print(f"intel scout: {exc}")
        return 1
    except (RuntimeError, httpx.HTTPError) as exc:
        print(f"intel scout: upload failed: {exc}")
        return 1
    _save_sent(state_path, sent + [o.source_url for o in observations])
    print(f"intel scout: accepted {totals['accepted']}, already sent {totals['duplicate']}, "
          f"corroborated now {totals['promoted']}")
    return 0


def cmd_scout(args: Any) -> int:
    feed_urls = parse_feed_args(getattr(args, "feeds", None) or [])
    if not feed_urls:
        print("adk intel scout: give --feeds <file|url,url> (RSS or Atom feeds you choose)")
        return 2
    fetcher = PoliteFetcher(min_interval_s=float(getattr(args, "min_host_interval", None)
                                                 or DEFAULT_MIN_INTERVAL_S))
    dry = bool(getattr(args, "dry_run", False))
    if getattr(args, "once", False):
        return scout_once(feed_urls, fetcher, dry_run=dry)
    interval = max(60, int(getattr(args, "interval", None) or DEFAULT_INTERVAL_S))
    try:
        while True:
            scout_once(feed_urls, fetcher, dry_run=dry)
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def schedule_command(feeds: Sequence[str]) -> str:
    """The command the wake runs: absolute feed files, so the wake's cwd is irrelevant."""
    parts = []
    for f in feeds:
        p = Path(f).expanduser()
        parts.append(str(p.resolve()) if p.is_file() else f)
    quoted = " ".join(f'"{p}"' for p in parts)
    return f"adk intel scout --once --feeds {quoted}"


def cmd_schedule(args: Any) -> int:
    from adk import wakes
    feeds = list(getattr(args, "feeds", None) or [])
    if not parse_feed_args(feeds):
        print("adk intel schedule: give --feeds <file|url,url>")
        return 2
    every = str(getattr(args, "every", None) or "15m")
    binary = wakes.resolve_bin()
    if not binary:
        print("adk intel schedule: awrise is not installed (pip install awrise), "
              "or set AWRISE_BIN")
        return 1
    try:
        argv = wakes.build_add_argv(binary, WAKE_NAME, command=schedule_command(feeds),
                                    every=every, timeout=600)
    except ValueError as exc:
        print(f"adk intel schedule: {exc}")
        return 2
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=flags, check=False)
    print((proc.stdout or proc.stderr or "").strip()
          or f"registered wake {WAKE_NAME} every {every}")
    return proc.returncode


def cmd_intel(args: Any) -> int:
    sub = getattr(args, "intel_command", None)
    if sub == "scout":
        return cmd_scout(args)
    if sub == "schedule":
        return cmd_schedule(args)
    print("Usage: adk intel [scout|schedule]")
    return 1
