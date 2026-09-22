"""adk bricks -- see, upgrade, test and roll back the Aither World bricks on this machine.

The aw* bricks ship as separate packages, so "what do I have, what is newer,
did the upgrade break anything, put it back" had no answer short of reading
pip output by hand. This module is that answer, stdlib only so it still runs
when the thing you are diagnosing is the broken dependency.

Truth rules, each one a way this could lie:

* An EDITABLE install is a source tree. Upgrading it from PyPI would replace
  the developer's checkout with a wheel, so it is REFUSED and named as such.
* "latest" comes from the package index or is reported unknown. A network
  failure is never shown as "up to date".
* A brick installed from git has no index version to compare. It says so.
* A test proves only what it ran: a fresh-process import, then the brick's
  ``_doctor --self-test`` when one exists. "no self-test" is not a pass.
* Every upgrade and rollback appends to a JSONL history, which is what
  ``rollback`` reads. No history, no rollback -- it never guesses a version.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from importlib import metadata as md
from pathlib import Path

#: Frozen from the Aither World registry, ecosystem.yaml (every entry whose install line
#: is ``pip install <dist>``). An installed package cannot read the registry,
#: so the list travels with the code; tests/test_bricks.py fails when it drifts
#: from the registry. (registry id, distribution name, source)
FAMILY: tuple[tuple[str, str, str], ...] = (
    ("aitherkvcache", "aither-kvcache", "pypi"),
    ("awask", "awask", "pypi"),
    ("awavatar", "awavatar", "git"),
    ("awbac", "awbac", "pypi"),
    ("awbrain", "awbrain", "pypi"),
    ("awbrowse", "awbrowse", "pypi"),
    ("awclassify", "awclassify", "git"),
    ("awdecide", "awdecide", "pypi"),
    ("awdelphi", "awdelphi", "pypi"),
    ("awdit", "awdit", "pypi"),
    ("awdk", "awdk", "pypi"),
    ("awembed", "awembed", "pypi"),
    ("awevolve", "awevolve", "pypi"),
    ("awfind", "awfind", "pypi"),
    ("awflow", "aitherium-awflow", "pypi"),
    ("awfocus", "awfocus", "pypi"),
    ("awgit", "awgit", "pypi"),
    ("awgraph", "awgraph", "pypi"),
    ("awgym", "awgym", "pypi"),
    ("awiam", "awiam", "pypi"),
    ("awkno", "awkno", "pypi"),
    ("awm", "awm", "pypi"),
    ("awmail", "awmail", "pypi"),
    ("awmine", "awmine", "git"),
    ("awnboard", "awnboard", "pypi"),
    ("awnest", "awnest", "pypi"),
    ("awnet", "awnet", "pypi"),
    ("awnode", "awnode", "pypi"),
    ("awpredict", "awpredict", "git"),
    ("awprism", "awprism", "pypi"),
    ("awreason", "awreason", "pypi"),
    ("awrecover", "awrecover", "pypi"),
    ("awrecurse", "awrecurse", "pypi"),
    ("awrelay", "awrelay", "pypi"),
    ("awrena", "awrena", "pypi"),
    ("awrepl", "awrepl", "pypi"),
    ("awreport", "awreport", "pypi"),
    ("awresearch", "awresearch", "pypi"),
    ("awrise", "awrise", "pypi"),
    ("awrouter", "awrouter", "pypi"),
    ("awrtifact", "awrtifact", "pypi"),
    ("awrun", "awrun", "pypi"),
    ("awscreen", "awscreen", "pypi"),
    ("awseal", "awseal", "pypi"),
    ("awsettings", "awsettings", "pypi"),
    ("awshare", "awshare", "pypi"),
    ("awstorage", "awstorage", "pypi"),
    ("awswarm", "awswarm", "pypi"),
    ("awtax", "awtax", "pypi"),
    ("awtoll", "awtoll", "pypi"),
    ("awtunnel", "awtunnel", "pypi"),
    ("awvision", "awvision", "pypi"),
    ("awvoice", "awvoice", "pypi"),
    ("awwall", "awwall", "pypi"),
    ("gawbbonet", "gawbbonet", "pypi"),
)

INDEX_URL = os.environ.get("ADK_BRICKS_INDEX", "https://pypi.org/pypi")


def home() -> Path:
    return Path(os.environ.get("ADK_BRICKS_HOME") or Path.home() / ".aither" / "bricks")


def history_path() -> Path:
    return home() / "history.jsonl"


# ── version helpers ──────────────────────────────────────────────────────────

def _vkey(v: str):
    """Sort key for a version. packaging when present, else a numeric tuple."""
    try:
        from packaging.version import Version

        return (0, Version(v))
    except Exception:
        parts = []
        for chunk in str(v).replace("-", ".").split("."):
            parts.append((0, int(chunk)) if chunk.isdigit() else (1, chunk))
        return (1, tuple(parts))


def is_newer(candidate: str | None, current: str | None) -> bool:
    if not candidate or not current:
        return False
    a, b = _vkey(candidate), _vkey(current)
    if a[0] != b[0]:  # one side parsed with packaging and one did not
        return str(candidate) != str(current)
    return a[1] > b[1]


# ── discovery ────────────────────────────────────────────────────────────────

def _family_dists() -> dict[str, tuple[str, str]]:
    return {dist.lower(): (bid, src) for bid, dist, src in FAMILY}


def _marked_aither(dist: md.Distribution) -> bool:
    m = dist.metadata
    blob = " ".join(
        filter(None, [m.get("Author") or "", m.get("Author-email") or "", m.get("Home-page") or ""]
               + (m.get_all("Project-URL") or []))
    ).lower()
    return "aitherium" in blob


def installed(dist_name: str) -> dict | None:
    """{version, editable, location, import_name} for an installed dist, else None."""
    try:
        dist = md.distribution(dist_name)
    except md.PackageNotFoundError:
        return None
    editable, location = False, None
    try:
        raw = dist.read_text("direct_url.json")
        if raw:
            direct = json.loads(raw)
            editable = bool(direct.get("dir_info", {}).get("editable"))
            location = direct.get("url")
    except (OSError, ValueError):
        # No readable direct_url.json: pip wrote none, so this is a plain
        # index/wheel install, which is exactly what the defaults say.
        editable, location = False, None
    import_name = None
    try:
        top = dist.read_text("top_level.txt")
        if top:
            import_name = top.split()[0].strip()
    except OSError:
        import_name = None  # falls back to the distribution name below
    return {
        "version": dist.version,
        "editable": editable,
        "location": location,
        "import_name": import_name or dist_name.replace("-", "_"),
    }


def latest(dist_name: str, timeout: float = 8.0) -> tuple[str | None, str | None]:
    """(latest version, error). Never raises; an unreachable index is an error, not a version."""
    url = f"{INDEX_URL.rstrip('/')}/{dist_name}/json"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.load(resp)
        return str(data["info"]["version"]), None
    except urllib.error.HTTPError as e:
        return None, "not on the index" if e.code == 404 else f"index HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as e:
        return None, f"index unreachable: {type(e).__name__}"


def status(check_latest: bool = True) -> list[dict]:
    """One row per family brick that is installed, plus any Aitherium-marked
    aw* dist the frozen list does not know yet (a brick newer than this awdk)."""
    fam = _family_dists()
    todo: list[tuple[str, str, str, dict]] = []
    seen: set[str] = set()
    for bid, dist, src in FAMILY:
        info = installed(dist)
        if info is None:
            continue
        seen.add(dist.lower())
        todo.append((bid, dist, src, info))
    for d in md.distributions():
        name = (d.metadata.get("Name") or "").lower()
        if not name.startswith("aw") or name in seen or name in fam:
            continue
        if _marked_aither(d):
            info = installed(name)
            if info:
                todo.append((name, name, "unlisted", info))
    # One index lookup per brick is network-bound: a small pool turns ~50
    # sequential round trips (4.9 s measured) into a few, so a settings pane
    # can call this without hanging. Order is preserved.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(lambda t: _row(t[0], t[1], t[2], t[3], check_latest), todo))


def _row(bid: str, dist: str, src: str, info: dict, check_latest: bool) -> dict:
    row = {
        "id": bid,
        "dist": dist,
        "source": src,
        "installed": info["version"],
        "editable": info["editable"],
        "latest": None,
        "error": None,
        "outdated": False,
        "action": "",
    }
    if src == "git":
        row["error"] = "installed from git: no index version to compare"
    elif check_latest:
        row["latest"], row["error"] = latest(dist)
        row["outdated"] = is_newer(row["latest"], info["version"])
    if info["editable"]:
        row["action"] = "editable source tree: update with git, not pip"
    elif row["outdated"]:
        row["action"] = f"adk bricks upgrade {bid}"
    return row


def _resolve(name: str) -> tuple[str, str, str]:
    """Registry id or distribution name -> (id, dist, source)."""
    low = name.lower()
    for bid, dist, src in FAMILY:
        if low in (bid.lower(), dist.lower()):
            return bid, dist, src
    return name, name, "unlisted"


# ── history ──────────────────────────────────────────────────────────────────

def record(entry: dict) -> None:
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **entry}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def history(name: str | None = None) -> list[dict]:
    path = history_path()
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if name is None or row.get("dist", "").lower() == _resolve(name)[1].lower():
            out.append(row)
    return out


# ── actions ──────────────────────────────────────────────────────────────────

def _pip(args: list[str], timeout: int = 900) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pip", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    return proc.returncode, (proc.stdout + proc.stderr)[-4000:]


def test(name: str, timeout: int = 180) -> dict:
    """Fresh-process import, then the brick's own self-test if it has one."""
    bid, dist, _src = _resolve(name)
    info = installed(dist)
    if info is None:
        return {"id": bid, "ok": False, "import": "not installed", "selftest": "skipped"}
    mod = info["import_name"]
    proc = subprocess.run(
        [sys.executable, "-c", f"import {mod}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    if proc.returncode != 0:
        return {"id": bid, "ok": False, "import": "FAILED", "selftest": "skipped",
                "detail": (proc.stderr or proc.stdout)[-1500:]}
    def has(sub: str) -> bool:
        probe = subprocess.run(
            [sys.executable, "-c",
             f"import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('{mod}.{sub}') "
             f"else 3)"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        return probe.returncode == 0

    # Label exactly what ran. A doctor with no _selftest answers --self-test
    # with exit 0 and "no machinery to prove" -- that is NOT a pass, and an
    # older doctor ignores the flag and prints its report instead.
    if has("_selftest"):
        st = subprocess.run(
            [sys.executable, "-m", f"{mod}._doctor", "--self-test"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        ok = st.returncode == 0
        return {"id": bid, "ok": ok, "import": "ok", "selftest": "pass" if ok else "FAILED",
                "detail": (st.stdout + st.stderr)[-1500:]}
    if has("_doctor"):
        rep = subprocess.run(
            [sys.executable, "-m", f"{mod}._doctor"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        ok = rep.returncode == 0
        return {"id": bid, "ok": ok, "import": "ok",
                "selftest": "none (doctor report ran)" if ok else "doctor reported a problem",
                "detail": (rep.stdout + rep.stderr)[-1500:]}
    return {"id": bid, "ok": True, "import": "ok", "selftest": "none (brick has no doctor)"}


def upgrade(name: str, to: str | None = None, run_test: bool = True,
            auto_rollback: bool = True, dry_run: bool = False) -> dict:
    bid, dist, src = _resolve(name)
    info = installed(dist)
    if info is None:
        return {"id": bid, "ok": False, "error": f"{dist} is not installed"}
    if info["editable"]:
        return {"id": bid, "ok": False,
                "error": f"{dist} is an editable source install ({info['location']}); "
                         "update it with git -- a pip upgrade would replace the checkout"}
    if src == "git":
        return {"id": bid, "ok": False,
                "error": f"{dist} installs from git; reinstall from its repo"}
    target = to
    if target is None:
        target, err = latest(dist)
        if target is None:
            return {"id": bid, "ok": False, "error": err}
    before = info["version"]
    if target == before:
        return {"id": bid, "ok": True, "from": before, "to": target, "note": "already at target"}
    if dry_run:
        return {"id": bid, "ok": True, "from": before, "to": target, "dry_run": True}
    code, out = _pip(["install", f"{dist}=={target}"])
    if code != 0:
        record({"op": "upgrade", "dist": dist, "from": before, "to": target, "ok": False})
        return {"id": bid, "ok": False, "from": before, "to": target, "error": "pip failed",
                "detail": out[-1500:]}
    result = {"id": bid, "ok": True, "from": before, "to": target}
    if run_test:
        verdict = test(dist)
        result["test"] = verdict
        if not verdict["ok"] and auto_rollback:
            rb_code, rb_out = _pip(["install", f"{dist}=={before}"])
            result["ok"] = False
            result["rolled_back"] = rb_code == 0
            if rb_code != 0:
                result["rollback_detail"] = rb_out[-1500:]
    record({"op": "upgrade", "dist": dist, "from": before, "to": target, "ok": result["ok"],
            "rolled_back": result.get("rolled_back", False)})
    return result


def rollback(name: str, run_test: bool = True) -> dict:
    """Reinstall the version this machine had before its last successful upgrade."""
    bid, dist, _src = _resolve(name)
    info = installed(dist)
    if info and info["editable"]:
        return {"id": bid, "ok": False, "error": f"{dist} is an editable source install; use git"}
    past = [h for h in history(dist) if h.get("op") == "upgrade" and h.get("ok")]
    if not past:
        return {"id": bid, "ok": False,
                "error": "no recorded upgrade to roll back (adk bricks only rolls back its own)"}
    prev = past[-1]["from"]
    current = info["version"] if info else None
    code, out = _pip(["install", f"{dist}=={prev}"])
    result = {"id": bid, "ok": code == 0, "from": current, "to": prev}
    if code != 0:
        result["detail"] = out[-1500:]
    elif run_test:
        result["test"] = test(dist)
        result["ok"] = result["test"]["ok"]
    record({"op": "rollback", "dist": dist, "from": current, "to": prev, "ok": result["ok"]})
    return result


# ── CLI ──────────────────────────────────────────────────────────────────────

def _print_rows(rows: list[dict]) -> None:
    if not rows:
        print("no Aither World bricks installed")
        return
    for r in rows:
        mark = "EDITABLE" if r["editable"] else ("OUTDATED" if r["outdated"] else "")
        latest_s = r["latest"] or ("?" if r["error"] else "-")
        print(f"{r['id']:14s} {r['installed']:>10s} -> {latest_s:10s} {mark:8s} "
              f"{r['error'] or r['action']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="adk bricks", description="See, upgrade, test and roll back Aither World bricks.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="verb")
    sub.add_parser("list", help="every installed brick, with the index's latest version")
    sub.add_parser("outdated", help="only bricks with a newer version on the index")
    up = sub.add_parser("upgrade", help="upgrade one brick, test it, roll back on failure")
    up.add_argument("name")
    up.add_argument("--to", help="exact version (default: latest on the index)")
    up.add_argument("--no-test", action="store_true")
    up.add_argument("--no-rollback", action="store_true", help="keep a failed upgrade in place")
    up.add_argument("--dry-run", action="store_true")
    t = sub.add_parser("test", help="fresh import + the brick's own self-test")
    t.add_argument("name")
    rb = sub.add_parser("rollback", help="reinstall the version before the last upgrade")
    rb.add_argument("name")
    h = sub.add_parser("history", help="upgrades and rollbacks recorded on this machine")
    h.add_argument("name", nargs="?")
    # --json is accepted ANYWHERE: `adk bricks --json list` loses a leading flag
    # to adk's REMAINDER pass-through, and a subparser does not know the flag.
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    args = ap.parse_args([a for a in argv if a != "--json"])
    args.json = as_json

    verb = args.verb or "list"
    if verb in ("list", "outdated"):
        rows = status()
        if verb == "outdated":
            rows = [r for r in rows if r["outdated"]]
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            _print_rows(rows)
        return 0
    if verb == "history":
        rows = history(args.name)
        print(json.dumps(rows, indent=2) if args.json else
              "\n".join(f"{r.get('at')} {r.get('op'):8s} {r.get('dist')} {r.get('from')} -> "
                        f"{r.get('to')} ok={r.get('ok')}" for r in rows) or "no history")
        return 0
    if verb == "upgrade":
        res = upgrade(args.name, to=args.to, run_test=not args.no_test,
                      auto_rollback=not args.no_rollback, dry_run=args.dry_run)
    elif verb == "test":
        res = test(args.name)
    else:
        res = rollback(args.name)
    print(json.dumps(res, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
