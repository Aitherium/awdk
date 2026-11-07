"""The ``adk home updates`` commands: wiring :mod:`adk.home.updates` to this home.

Kept apart from :mod:`adk.home.cli` so the seams (Home Assistant client, self_update,
the command runner, the clocks) can be handed in by a test. Exit codes: 0 ok, 1 an
approved run failed, 2 bad setup / arguments / a state file that cannot be read.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import updates as up

EXIT_OK, EXIT_FAIL, EXIT_SETUP = 0, 1, 2
#: How this process names itself on the locks it takes.
LOCK_HOLDER = "adk home updates"


def _default_client() -> Any:
    from .device_tools import ha_url, read_token
    from .devices.ha import HAClient

    return HAClient(ha_url(), read_token) if ha_url() else None


class Home:
    """This home's update seams: files, receipts, the device gate."""

    def __init__(self, root: Optional[Path] = None, *, client: Any = "default",
                 su: Any = None, runner: up.Runner = up.run_command,
                 platform: Optional[str] = None, clock: Callable[[], float] = time.time,
                 now_local: Callable[[], datetime] = up._now_local,
                 euid: Optional[int] = None, upgrader: up.UpgradeRunner = up.run_no_kill):
        from .config import home_dir
        from .serve import receipts_path

        self.root = root or home_dir()
        self.client = _default_client() if client == "default" else client
        self.su = su
        self.runner = runner
        self.platform = platform
        self.clock = clock
        self.now_local = now_local
        self.euid = euid
        self.upgrader = upgrader
        self.receipts = receipts_path(self.root)
        self.state_path = up.state_path(self.root)

    def receipt(self, kind: str, name: str, args: Any, result: Any, approval: str) -> None:
        from adk import receipts

        receipts.append(kind, name, args=args, result=result, approval=approval,
                        path=self.receipts)

    def gate(self) -> Any:
        if self.client is None:
            return None
        from .devices.gate import DeviceGate

        return DeviceGate(self.client, household=up.HOUSEHOLD, receipt=self.receipt,
                          clock=self.clock)

    def device_store_path(self) -> Path:
        from .device_tools import APPROVALS_NAME

        return self.root / APPROVALS_NAME

    def load_device_store(self) -> Dict[str, Any]:
        """The device approval store, malformed rows dropped (and warned about)."""
        try:
            data = json.loads(self.device_store_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"approvals": {}}
        except ValueError as exc:
            raise ValueError(f"{self.device_store_path()} is unreadable: {exc}") from exc
        if not isinstance(data, dict):
            return {"approvals": {}}
        return {**data, "approvals": up.valid_rows(data.get("approvals"),
                                                   str(self.device_store_path()))}

    def save_device_store(self, store: Dict[str, Any]) -> None:
        """Write it, never moving a row the file holds further along back down (the
        Hearth may have answered a card while this ran)."""
        from .._private_file import write_private_text

        try:
            disk = self.load_device_store()["approvals"]
        except ValueError:
            disk = {}
        rows = up.merge_rows(dict(store.get("approvals") or {}), disk)
        store["approvals"] = rows
        write_private_text(self.device_store_path(), json.dumps(store, default=str))

    def policy(self) -> Dict[str, Any]:
        from .device_tools import load_policy

        return load_policy(self.root)

    def raw_policy(self) -> Optional[Dict[str, Any]]:
        """The household's own policy file as written (None when there is none)."""
        from .device_tools import POLICY_NAME

        try:
            data = json.loads((self.root / POLICY_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def lock(self) -> Any:
        """One updates run at a time on this home (load -> vote -> claim -> save)."""
        from .filelock import run_lock

        return run_lock(self.state_path, holder=LOCK_HOLDER, timeout=30.0)

    def device_lock(self) -> Any:
        """The lock `adk home serve` takes on the device approvals: a guardian's vote
        there and a card raised here never overwrite each other."""
        from .filelock import run_lock

        return run_lock(self.device_store_path(), holder=LOCK_HOLDER, timeout=30.0)

    async def check(self, args: argparse.Namespace) -> Dict[str, Any]:
        return await up.check_all(client=self.client, host=not args.no_host,
                                  ha=not args.no_ha, su=self.su, runner=self.runner,
                                  platform=self.platform)

    async def act(self, report: Dict[str, Any], state: Dict[str, Any], *,
                  adk_direct: bool) -> Dict[str, Any]:
        with self.device_lock():
            store = self.load_device_store()
            out = await up.raise_cards(report, state=state, gate=self.gate(),
                                       cfg=self.policy(), device_store=store,
                                       receipt=self.receipt, now=self.clock(), su=self.su,
                                       adk_direct=adk_direct, raw_cfg=self.raw_policy())
            if "ha" in out:
                self.save_device_store(store)
        return out


# ── text ─────────────────────────────────────────────────────────────────────

def report_lines(report: Dict[str, Any]) -> List[str]:
    lines = []
    adk = report.get("adk") or {}
    if adk.get("pending"):
        how = "validated" if adk.get("adoptable") else f"NOT adoptable: {adk.get('why')}"
        lines.append(f"adk:   update ready at {adk.get('installed_at')} ({how})")
    elif not adk.get("ok", True):
        lines.append(f"adk:   could not check ({adk.get('error')})")
    else:
        lines.append("adk:   current")
    host = report.get("host") or {}
    if host.get("skipped"):
        lines.append("host:  skipped")
    elif not host.get("ok"):
        lines.append(f"host:  {host.get('platform')}: could not check ({host.get('error')})")
    else:
        note = f" ({host['note']})" if host.get("note") else ""
        lines.append(f"host:  {host['platform']}: {host['count']} pending{note}")
        for u in (host.get("updates") or [])[:15]:
            name = u.get("name") or u.get("title") or u.get("label")
            ver = u.get("version") or u.get("kb") or ""
            lines.append(f"         {name} {ver}".rstrip())
    ha = report.get("ha") or {}
    if ha.get("skipped"):
        lines.append("ha:    skipped")
    elif not ha.get("configured"):
        lines.append("ha:    not configured (AITHER_HA_URL)")
    elif not ha.get("ok"):
        lines.append(f"ha:    could not check ({ha.get('error')})")
    else:
        lines.append(f"ha:    {len(ha['pending'])} of {len(ha['entities'])} update "
                     "entities pending")
        for e in ha["pending"]:
            lines.append(f"         {e['entity_id']}: {e['installed_version']} -> "
                         f"{e['latest_version']}")
    return _clean_lines(lines)


def action_lines(out: Dict[str, Any]) -> List[str]:
    lines = []
    if "adk" in out:
        a = out["adk"]
        lines.append(f"adk:   {a.get('status')} {a.get('why') or a.get('note') or ''}".rstrip())
        if a.get("approval"):
            lines.append(f"         card {a['approval']['id']}: {a['summary']}")
    for e in out.get("ha") or []:
        appr = (e.get("approval") or {}).get("id")
        tail = f" card {appr}" if appr else (f" {e.get('error') or e.get('note') or ''}")
        lines.append(f"ha:    {e['entity_id']}: {e.get('status') or 'refused'}{tail}".rstrip())
    if "host" in out:
        h = out["host"]
        appr = (h.get("approval") or {}).get("id")
        lines.append(f"host:  {h['status']}" + (f" card {appr}" if appr else "")
                     + f": {h['summary']}")
    if not lines:
        lines.append("nothing to do")
    if any("card" in ln for ln in lines):
        lines.append("Answer a card: adk home updates apply --approve ID  (or --deny ID)")
    return _clean_lines(lines)


def _emit(data: Any, as_json: bool, lines: List[str]) -> None:
    """JSON escapes control characters itself; text lines are cleaned before print."""
    print(json.dumps(data, indent=2, default=str) if as_json
          else "\n".join(_clean_lines(lines)))


def _clean_lines(lines: List[str]) -> List[str]:
    return [ln[: len(ln) - len(ln.lstrip(" "))] + up.clean(ln, 600) for ln in lines]


# ── commands ─────────────────────────────────────────────────────────────────

async def cmd_check(home: Home, args: argparse.Namespace) -> int:
    report = await home.check(args)
    _emit(report, args.json, report_lines(report))
    return EXIT_OK


async def _answer(home: Home, args: argparse.Namespace, state: Dict[str, Any]) -> int:
    aid = args.approve or args.deny
    allow = bool(args.approve)
    if up.ap.get(state, aid) is not None:
        res = await up.decide_card(
            state, aid, allow, receipt=home.receipt, now=home.clock(),
            platform=home.platform, su=home.su, euid=home.euid, upgrader=home.upgrader,
            persist=lambda: up.save_state(home.state_path, state))
        up.save_state(home.state_path, state)
    else:
        with home.device_lock():
            store = home.load_device_store()
            gate = home.gate()
            if gate is None or up.ap.get(store, aid) is None:
                print(f"no such update request: {aid}")
                return EXIT_SETUP
            if (up.ap.get(store, aid).get("action") or {}).get("domain") != "update":
                print(f"{aid} is not an update request (answer it where it was asked)")
                return EXIT_SETUP
            res = await gate.vote(up.OWNER, home.policy(), store, aid, allow,
                                  guardians=[up.OWNER["pid"]],
                                  persist=lambda: home.save_device_store(store))
            home.save_device_store(store)
    if res.get("error"):
        _emit(res, args.json, [f"refused: {res['error']}"])
        return EXIT_SETUP
    result = res.get("result") or {}
    if result.get("state") == "still_running":
        _emit(res, args.json, [f"approved; {result.get('note')}. Log: {result.get('log')}"])
        return EXIT_FAIL
    if result.get("instructions"):
        line = f"approved. Nothing was installed here: {result['instructions']}"
    elif res.get("ran"):
        line = "approved and installed"
    elif res.get("ok") and not allow:
        line = "denied; nothing ran"
    else:
        err = result.get("error") or (res.get("approval") or {}).get("status")
        _emit(res, args.json, [f"approved, but it did not complete: {err}"])
        return EXIT_FAIL
    _emit(res, args.json, [line])
    return EXIT_OK


async def cmd_apply(home: Home, args: argparse.Namespace) -> int:
    state = up.load_state(home.state_path)
    if args.approve or args.deny:
        return await _answer(home, args, state)
    report = await home.check(args)
    out = await home.act(report, state, adk_direct=True)
    up.save_state(home.state_path, state)
    _emit({"report": report, "actions": out}, args.json, action_lines(out))
    return EXIT_OK


async def cmd_schedule(home: Home, args: argparse.Namespace) -> int:
    state = up.load_state(home.state_path)
    sched = dict(state["schedule"])
    changed = bool(args.day or args.at or args.off or args.on)
    if args.day:
        sched["weekday"] = up.parse_weekday(args.day)
    if args.at:
        sched["at"] = args.at
    if args.off or args.on:
        sched["enabled"] = bool(args.on)
    state["schedule"] = up.validate_schedule(sched)
    now = home.now_local()
    out: Dict[str, Any] = {"schedule": state["schedule"], "last_run": state["last_run"]}
    lines = []
    if args.run_due:
        if up.is_due(state["schedule"], now, state["last_run"]):
            report = await home.check(args)
            from adk import self_update as default_su

            su = home.su or default_su
            # adk restarts on its own only where its owner opted in (adk autoupdate on).
            acted = await home.act(report, state, adk_direct=bool(su.auto_enabled()))
            state["last_run"] = up._iso(now)
            state["last_summary"] = {
                "adk": bool((report.get("adk") or {}).get("pending")),
                "host": (report.get("host") or {}).get("count"),
                "ha": len((report.get("ha") or {}).get("pending") or [])}
            out.update(ran=True, report=report, actions=acted, last_run=state["last_run"])
            lines.extend(action_lines(acted))
        else:
            out["ran"] = False
            lines.append("not due: the last run was after this week's window")
    if changed or args.run_due:
        up.save_state(home.state_path, state)
    s = state["schedule"]
    day = up.WEEKDAYS[s["weekday"]].capitalize()
    when = up._iso(up.next_window(s, now)) if s["enabled"] else "never (paused)"
    out["next_window"] = when
    lines.insert(0, f"updates window: {day} {s['at']} local "
                    f"({'on' if s['enabled'] else 'off'}); next: {when}")
    _emit(out, args.json, lines)
    return EXIT_OK


async def run(args: argparse.Namespace, home: Optional[Home] = None) -> int:
    sub = getattr(args, "updates_command", None) or "check"
    for flag in ("no_host", "no_ha", "json"):
        if not hasattr(args, flag):
            setattr(args, flag, False)
    try:
        home = home or Home()
        fn = {"check": cmd_check, "apply": cmd_apply, "schedule": cmd_schedule}.get(sub)
        if fn is None:
            print(f"unknown updates command: {sub} (check | apply | schedule)")
            return EXIT_SETUP
        if sub == "check":
            return await fn(home, args)
        with home.lock():
            return await fn(home, args)
    except RuntimeError as exc:
        print(f"updates {sub}: {exc}")
        return EXIT_SETUP
    except ValueError as exc:
        print(f"updates {sub}: {exc}")
        return EXIT_SETUP
