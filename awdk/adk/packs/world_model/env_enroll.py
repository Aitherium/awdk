"""env_enroll — hand the system an environment, get back a learned verdict.

The north-star piece of the world-model program: an agent (or operator) points
at ANY environment adapter — a dotted path to a class implementing the
EnvironmentAdapter contract (observe/actions/step + a domain name) — and the
system validates the contract, explores it under a hard safety budget with the
learn-safely loop, and records whether the world model genuinely LEARNED the
environment's rules (surprise driven down over episodes).

A successful enrollment writes a durable sandbox proof that
adk.tool_readiness._check_sandbox_proven reads — which is what makes
`require_sandbox_proven=True` a real gate instead of a permanent False: a
capability tied to an environment unlocks only after the model has
demonstrably learned that environment.

Proof criteria (matches tool_readiness' documented spec): at least 20 graded
transitions, and the trailing-10 mean surprise at or under 0.3. A chaotic or
unlearnable environment never proves; an adapter that fails the contract is
reported degraded and writes nothing.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("world_model_pack.enroll")

PROOF_PATH = Path(os.environ.get(
    "ADK_SANDBOX_PROOF_PATH",
    str(Path.home() / ".aither" / "sandbox_proofs.json")))

_REQUIRED = ("observe", "actions", "step")
MIN_TRANSITIONS = 20
TRAILING_MEAN_MAX = 0.3


def _load_adapter(spec: str, kwargs: Optional[Dict[str, Any]] = None) -> Any:
    """Instantiate an adapter from 'package.module:ClassName'."""
    mod_name, _, cls_name = spec.partition(":")
    if not mod_name or not cls_name:
        raise ValueError(
            f"adapter spec must be 'module.path:ClassName', got {spec!r}")
    mod = importlib.import_module(mod_name)
    cls = getattr(mod, cls_name)
    return cls(**(kwargs or {}))


def _validate(adapter: Any) -> list:
    missing = [m for m in _REQUIRED
               if not callable(getattr(adapter, m, None))]
    if not isinstance(getattr(adapter, "domain", None), str):
        missing.append("domain (str attribute)")
    return missing


def _read_proofs() -> Dict[str, Any]:
    try:
        with open(PROOF_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_proof(name: str, record: Dict[str, Any]) -> bool:
    try:
        PROOF_PATH.parent.mkdir(parents=True, exist_ok=True)
        proofs = _read_proofs()
        proofs[name] = record
        tmp = PROOF_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(proofs, f, indent=1)
        os.replace(tmp, PROOF_PATH)
        return True
    except OSError as exc:
        logger.error("env_enroll: could not persist proof for %s: %s", name, exc)
        return False


def env_enroll(
    adapter_spec: str,
    adapter_kwargs: Optional[Dict[str, Any]] = None,
    episodes: int = 10,
    budget: int = 30,
    epsilon: float = 0.3,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """Enroll an environment: validate -> explore safely -> record the verdict.

    Args:
        adapter_spec: 'module.path:ClassName' of an EnvironmentAdapter.
        adapter_kwargs: Constructor kwargs (a FRESH adapter per episode).
        episodes: Exploration episodes (each starts a fresh adapter instance).
        budget: Hard step cap per episode — the safety budget.
        epsilon: Exploration probability for the epsilon-greedy loop.
        name: Proof registry key (default: the adapter's domain).

    Returns a fail-soft dict:
        {ok, name, domain, episodes_run, transitions, trailing_mean_surprise,
         proven, proof_persisted, degraded?, error?}
    """
    from . import tools as wm_tools
    from .safe_explore import explore

    try:
        probe = _load_adapter(adapter_spec, adapter_kwargs)
    except Exception as exc:
        return {"ok": False, "error": f"adapter load failed: {exc}",
                "adapter_spec": adapter_spec}

    missing = _validate(probe)
    if missing:
        return {"ok": False, "degraded": True, "adapter_spec": adapter_spec,
                "error": f"adapter fails EnvironmentAdapter contract; "
                         f"missing: {missing}"}

    domain = probe.domain
    key = name or domain
    total_steps = 0
    total_transitions = 0
    surprise_series: list = []

    for _ep in range(int(episodes)):
        adapter = _load_adapter(adapter_spec, adapter_kwargs)
        result = explore(adapter=adapter, budget=int(budget),
                         epsilon=float(epsilon),
                         wm_observe_fn=wm_tools.wm_observe)
        if result.get("degraded"):
            return {"ok": False, "degraded": True, "adapter_spec": adapter_spec,
                    "error": "explore() degraded mid-enrollment", **result}
        total_steps += result.get("steps", 0)
        total_transitions += result.get("transitions_recorded", 0)
        for k in ("mean_surprise_start", "mean_surprise_end"):
            v = result.get(k)
            if isinstance(v, (int, float)):
                surprise_series.append(float(v))

    trailing = surprise_series[-10:]
    trailing_mean = (sum(trailing) / len(trailing)) if trailing else None
    proven = bool(
        total_transitions >= MIN_TRANSITIONS
        and trailing_mean is not None
        and trailing_mean <= TRAILING_MEAN_MAX
    )

    record = {
        "domain": domain,
        "adapter_spec": adapter_spec,
        "episodes": int(episodes),
        "budget": int(budget),
        "transitions": total_transitions,
        "trailing_mean_surprise": trailing_mean,
        "surprise_series": surprise_series[-20:],
        "proven": proven,
    }
    persisted = _write_proof(key, record) if proven else False

    return {
        "ok": True,
        "name": key,
        "domain": domain,
        "episodes_run": int(episodes),
        "steps": total_steps,
        "transitions": total_transitions,
        "trailing_mean_surprise": trailing_mean,
        "proven": proven,
        "proof_persisted": persisted,
        "proof_path": str(PROOF_PATH),
    }


def is_sandbox_proven(name: str) -> bool:
    """Registry read used by adk.tool_readiness._check_sandbox_proven."""
    rec = _read_proofs().get(name)
    return bool(rec and rec.get("proven"))
