"""Deterministic ODS model resolver.

This module is a THIN WRAPPER over `adk.ods._upstream_select`, which is the ODS
`scripts/select-model.py` vendored byte-for-byte under Apache-2.0. All selection
semantics — memory envelopes, family/profile routing, runtime-profile matching,
the Spark-aarch64 and unified-memory coder-next substitutions, the size ceiling —
live upstream. This file only adapts the result into ADK's typed API.

WHY A WRAPPER AND NOT A REIMPLEMENTATION
----------------------------------------
An earlier version of this file re-derived the selection algorithm in Python
from a prose description of upstream's behaviour. Differential-tested against
the upstream script across 20 hardware envelopes, that re-derivation returned a
different model in 16 of 20 cases — including one identical model for 8GB, 12GB,
24GB, 48GB and 96GB NVIDIA envelopes, because its memory-capacity computation
was wrong. Model selection gates every node that joins the compute market, so
upstream code is carried verbatim and this wrapper stays thin.

`adk/ods/tests/test_differential.py` re-runs that comparison as a real test.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import _upstream_select as ups
from .data import load_catalog
from .model_types import ModelRecord, OdsError, OdsRecommendation

logger = logging.getLogger("adk.ods.resolver")

# Upstream emits a confidence *label*; ADK's API exposes a float. Keep the
# mapping explicit rather than inventing a score.
_CONFIDENCE_TO_FLOAT = {"high": 0.90, "medium": 0.75, "low": 0.60}


@dataclass(frozen=True)
class RolePreference:
    """How one ADK role narrows upstream's feasible set.

    `specialties` is an ORDERED preference list of catalog `specialty` values;
    the first specialty with a fitting model wins. An EMPTY tuple means "this
    role is upstream's own question" — take upstream's pick unchanged.
    `objective` then picks within that specialty group:

      "rank"    — keep upstream's order (most capable that fits).
      "context" — longest context window.
    """

    specialties: tuple[str, ...]
    objective: str = "rank"
    capacity_seeking: bool = False
    """Whether a BIGGER model is a better answer for this role.

    True only where capability scales with size for the work the role names.
    It gates the underfill fallback, and getting it wrong is not subtle:
    measured across eight envelopes, setting it for every role turned `fast`
    into the largest model on the box (a 48GB pick on an A6000) and collapsed
    `chat` onto `balanced` -- five roles answering with one model, which is the
    degeneracy the role system exists to prevent.
    """


# Catalog specialty values, counted over the vendored 52-model library:
#   Fast 10, Chat 9, Quality 7, Reasoning 6, General 5, Balanced 4,
#   Long Context 4, Tool Use 3, Code 3, Enterprise 1.
# There is NO embedding specialty — see `EMBEDDING_ROLE_UNSUPPORTED` below.
ROLE_PREFERENCES: dict[str, RolePreference] = {
    # Every Fast-specialty record in the library is already >=110 tok/s, so
    # within the group CAPABILITY wins, not raw throughput. Ranking by
    # tokens_per_sec_estimate instead picks the 0.5B/350M models on every box,
    # including a 32GB GPU — technically "fastest", useless as a tier.
    "fast": RolePreference(("Fast",)),
    # Upstream's entire policy is "context-aware-largest-capable-GENERAL": its
    # own pick IS the balanced answer, so this role narrows nothing. Filtering
    # to the Balanced specialty would CAP a 96GB host at the largest 7B record
    # that happens to carry that label.
    "balanced": RolePreference(()),
    "reasoning": RolePreference(("Reasoning", "Quality"), capacity_seeking=True),
    # The library's entire Code specialty is three records of 3B and under, so
    # on anything past ~12GB this role's group is exhausted far below the
    # envelope -- see the underfill fallback.
    "coding": RolePreference(("Code", "Tool Use"), capacity_seeking=True),
    "long_context": RolePreference(("Long Context",), objective="context"),
    "chat": RolePreference(("Chat", "Balanced", "General")),
}


def agent_viability(model: dict[str, Any]) -> str | None:
    """Upstream's recorded `agent_viability` status for a model, or None.

    The catalog's `app_compatibility` block is a real evidence trail: each entry
    carries a status, a reason, and a `fleet-test/runs/...` path from an actual
    release probe. 22 of the 52 vendored models are marked `not_agent_viable`
    (e.g. Phi-4 Mini: "ODS Talk release probe failed exact verification after a
    successful load"). None means upstream never tested it — unknown, not bad.
    """
    compat = model.get("_app_compatibility") or model.get("app_compatibility") or {}
    if not isinstance(compat, dict):
        return None
    entry = compat.get("agent_viability")
    if not isinstance(entry, dict):
        return None
    status = entry.get("status")
    return str(status) if status else None


def is_agent_viable(model: dict[str, Any]) -> bool:
    """False ONLY when upstream explicitly recorded a non-verified status.

    Untested models pass. This is deliberately not "verified-only": just 3 of 52
    records carry a verified agent_viability, so requiring it would collapse
    every role on every machine onto the same handful of models — trading one
    degenerate answer for another.
    """
    status = agent_viability(model)
    return status is None or status == "verified"


# A specialty pick may need at most this much LESS memory than the general pick
# before it counts as underfilling the envelope. At 0.5, a specialty match is
# kept whenever it is within 2x of the general pick's requirement.
UNDERFILL_RATIO = 0.5


def _required_gb(model: dict[str, Any]) -> float:
    """Memory the catalog says this record needs, including context/KV."""
    try:
        return float(model.get("vram_required_gb") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _underfills(
    pick: dict[str, Any], general: dict[str, Any], preference: RolePreference
) -> bool:
    """True when a specialty match leaves most of the envelope unused.

    A specialty group can be EXHAUSTED far below the hardware: the library's
    entire Code specialty is three records of 3B and under, so a 24GB box asking
    for a coding model is handed a 1.9GB one while a 16.3GB general model fits.
    That is not a ranking fault to be reordered away -- the group has nothing
    bigger -- so the choice is between a small on-specialty model and a much
    larger off-specialty one, and past some gap the larger model is the better
    answer for an agent.

    Deliberately narrow, because a rule that fires on every envelope would
    disable specialty matching altogether: it needs the general pick to require
    more than TWICE what the specialty pick does. A group holding anything
    comparable in size is left alone.

    Applies ONLY to a `capacity_seeking` role. `fast` wants a SMALL model and
    `long_context` wants the longest window, so a small record winning there is
    the correct answer rather than an underfill; `chat` collapsing onto the
    general pick would make that tier redundant.
    """
    if not preference.capacity_seeking:
        return False
    if preference.objective == "context":
        return False
    if pick is general:
        return False
    pick_gb = _required_gb(pick)
    general_gb = _required_gb(general)
    if pick_gb <= 0 or general_gb <= 0:
        # No usable sizes recorded -- keep the specialty match rather than
        # guessing. An absent number must not decide this.
        return False
    return pick_gb < general_gb * UNDERFILL_RATIO


def _underfill_note(
    pick: dict[str, Any], general: dict[str, Any], specialty: str
) -> str:
    """Say WHICH specialty was passed over and why, never just 'fallback'."""
    return (
        f"the '{specialty}' specialty tops out at {pick.get('name') or pick.get('id')} "
        f"({_required_gb(pick):.0f}GB), well under this envelope; took the larger "
        f"general pick {general.get('name') or general.get('id')} "
        f"({_required_gb(general):.0f}GB) instead"
    )


EMBEDDING_ROLE_UNSUPPORTED = (
    "The ODS catalog is a generation-model library — it contains no embedding "
    "models (no record carries an embedding specialty). Resolve embeddings "
    "through adk.embeddings.CANONICAL_MODEL instead of asking ODS for a pick it "
    "cannot make."
)


def _default_catalog_path() -> Path:
    return Path(__file__).resolve().parent / "model-library.json"


@dataclass(frozen=True)
class _Envelope:
    """Everything upstream derives from a hardware envelope, computed once."""

    ranked: list[dict[str, Any]]
    capacity_gb: float
    memory_label: str
    resolved_profile: str
    confidence_label: str
    tier: str
    memory_type: str
    host_arch: str
    installable_only: bool


def _to_record(model: dict[str, Any]) -> ModelRecord:
    """Adapt an upstream normalised model dict into the typed ModelRecord.

    Upstream's `normalize_model()` guarantees every key read here, so a missing
    key is a genuine contract break and should raise rather than be defaulted.
    """
    return ModelRecord(
        id=model["id"],
        name=model["name"],
        family=model["family"],
        gguf_file=model["gguf_file"],
        gguf_url=model["gguf_url"],
        gguf_sha256=model["gguf_sha256"],
        size_mb=model["size_mb"],
        vram_required_gb=model["vram_required_gb"],
        context_length=model["context_length"],
        quantization=model["quantization"],
        specialty=model["specialty"],
        llm_model_name=model["llm_model_name"],
        install_recommendation=model["install_recommendation"],
        runtime_profiles=model.get("runtime_profiles") or [],
        # Re-joined by OdsResolver.models — upstream's normalize_model() drops
        # it, so reading the plain key here yielded {} for every record.
        app_compatibility=model.get("_app_compatibility")
        or model.get("app_compatibility")
        or {},
        # Annotated onto the normalised dict by OdsResolver.models — upstream's
        # normalize_model() drops it, but role selection needs it.
        tokens_per_sec_estimate=float(model.get("_tokens_per_sec_estimate") or 0.0),
    )


class OdsResolver:
    """Deterministic, offline model selection from the vendored ODS catalog."""

    def __init__(self, catalog_path: str | None = None) -> None:
        self.catalog_path = catalog_path
        self._catalog: dict[str, Any] | None = None
        self._models: list[dict[str, Any]] | None = None
        # Fail fast on a missing/corrupt catalog rather than at first resolve().
        _ = self.catalog

    @property
    def catalog(self) -> dict[str, Any]:
        """Raw catalog dict (schema-validated). Raises OdsError if unusable."""
        if self._catalog is None:
            self._catalog = load_catalog(self.catalog_path)
        return self._catalog

    @property
    def models(self) -> list[dict[str, Any]]:
        """Upstream-normalised model list used for selection."""
        if self._models is None:
            path = Path(self.catalog_path) if self.catalog_path else _default_catalog_path()
            try:
                self._models = ups.load_catalog(path)
            except (OSError, ValueError) as e:
                raise OdsError(f"Failed to load catalog for selection: {e}") from e
            if not self._models:
                raise OdsError(
                    f"Model catalog at {path} contains no usable models — refusing to "
                    "return an empty pick (fail closed)."
                )
            # Re-join the fields upstream's normalize_model() drops but role
            # selection needs. Annotated with a leading underscore so they can
            # never be confused with an upstream-guaranteed key.
            by_id = {
                str(raw.get("id")): raw
                for raw in self.catalog.get("models", [])
                if raw.get("id")
            }
            for model in self._models:
                raw = by_id.get(model["id"]) or {}
                model["_tokens_per_sec_estimate"] = float(
                    raw.get("tokens_per_sec_estimate") or 0.0
                )
                # app_compatibility carries upstream's REAL fleet-test evidence
                # per model per app. `_to_record` read it straight off the
                # normalised dict, where it never exists, so ModelRecord's
                # app_compatibility was silently always empty.
                model["_app_compatibility"] = raw.get("app_compatibility") or {}
        return self._models

    def _envelope(
        self,
        backend: str,
        memory_type: str | None,
        vram_mb: int,
        ram_gb: int,
        profile: str,
        tier: str | None,
        host_arch: str,
        max_size_mb: float | None,
        installable_only: bool,
    ) -> _Envelope:
        """Run upstream's feasibility pass for a hardware envelope.

        This is the ONLY place candidates are produced. `resolve()` and
        `resolve_role()` both consume it, so a role pick can never be a model
        upstream judged infeasible — role preference reorders the feasible set,
        it never widens it.
        """
        catalog = self.models
        tier_value = tier if tier is not None else "1"
        ceiling = float(max_size_mb) if max_size_mb else 0.0
        memory_type_value = memory_type if memory_type is not None else "discrete"

        resolved_profile = ups.effective_profile(
            ups.normalize_profile(profile), backend, tier_value
        )
        capacity_gb, memory_label = ups.usable_memory_gb(
            backend, memory_type_value, vram_mb, ram_gb
        )
        confidence_label = (
            "high"
            if ups.normalize_key(backend) not in {"unknown", "none"} and capacity_gb > 0
            else "medium"
        )
        ranked = ups.rank_models(
            catalog,
            capacity_gb,
            resolved_profile,
            installable_only,
            backend,
            memory_type_value,
            vram_mb,
            ram_gb,
            host_arch,
            ceiling,
        )
        if not ranked:
            raise OdsError(
                "No model in the ODS catalog satisfies the constraints "
                f"(backend={backend}, capacity={capacity_gb:.1f}GB, "
                f"profile={resolved_profile}, installable_only={installable_only}, "
                f"max_size_mb={ceiling or 'unbounded'}). Failing closed."
            )
        return _Envelope(
            ranked=ranked,
            capacity_gb=capacity_gb,
            memory_label=memory_label,
            resolved_profile=resolved_profile,
            confidence_label=confidence_label,
            tier=tier_value,
            memory_type=memory_type_value,
            host_arch=host_arch,
            installable_only=installable_only,
        )

    def resolve(
        self,
        backend: str,
        memory_type: str | None,
        vram_mb: int,
        ram_gb: int,
        profile: str,
        tier: str | None = None,
        host_arch: str = "x86_64",
        max_size_mb: float | None = None,
        installable_only: bool = False,
    ) -> OdsRecommendation:
        """Resolve a model for the given hardware envelope.

        Mirrors `select-model.py main()` exactly, then adapts to the typed API.

        Raises:
            OdsError: catalog unusable, or no candidate survives the constraints.
                      Never returns an empty/None pick.
        """
        env = self._envelope(
            backend, memory_type, vram_mb, ram_gb, profile, tier,
            host_arch, max_size_mb, installable_only,
        )
        return self._finalize(env, env.ranked[0], backend, role=None)

    def resolve_role(
        self,
        role: str,
        backend: str,
        memory_type: str | None,
        vram_mb: int,
        ram_gb: int,
        profile: str,
        tier: str | None = None,
        host_arch: str = "x86_64",
        max_size_mb: float | None = None,
        installable_only: bool = False,
        require_agent_viable: bool = True,
    ) -> OdsRecommendation:
        """Resolve the best model for a ROLE on the given hardware envelope.

        Upstream ODS answers one question — "which single model should this box
        install" — so calling `resolve()` once per ADK tier returns the same
        model five times. This narrows upstream's *feasible* set by the
        role's preferred catalog specialties; it never adds a candidate upstream
        rejected, and the arch-policy substitution still applies to the role's
        own pick, so a unified-memory host cannot be handed the coder-next model
        by the back door.

        When no model of a preferred specialty fits, this falls back to
        upstream's overall best pick and says so in `reason` — a degraded answer
        that is labelled, not a silent one.

        Raises:
            OdsError: unknown role, the embedding role (no such models exist in
                      the catalog), or no candidate survives the constraints.
        """
        role_key = str(role or "").strip().lower()
        if role_key == "embedding":
            raise OdsError(EMBEDDING_ROLE_UNSUPPORTED)
        preference = ROLE_PREFERENCES.get(role_key)
        if preference is None:
            raise OdsError(
                f"Unknown ODS role {role!r}. Known roles: "
                f"{', '.join(sorted(ROLE_PREFERENCES))}."
            )

        env = self._envelope(
            backend, memory_type, vram_mb, ram_gb, profile, tier,
            host_arch, max_size_mb, installable_only,
        )
        selected, matched_specialty, viability_note = self._pick_for_role(
            env, preference, require_agent_viable
        )
        return self._finalize(
            env, selected, backend, role=role_key,
            matched_specialty=matched_specialty, viability_note=viability_note,
        )

    @staticmethod
    def _pick_for_role(
        env: _Envelope, preference: RolePreference, require_agent_viable: bool = True
    ) -> tuple[dict[str, Any], str | None, str | None]:
        """Best feasible model for a role, preferring agent-viable candidates.

        ADK is an AGENT SDK, so a model upstream's own release probes recorded as
        `not_agent_viable` is the wrong answer even when it fits and matches the
        specialty. Measured before this filter existed: 14 role picks across six
        realistic envelopes landed on such a model.

        The filter narrows within `env.ranked` — it never widens it — and if it
        would empty the candidate set the unfiltered pick is returned WITH a note,
        so a degraded answer is labelled rather than silent.
        """
        pool = env.ranked
        note = None
        if require_agent_viable:
            viable = [m for m in pool if is_agent_viable(m)]
            if viable:
                pool = viable
            else:
                note = (
                    "no agent-viable model fits this envelope; kept upstream's "
                    "pick despite a recorded non-viable status"
                )

        def _from(candidates: list[dict[str, Any]], specialty: str) -> dict[str, Any]:
            if preference.objective == "context":
                # Already in upstream rank order and max() is stable, so equal
                # context lengths keep upstream's preference.
                return max(candidates, key=lambda m: int(m.get("context_length") or 0))
            return candidates[0]

        for specialty in preference.specialties:
            group = [
                m for m in pool
                if str(m.get("specialty") or "").lower() == specialty.lower()
            ]
            if not group:
                continue
            pick = _from(group, specialty)
            if _underfills(pick, pool[0], preference):
                return pool[0], None, _underfill_note(pick, pool[0], specialty)
            return pick, specialty, note
        return pool[0], None, note

    def _finalize(
        self,
        env: _Envelope,
        selected: dict[str, Any],
        backend: str,
        role: str | None,
        matched_specialty: str | None = None,
        viability_note: str | None = None,
    ) -> OdsRecommendation:
        """Apply upstream's arch policy to `selected` and build the result.

        Kept common to `resolve()` and `resolve_role()` so the hardware-safety
        substitution can never be skipped on one path but not the other.
        """
        catalog = self.models
        arch_selected, arch_policy_tag = ups.arch_policy_model(
            catalog,
            env.tier,
            env.resolved_profile,
            env.host_arch,
            env.memory_type,
            env.installable_only,
            selected,
        )
        if arch_selected:
            selected = arch_selected
            alternatives = [selected] + [
                m
                for m in env.ranked
                if m["id"] != selected["id"] and not ups.is_spark_aarch64_excluded_model(m)
            ][:2]
            policy = f"{ups.POLICY}+{arch_policy_tag}"
            reason = ups.arch_policy_reason(
                selected, env.capacity_gb, env.memory_label, arch_policy_tag
            )
        else:
            alternatives = [selected] + [m for m in env.ranked if m["id"] != selected["id"]][:2]
            policy = ups.POLICY
            reason = ups.recommendation_reason(
                selected, env.capacity_gb, env.memory_label, backend, env.confidence_label
            )

        if role is not None:
            policy = f"{policy}+role-{role}"
            wanted = ROLE_PREFERENCES[role].specialties
            if matched_specialty:
                reason = f"{reason} Role '{role}' matched specialty '{matched_specialty}'."
            elif not wanted:
                reason = (
                    f"{reason} Role '{role}' takes upstream's overall best-fit pick "
                    "by design — it narrows nothing."
                )
            else:
                reason = (
                    f"{reason} Role '{role}' wanted [{', '.join(wanted)}] but no such "
                    f"model fits {env.capacity_gb:.1f}GB — fell back to the overall "
                    "best-fit pick."
                )
            if viability_note:
                reason = f"{reason} WARNING: {viability_note}."
            else:
                status = agent_viability(selected)
                if status and status != "verified":
                    # Reachable only via the arch-policy substitution, which
                    # overrides the role pick for hardware-safety reasons.
                    reason = (
                        f"{reason} NOTE: upstream records agent_viability="
                        f"'{status}' for this model."
                    )

        logger.debug(
            "ODS resolve%s: backend=%s capacity=%.1fGB profile=%s tier=%s -> %s (policy=%s)",
            f" role={role}" if role else "", backend, env.capacity_gb,
            env.resolved_profile, env.tier, selected["id"], policy,
        )

        return OdsRecommendation(
            policy=policy,
            source="ods",
            confidence=_CONFIDENCE_TO_FLOAT.get(env.confidence_label, 0.75),
            profile=env.resolved_profile,
            host_arch=ups.normalize_host_arch(env.host_arch),
            memory_capacity_gb=round(env.capacity_gb, 1),
            memory_label=env.memory_label,
            selected=_to_record(selected),
            reason=reason,
            alternatives=[_to_record(m) for m in alternatives],
        )
