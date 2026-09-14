"""``adk setup-all`` — one command to install / set up all AitherOS client products.

Installs, in dependency order, whichever of the five client products are requested:

  1. awdk[shell,platform,node]  — this SDK + extras (pip; also brings awnode)
  2. AitherShell CLI                  — npm ``@aitherium/shell-cli`` (if Node present)
  3. awnode                       — MCP gateway (verified; ships via the [node] extra)
  4. AitherConnect                    — federation bundle (setup hint / wizard)
  3. AitherNode                       — MCP gateway (verified; ships via the [node] extra)
  4. Awconnect                    — federation bundle (setup hint / wizard)
  5. AitherZero public stack          — heavy; opt-in via ``--with-stack`` (delegates to
                                        ``adk setup --stack``)

Design: this orchestrator calls each product's REAL install entry point over subprocess
(pip / npm / pwsh / adk) rather than reimplementing any of them — so it never drifts from
the canonical installers. Every step is BEST-EFFORT: a failure is recorded and reported in
the final summary, and does not abort the remaining steps (unless ``--strict``). ``--dry-run``
prints the plan without doing anything; ``--only`` / ``--skip`` select products.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field

# Canonical product ids (order = dependency/install order).
PRODUCTS = ["adk", "shell", "node", "connect", "aitherzero"]

# pip extras installed with the SDK (shell CLI + platform helpers + node/MCP gateway).
_ADK_EXTRAS = "shell,platform,node"
_SHELL_NPM = "@aitherium/shell-cli"


@dataclass
class StepResult:
    product: str
    status: str  # "ok" | "skipped" | "failed" | "planned"
    detail: str = ""


@dataclass
class _Ctx:
    dry_run: bool = False
    strict: bool = False
    with_stack: str = ""          # "" = skip aitherzero; else a stack profile (core/full/…)
    dev: bool = False             # pip install -e the local checkout instead of from PyPI
    yes: bool = False             # non-interactive
    results: list = field(default_factory=list)


def _have(exe: str) -> bool:
    return shutil.which(exe) is not None


def _run(ctx: _Ctx, cmd: list, product: str, what: str) -> StepResult:
    """Run a subprocess step (or print it under --dry-run). Never raises."""
    printable = " ".join(cmd)
    if ctx.dry_run:
        print(f"  [dry-run] {product}: would run  {printable}")
        return StepResult(product, "planned", printable)
    print(f"  → {product}: {what}\n     $ {printable}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except FileNotFoundError:
        return StepResult(product, "failed", f"command not found: {cmd[0]}")
    except subprocess.TimeoutExpired:
        return StepResult(product, "failed", f"timed out: {printable}")
    except Exception as exc:  # noqa: BLE001 — best-effort installer
        return StepResult(product, "failed", f"{type(exc).__name__}: {exc}")
    if proc.returncode == 0:
        return StepResult(product, "ok", what)
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return StepResult(product, "failed", (tail[-1] if tail else f"exit {proc.returncode}"))


def _repo_root():
    """The awdk source root if we're running from a checkout, else None."""
    from pathlib import Path
    here = Path(__file__).resolve().parent.parent  # adk/ -> awdk/
    return here if (here / "pyproject.toml").exists() else None


# ── per-product steps ───────────────────────────────────────────────────────

def _step_adk(ctx: _Ctx) -> StepResult:
    root = _repo_root()
    if ctx.dev and root:
        cmd = [sys.executable, "-m", "pip", "install", "-e", f".[{_ADK_EXTRAS}]"]
        # run in the repo root
        if ctx.dry_run:
            print(f"  [dry-run] adk: would run (cwd={root})  {' '.join(cmd)}")
            return StepResult("adk", "planned", "editable install")
        print(f"  → adk: editable install from {root}")
        try:
            proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=1800)
            if proc.returncode == 0:
                return StepResult("adk", "ok", "editable install")
            tail = (proc.stderr or "").strip().splitlines()
            return StepResult("adk", "failed", tail[-1] if tail else f"exit {proc.returncode}")
        except Exception as exc:  # noqa: BLE001
            return StepResult("adk", "failed", str(exc))
    cmd = [sys.executable, "-m", "pip", "install", "-U", f"awdk[{_ADK_EXTRAS}]"]
    return _run(ctx, cmd, "adk", f"pip install awdk[{_ADK_EXTRAS}]")


def _step_shell(ctx: _Ctx) -> StepResult:
    if not _have("npm"):
        return StepResult(
            "shell", "skipped",
            "npm not found — AitherShell also ships via the pip [shell] extra "
            "(run `adk-shell`); install Node.js for the standalone `aither` binary.",
        )
    return _run(ctx, ["npm", "install", "-g", _SHELL_NPM], "shell",
                f"npm install -g {_SHELL_NPM}")


def _step_node(ctx: _Ctx) -> StepResult:
    # awnode ships with the [node] extra (installed in _step_adk); verify it imports.
    if ctx.dry_run:
        print("  [dry-run] node: would verify `import mcp` (installed via [node] extra)")
        return StepResult("node", "planned", "verify mcp import")
    code = "import mcp, starlette; print(getattr(mcp,'__version__','?'))"
    res = _run(ctx, [sys.executable, "-c", code], "node", "verify awnode (mcp/starlette)")
    if res.status == "failed":
        # try to repair by installing the extra explicitly
        return _run(ctx, [sys.executable, "-m", "pip", "install", "awdk[node]"],
                    "node", "install awdk[node]")
    return res


def _step_connect(ctx: _Ctx) -> StepResult:
    # Awconnect is a federation bundle set up via the shell install wizard (device-flow
    # auth + endpoint register). That flow is interactive, so we do NOT run it unattended —
    # verify the entry point exists and hand the user the exact command.
    if ctx.dry_run:
        print("  [dry-run] connect: would verify the connect setup entry point")
        return StepResult("connect", "planned", "verify connect bundle")
    try:
        import importlib
        importlib.import_module("adk.shell.plugins.builtins.setup")
        hint = "run `aither-shell -c install` (device-flow auth + endpoint register)"
        return StepResult("connect", "ok", f"bundle present — {hint}")
    except Exception as exc:  # noqa: BLE001
        return StepResult("connect", "skipped", f"connect setup not importable: {exc}")


def _step_aitherzero(ctx: _Ctx) -> StepResult:
    if not ctx.with_stack:
        return StepResult("aitherzero", "skipped",
                          "not requested — pass --with-stack <profile> (e.g. core) to deploy")
    if not _have("pwsh"):
        return StepResult("aitherzero", "failed",
                          "PowerShell 7 (pwsh) required for the AitherZero stack — install it first")
    # Delegate to the canonical stack installer (adk setup --stack <profile>).
    adk = shutil.which("adk") or sys.executable
    cmd = ([adk, "setup", "--stack", ctx.with_stack] if adk != sys.executable
           else [sys.executable, "-m", "adk.cli", "setup", "--stack", ctx.with_stack])
    return _run(ctx, cmd, "aitherzero", f"adk setup --stack {ctx.with_stack}")


_STEPS = {
    "adk": _step_adk,
    "shell": _step_shell,
    "node": _step_node,
    "connect": _step_connect,
    "aitherzero": _step_aitherzero,
}


def cmd_setup_all(args) -> int:
    """Handler for ``adk setup-all``. Returns a process exit code."""
    only = {p.strip() for p in (getattr(args, "only", "") or "").split(",") if p.strip()}
    skip = {p.strip() for p in (getattr(args, "skip", "") or "").split(",") if p.strip()}
    unknown = (only | skip) - set(PRODUCTS)
    if unknown:
        print(f"Unknown product(s): {', '.join(sorted(unknown))}. "
              f"Valid: {', '.join(PRODUCTS)}")
        return 2

    ctx = _Ctx(
        dry_run=bool(getattr(args, "dry_run", False)),
        strict=bool(getattr(args, "strict", False)),
        with_stack=(getattr(args, "with_stack", "") or ""),
        dev=bool(getattr(args, "dev", False)),
        yes=bool(getattr(args, "yes", False)),
    )

    selected = [p for p in PRODUCTS if (not only or p in only) and p not in skip]
    print("AitherOS unified setup — installing: " + ", ".join(selected)
          + (" (dry-run)" if ctx.dry_run else ""))
    print()

    for product in selected:
        res = _STEPS[product](ctx)
        ctx.results.append(res)
        if res.status == "failed" and ctx.strict and not ctx.dry_run:
            print(f"\n✗ {product} failed (--strict): {res.detail}")
            _summary(ctx.results)
            return 1

    _summary(ctx.results)
    failed = [r for r in ctx.results if r.status == "failed"]
    return 1 if (failed and not ctx.dry_run) else 0


def _summary(results: list) -> None:
    glyph = {"ok": "✓", "skipped": "•", "failed": "✗", "planned": "·"}
    print("\n── setup summary ─────────────────────────────")
    for r in results:
        print(f"  {glyph.get(r.status, '?')} {r.product:<11} {r.status:<8} {r.detail}")
    ok = sum(1 for r in results if r.status == "ok")
    print(f"  {ok}/{len(results)} installed"
          + (f"  ·  {sum(1 for r in results if r.status == 'failed')} failed"
             if any(r.status == 'failed' for r in results) else ""))
