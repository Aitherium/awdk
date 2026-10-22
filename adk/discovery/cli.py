"""``adk discover`` -- build, query and check the session discovery index.

    adk discover build [--detach] [--force] [--cwd DIR] [--all-cwds]
    adk discover query "<task>" [--k 8] [--json] [--no-live]
    adk discover check [--max-age 2h] [--probe "<task>" --expect-domains tools,skills]
    adk discover --self-test

Exit codes: 0 ok, 1 stale / empty / missing domain / dead critical source / build
failed, 2 could not judge (bad arguments, unreadable index directory).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from adk.discovery import check as check_mod
from adk.discovery import index, rank, scope


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="adk discover", description=__doc__.splitlines()[0])
    ap.add_argument("--self-test", action="store_true", help="prove every check can fail")
    sub = ap.add_subparsers(dest="verb")
    b = sub.add_parser("build", help="rebuild the index for this directory and user")
    b.add_argument("--detach", action="store_true", help="start the build and return at once")
    b.add_argument("--force", action="store_true", help="ignore source TTLs")
    b.add_argument("--cwd", default=None)
    b.add_argument("--all-cwds", action="store_true",
                   help="also refresh every directory that already has a cwd part")
    q = sub.add_parser("query", help="ranked cards for a task")
    q.add_argument("task")
    q.add_argument("--k", type=int, default=8)
    q.add_argument("--json", action="store_true")
    q.add_argument("--no-live", action="store_true", help="skip the gateway's live search")
    q.add_argument("--cwd", default=None)
    c = sub.add_parser("check", help="CD001 stale, CD002 empty probe, CD004 dead source")
    c.add_argument("--max-age", default=f"{int(check_mod.DEFAULT_MAX_AGE_S)}s")
    c.add_argument("--probe", action="append", default=None)
    c.add_argument("--expect-domains", default="tools,skills")
    c.add_argument("--cwd", default=None)
    return ap


def _print_cards(res: dict) -> None:
    live = res.get("live") or {}
    note = "" if not live.get("error") else f"  (live search: {live['error'][:80]})"
    print(f"{len(res['cards'])} cards for {res['task']!r}{note}")
    if (res.get("caller") or {}).get("note"):
        print(f"  ({res['caller']['note']})")
    for c in res["cards"]:
        flag = f" [{c['license_flag']}]" if c.get("license_flag") else ""
        print(f"  {c['score']:.2f} {c['domain']:<6} {c['name']}  ({c['hosting']}){flag}")
        print(f"       {c['one_line'][:110]}")
        print(f"       -> {c['how_to_invoke']}")


def build_all(cwd: Optional[str] = None, force: bool = False,
              all_cwds: bool = False) -> "tuple[int, list]":
    """Full build (writes the refresh receipt) plus, with ``all_cwds``, every other
    directory's cwd part. (rc, lines)."""
    docs = index.build(cwd, force=force)
    lines = []
    for part, doc in docs.items():
        srcs = doc.get("sources") or {}
        dead = [s for s, r in srcs.items() if not r.get("ok")]
        lines.append(f"{part}: {len(doc.get('cards') or [])} cards, {len(srcs)} sources"
                     + (f", dead: {', '.join(dead)}" if dead else "")
                     + f" -> {index.part_path(part, cwd)}")
    if all_cwds:
        here = str(Path(cwd or index.default_cwd()).resolve())
        caller = scope.caller_from_me((docs.get("user") or {}).get("caller") or {})
        for other in index.known_cwds():
            if str(Path(other).resolve()) == here:
                continue
            doc = index.build_part("cwd", cwd=other, force=force, caller=caller)
            lines.append(f"cwd {other}: {len(doc.get('cards') or [])} cards")
    rec = index.read_receipt() or {}
    return int(rec.get("exit_code", 1) or 0), lines


def main(argv: Optional[list] = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if exc.code in (0, None) else 2
    if args.self_test:
        return self_test()
    # A cp1252 console must not turn a card's arrow into a traceback; a stream that is
    # not a TextIOWrapper (a test's capture) keeps its own handling.
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    if args.verb == "build":
        if args.detach:
            pid = index.detach_build(args.cwd, force=args.force)
            print(f"discovery build started (pid {pid})")
            return 0
        rc, lines = build_all(args.cwd, force=args.force, all_cwds=args.all_cwds)
        for ln in lines:
            print(ln)
        return rc
    if args.verb == "query":
        res = rank.query(args.task, k=args.k, cwd=args.cwd, live=not args.no_live)
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=True))
        else:
            _print_cards(res)
        return 0 if res["cards"] else 1
    if args.verb == "check":
        try:
            max_age = check_mod.parse_age(args.max_age)
        except ValueError:
            print(f"cannot parse --max-age {args.max_age!r}")
            return 2
        probes = None
        if args.probe:
            want = tuple(d for d in args.expect_domains.split(",") if d)
            probes = tuple((p, want, ()) for p in args.probe)
        try:
            rc, lines = check_mod.check_index(args.cwd, max_age_s=max_age, probes=probes)
        except OSError as exc:
            print(f"could not judge: {exc}")
            return 2
        for ln in lines:
            print(ln)
        print({0: "CLEAN", 1: "VIOLATION"}[rc])
        return rc
    _parser().print_help()
    return 2


# ── self-test ───────────────────────────────────────────────────────────────

FP_FREE = "f1f1f1f1f1f1"
FP_PLATFORM = "a0a0a0a0a0a0"


def _fixture_cards(fetched_for: str = FP_FREE) -> list:
    def card(domain: str, name: str, text: str, **kw: object) -> dict:
        c = {"domain": domain, "name": name, "one_line": text, "source": "fixture",
             "hosting": "local", "how_to_invoke": name, "tags": [], "license": "",
             "license_flag": ""}
        c.update(kw)
        return c

    def gw(name: str, text: str) -> dict:
        return card("tools", name, text, source="gateway", hosting="fleet",
                    fetched_for=fetched_for)
    return [
        gw("sana_video", "segment and edit objects in video with masks"),
        gw("fleet_control", "restart fleet containers"),
        gw("federation_sync_agents", "sync agents"),
        gw("get_secret", "read a secret from the vault"),
        card("tools", "awgit", "commit safely in a shared worktree", source="cli"),
        card("skills", "awvision", "ask a vision model about video frames and masks"),
        card("skills", "reverse-engineer-anything", "reverse engineer a desktop app"),
        card("skills", "concurrent-safe-git", "commit safely in a shared worktree"),
        card("packs", "pro-pack", "segment objects", min_tier="enterprise", hosting="hosted"),
    ]


def self_test() -> int:
    failures: list = []
    saved = os.environ.get("AITHER_DISCOVERY_DIR")
    try:
        _self_test_body(failures)
    finally:
        if saved is None:
            os.environ.pop("AITHER_DISCOVERY_DIR", None)
        else:
            os.environ["AITHER_DISCOVERY_DIR"] = saved
    if failures:
        print("SELF-TEST FAIL")
        for f in failures:
            print("  " + f)
        return 1
    print("SELF-TEST PASS: scope (fetched_for allowlist, admin second pass, "
          "not-implemented, card tier, unknown caller tier, unknown caller), rank "
          "(querying caller, platform index read by a tenant, live re-filtered), "
          "TTL reuse keyed by caller, CD001 stale+missing+no receipt+failed build, "
          "CD002 empty+nonsense+pinned, CD004 dead gateway, dead-pid lock, per-cwd and "
          "per-caller keying")
    return 0


def _table(cards: list) -> list:
    return [("fixture", lambda _cwd: ([dict(c) for c in cards],
                                      {"ok": True, "count": len(cards), "error": ""}))]


def _self_test_body(failures: list) -> None:
    with tempfile.TemporaryDirectory() as td:
        os.environ["AITHER_DISCOVERY_DIR"] = td
        cwd = Path(td) / "proj"
        cwd.mkdir()
        free = scope.Caller(tenant_id="tnt_x", tier="free", known=True, fingerprint=FP_FREE)
        plat = scope.Caller(tenant_id="platform", tier="platform", known=True,
                            fingerprint=FP_PLATFORM)

        for part in ("user", "cwd"):
            index.build_part(part, cwd=cwd, caller=free, table=_table(_fixture_cards()))
        merged = index.load_merged(cwd, fp=FP_FREE)
        names = {c["name"] for c in merged["cards"]}
        for bad, why in (("fleet_control", "platform-only tool shown to a free caller"),
                         ("get_secret", "a secret tool shown to a free caller"),
                         ("federation_sync_agents", "not-implemented tool shown"),
                         ("pro-pack", "enterprise pack shown to a free caller")):
            if bad in names:
                failures.append(f"scope: {why}")
        if "sana_video" not in names:
            failures.append("scope: the caller's own gateway card was dropped")
        if any(c["name"] == "sana_video" for c in
               scope.filter_cards(_fixture_cards(), scope.Caller())[0]):
            failures.append("scope: unknown caller did not fail closed")
        other = scope.Caller(tenant_id="tnt_y", tier="free", known=True, fingerprint="b" * 12)
        if any(c["name"] == "sana_video" for c in
               scope.filter_cards(_fixture_cards(), other)[0]):
            failures.append("scope: a card fetched for one caller was shown to another")
        pro = scope.caller_from_me({"tenant_id": "tnt_x", "tier": "Pro "}, fingerprint=FP_FREE)
        if scope.admit({"domain": "packs", "name": "x", "hosting": "hosted",
                        "min_tier": "enterprise"}, pro)[0] or not pro.tier_error:
            failures.append("scope: an unknown caller tier outranked an enterprise pack")

        # A platform-built index read by a free tenant (same files, other bearer).
        pdir = Path(td) / "plat"
        os.environ["AITHER_DISCOVERY_DIR"] = str(pdir)
        index.build_part("user", cwd=cwd, caller=plat,
                         table=_table(_fixture_cards(FP_PLATFORM)))
        pm = index.load_merged(cwd, fp=FP_PLATFORM)
        if "get_secret" not in {c["name"] for c in pm["cards"]}:
            failures.append("fixture: the platform index lacks its own admin card")
        res = rank.query("read a secret from the vault", live=False, merged=pm,
                         current_fp=FP_FREE)
        if any(c["name"] in ("get_secret", "fleet_control") for c in res["cards"]):
            failures.append("rank: a platform-built index answered a tenant with admin cards")
        if index.load_merged(cwd, fp=FP_FREE)["parts"]["user"] is not None:
            failures.append("index: a tenant read the platform caller's user part")

        # TTL reuse must not cross callers.
        calls: list = []

        def gw_table(names: list) -> list:
            def fn(_cwd: Path) -> "tuple[list, dict]":
                calls.append(list(names))
                return ([{"domain": "tools", "name": n, "one_line": n, "source": "gateway",
                          "hosting": "fleet", "how_to_invoke": n} for n in names],
                        {"ok": True, "count": len(names), "error": ""})
            return [("gateway_tools", fn)]
        shared = Path(td) / "shared"
        os.environ["AITHER_DISCOVERY_DIR"] = str(shared)
        index.build_part("user", cwd=cwd, caller=plat, table=gw_table(["get_secret", "rag_query"]))
        prev_path = index.part_path("user", cwd, fp=FP_PLATFORM)
        tenant_path = index.part_path("user", cwd, fp=FP_FREE)
        if prev_path == tenant_path:
            failures.append("index: two callers share one user part")
        tenant_path.write_bytes(prev_path.read_bytes())  # worst case: a copied file
        doc = index.build_part("user", cwd=cwd, caller=free, table=gw_table(["rag_query"]))
        if len(calls) != 2 or any(c["name"] == "get_secret" for c in doc["cards"]):
            failures.append(f"index: a cached gateway list was reused for another caller "
                            f"({calls}, {[c['name'] for c in doc['cards']]})")

        os.environ["AITHER_DISCOVERY_DIR"] = td
        res = rank.query("segment objects in video masks", live=False, merged=merged,
                         current_fp=FP_FREE)
        doms = {c["domain"] for c in res["cards"]}
        if not {"tools", "skills"} <= doms:
            failures.append(f"rank: probe reached {sorted(doms)}, want tools+skills")
        live = rank.query("segment objects in video", live=True, merged=merged,
                          current_fp=FP_FREE,
                          search=lambda task, k, timeout: (["fleet_control", "sana_video"], ""))
        if any(c["name"] == "fleet_control" for c in live["cards"]):
            failures.append("rank: a live hit bypassed the scope filter")

        ok_rec = {"exit_code": 0, "ts": time.time()}
        probes = (("commit safely in a shared worktree", ("tools", "skills"),
                   ("awgit", "concurrent-safe-git")),)
        rc, out = check_mod.check_index(str(cwd), merged=merged, refresh=ok_rec, probes=probes)
        if rc != 0:
            failures.append(f"check: fresh fixture index did not pass: {out}")
        cases = [
            ("CD001: a 7-day-old index passed", _aged(merged), ok_rec, probes),
            ("CD002: an empty index passed", {"cards": [], "parts": merged["parts"]},
             ok_rec, probes),
            ("CD001: a missing cwd part passed",
             {"cards": merged["cards"], "parts": {"user": merged["parts"]["user"],
                                                  "cwd": None}}, ok_rec, probes),
            ("CD004: a dead gateway source passed", _dead(merged), ok_rec, probes),
            ("CD001: no refresh receipt passed", merged, None, probes),
            ("CD001: a 7-day-old receipt passed", merged,
             {"exit_code": 0, "ts": time.time() - 7 * 86400}, probes),
            ("CD001: a failed build receipt passed", merged,
             {"exit_code": 1, "ts": time.time(), "error": "x"}, probes),
            ("CD002: a nonsense probe passed", merged, ok_rec,
             (("xyzzy plugh frobnicate video", ("tools", "skills"), ()),)),
            ("CD002: a pinned card missing from the top passed", merged, ok_rec,
             (("commit safely in a shared worktree", ("tools",), ("no-such-card",)),)),
        ]
        for why, fixture, rec, prb in cases:
            if check_mod.check_index(str(cwd), merged=fixture, refresh=rec, probes=prb)[0] != 1:
                failures.append(why)
        a = index.part_path("cwd", cwd)
        other_dir = Path(td) / "other"
        other_dir.mkdir()
        if a == index.part_path("cwd", other_dir):
            failures.append("index: two directories share one cwd part")

        lock_path = Path(td) / "x.lock"
        lock_path.write_text("999999999", encoding="utf-8")  # no such pid
        with index._Lock(lock_path) as lock:
            if not lock.held:
                failures.append("index: a lock held by a dead pid was not taken over")


def _aged(merged: dict) -> dict:
    out = json.loads(json.dumps(merged))
    out["parts"]["user"]["built_at"] = time.time() - 7 * 86400
    return out


def _dead(merged: dict) -> dict:
    out = json.loads(json.dumps(merged))
    out["parts"]["user"]["sources"]["gateway_tools"] = {"ok": False, "error": "down"}
    return out


if __name__ == "__main__":
    sys.exit(main())
