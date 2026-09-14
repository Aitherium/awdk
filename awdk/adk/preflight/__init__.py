"""Aither self-bootstrap PREFLIGHT layer (Phase 0).

Public surface:
    run_preflight(agent, ctx=None, spec=None, headless=None, task=None) -> CapabilityReport
    CapabilityReport, SlotHealth

Phase 0 scope ONLY: liveness probe + honest capability report + a single hard
binder (allowed_roots). No roles vocabulary, no interview, no
capability_domains.yaml, no domain/safety binders beyond allowed_roots.

The orchestrator NEVER calls ``sys.exit`` and NEVER mutates ``ctx.roles`` — it
marks the report with an ABORT decision (rendered in the table) and RETURNS the
report, letting the harness decide whether to stop.
"""

from __future__ import annotations

import asyncio
import os

from .report import (
    CapabilityReport,
    SlotHealth,
    STATUS_OK,
    STATUS_UNSUPPORTED,
    compute_abort,
    render,
)
from .probe import LivenessProbe
from .policy import apply_policy

__all__ = ["run_preflight", "CapabilityReport", "SlotHealth", "render"]

# Role name -> capability slot. A spec's llm.roles use domain names (default,
# perceive, plan, ...); the probe reports capability slots. This maps one to the
# other so a spec's REQUIRED roles select which slots must be OK.
_ROLE_TO_SLOT = {
    "default": "primary",
    "primary": "primary",
    "plan": "primary",
    "distill": "primary",
    "reasoning": "reasoning",
    "reason": "reasoning",
    "perceive": "reasoning",
    "vision": "vision",
    "embed": "embeddings",
    "embeddings": "embeddings",
}

# Optional slots: an honest MISSING/UNSUPPORTED here never triggers ABORT.
_OPTIONAL_SLOTS = {"ml_teach", "voice", "vision"}


def _spec_get(spec, *path, default=None):
    cur = spec or {}
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _resolve_task(task, spec) -> str:
    """Precedence: task-arg > env > spec default_task / onboarding."""
    if task:
        return str(task)
    env_task = os.getenv("AITHER_TASK", "").strip()
    if env_task:
        return env_task
    st = _spec_get(spec, "default_task")
    if st:
        return str(st)
    ot = _spec_get(spec, "onboarding", "default_task")
    if ot:
        return str(ot)
    return ""


def _resolve_allowed_roots(spec) -> list:
    """Precedence: env > spec onboarding.allowed_roots. Defaults to []."""
    env_roots = os.getenv("AITHER_ALLOWED_ROOTS", "").strip()
    if env_roots:
        return [r for r in env_roots.split(";") if r]
    roots = _spec_get(spec, "onboarding", "allowed_roots")
    if isinstance(roots, list):
        return [str(r) for r in roots if r]
    return []


def _resolve_rules(spec) -> list:
    """Precedence: env (AITHER_RULES, newline/';'-separated) > spec onboarding.rules."""
    env_rules = os.getenv("AITHER_RULES", "").strip()
    if env_rules:
        parts = [p.strip() for p in env_rules.replace("\n", ";").split(";")]
        return [p for p in parts if p]
    rules = _spec_get(spec, "onboarding", "rules")
    if isinstance(rules, list):
        return [str(r) for r in rules if r]
    if isinstance(rules, str) and rules.strip():
        return [rules.strip()]
    return []


def _required_slots(spec) -> list:
    """Derive required capability slots from spec llm.roles.

    A role is required unless it declares ``required: false`` / ``optional: true``.
    With no spec (or no roles), the primary inference slot is required by default
    (an agent with no brain cannot proceed)."""
    roles = _spec_get(spec, "llm", "roles")
    required: set = set()
    if isinstance(roles, dict) and roles:
        for role_name, cfg in roles.items():
            optional = False
            if isinstance(cfg, dict):
                if cfg.get("required") is False or cfg.get("optional") is True:
                    optional = True
            if optional:
                continue
            slot = _ROLE_TO_SLOT.get(str(role_name).lower(), "primary")
            required.add(slot)
    if not required:
        required.add("primary")
    return sorted(required)


def _inject_rules(agent, rules: list) -> bool:
    """Append rules to the agent's system prompt WITHOUT clobbering it. Returns
    True on success, False if it could not safely append (caller notes it)."""
    if not rules:
        return True
    try:
        block = "\n\n[SESSION RULES]\n" + "\n".join(f"- {r}" for r in rules)
        # The public ``system_prompt`` is a read-only property backed by
        # ``_system_prompt``; read the effective prompt, then set the backing
        # field so the rules are appended, not replaced.
        base = ""
        try:
            base = (agent.system_prompt or "").rstrip()
        except Exception:
            base = (getattr(agent, "_system_prompt", "") or "").rstrip()
        if not hasattr(agent, "_system_prompt"):
            return False
        agent._system_prompt = (base + block) if base else block.lstrip()
        return True
    except Exception:
        return False


def run_preflight(agent, ctx=None, spec=None, headless=None, task=None) -> CapabilityReport:
    """Run the Phase-0 preflight: probe -> policy -> rules -> render.

    Returns the CapabilityReport (marked with an ABORT decision if a required
    slot is unsatisfied). Never exits, never mutates ctx.roles.
    """
    spec = spec or {}

    # 1. Liveness probe on a fresh event loop (bounded, non-hanging).
    probe = LivenessProbe()
    report = asyncio.run(probe.run(agent))

    # 2. HEADLESS-resolve task + rules + allowed_roots by precedence.
    resolved_task = _resolve_task(task, spec)
    report.task = resolved_task
    rules = _resolve_rules(spec)
    allowed_roots = _resolve_allowed_roots(spec)

    # 3. Bind the one hard policy (allowed_roots) in-process + for children.
    if allowed_roots:
        apply_policy({"allowed_roots": allowed_roots})

    # 4. Inject rules into the system prompt (append, do not clobber).
    if rules:
        if not _inject_rules(agent, rules):
            report.abort_reasons.append(
                "rules: could not safely append to system prompt (skipped)"
            )

    # 5. Requiredness -> ABORT decision (mark report; harness decides to stop).
    report.required = _required_slots(spec) if spec else ["primary"]
    abort, reasons = compute_abort(report)
    report.abort = abort
    # Preserve any note (e.g. rules-inject failure) already recorded.
    report.abort_reasons = list(reasons) + [
        r for r in report.abort_reasons if r not in reasons
    ]

    # 6. Print the honest render.
    print(render(report))

    return report
