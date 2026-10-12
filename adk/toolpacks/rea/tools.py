"""rea_* agent tools. Fail soft: every tool returns a dict, never raises."""
from __future__ import annotations

import json
import os
import shutil
import subprocess

REA_VERSION = "6.4.0"  # pinned; upgrade deliberately
_AUTHORITY = ["static", "runtime", "process", "packaged-process"]  # weakest -> strongest

_DEFAULT_CLASSES = ("owned", "licensed", "interop")
_ALL_CLASSES = ("owned", "licensed", "interop", "any")


def _allowed_classes() -> tuple:
    raw = os.environ.get("AITHER_REA_TARGET_CLASSES", "")
    if not raw.strip():
        return _DEFAULT_CLASSES
    picked = tuple(c.strip().lower() for c in raw.split(",") if c.strip())
    return tuple(c for c in picked if c in _ALL_CLASSES) or _DEFAULT_CLASSES


def _authorize(target_class: str) -> tuple:
    cls = (target_class or "").strip().lower()
    if not cls:
        return False, "target_class required: owned|licensed|interop"
    allowed = _allowed_classes()
    if cls == "any" and "any" not in allowed:
        return False, "target_class='any' not permitted; widen AITHER_REA_TARGET_CLASSES"
    if cls not in _ALL_CLASSES:
        return False, "unknown target_class %r; use owned|licensed|interop" % cls
    if cls not in allowed:
        return False, "target_class %r not permitted (allowed: %s)" % (cls, ", ".join(allowed))
    return True, ""



def _cmd() -> list:
    exe = shutil.which("rea")
    if exe:
        return [exe]
    npx = shutil.which("npx")
    return [npx, "-y", f"rea-agents@{REA_VERSION}"] if npx else []


def _run(args: list, timeout: int = 300) -> tuple:
    base = _cmd()
    if not base:
        return 127, "neither rea nor npx on PATH (Node >=22 required)"
    try:
        p = subprocess.run(base + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, check=False)
        if p.returncode == 0:
            return p.returncode, (p.stdout or "")[-3000:]
        return p.returncode, ((p.stdout or "") + (p.stderr or ""))[-3000:]
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as exc:
        return 126, str(exc)


def rea_status() -> dict:
    """Report whether the pinned rea CLI is runnable."""
    rc, out = _run(["--version"], timeout=120)
    return {"ok": rc == 0, "pinned": REA_VERSION, "output": out.strip()[-500:],
            "authorized_target_classes": list(_allowed_classes())}


def rea_analyze_javascript(path: str, target_class: str = "", timeout: int = 300) -> dict:
    """Analyze a JS/Electron app dir or ASAR (returns rea Evidence). target_class declares
    your authorization: owned|licensed|interop; refused otherwise."""
    ok, reason = _authorize(target_class)
    if not ok:
        return {"ok": False, "refused": True, "error": reason}
    if not path or not os.path.exists(path):
        return {"ok": False, "error": f"path not found: {path!r}"}
    rc, out = _run(["analyze-javascript-application", os.path.abspath(path), "--json"], timeout)
    if rc != 0:
        return {"ok": False, "exit": rc, "error": out}
    try:
        return {"ok": True, "evidence": json.loads(out)}
    except ValueError:
        return {"ok": False, "error": "rea output was not JSON", "output": out[:1000]}


def rea_ledger_close(obligations: list) -> dict:
    """Fail-closed closure verdict.

    Each obligation: {id, required(bool, default True), original_authority,
    verifier_authority, verifier_passed(bool)}. An obligation closes only when a
    verifier passed AND its authority is >= the original's. Unknown authority, a
    missing verifier or an empty list never closes.
    """
    if not isinstance(obligations, list) or not obligations:
        return {"status": "unknown", "open": [], "closed": [],
                "reason": "no obligations: absence of evidence is not closure"}
    closed, open_ = [], []
    for o in obligations:
        oid = str((o or {}).get("id", "?"))
        if not (o or {}).get("required", True):
            continue
        _oa = o.get("original_authority")
        orig = _AUTHORITY.index(_oa) if _oa in _AUTHORITY else None
        _va = o.get("verifier_authority")
        ver = _AUTHORITY.index(_va) if _va in _AUTHORITY else None
        ok = bool(o.get("verifier_passed")) and orig is not None and ver is not None and ver >= orig
        (closed if ok else open_).append(oid)
    return {"status": "closed" if not open_ else "open", "open": open_, "closed": closed}
