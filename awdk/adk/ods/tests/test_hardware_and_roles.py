"""Host classification and role-differentiated selection.

These two changes exist because of two concrete defects:

* `recommend_config()` returned the SAME model for all five tiers — it called
  `resolve()` five times with an identical envelope, and upstream ODS answers
  "one model for this box", not "one per role".
* `hardware-classes.json` and `gpu-database.json` were vendored, shipped and
  hash-pinned with NO code path reading either, while the resolver was called
  with `tier=None` (pinned to "1"), which made upstream's Spark/GB10 arch-policy
  guard structurally unreachable.

Every test below asserts something POSITIVE (a real pick, a real tier), not just
that a bad input is rejected — an inert implementation passes negative-only
suites trivially.
"""

from __future__ import annotations

import pytest

from adk.ods.hardware import classify_host, current_platform_id
from adk.ods.model_types import OdsError
from adk.ods.resolver import ROLE_PREFERENCES, OdsResolver

RTX_5090 = dict(backend="nvidia", memory_type="discrete", vram_mb=32607, ram_gb=128)
GPU_8GB = dict(backend="nvidia", memory_type="discrete", vram_mb=8192, ram_gb=32)
CPU_ONLY = dict(backend="cpu", memory_type="discrete", vram_mb=0, ram_gb=16)
STRIX = dict(backend="amd", memory_type="unified", vram_mb=98304, ram_gb=128)


@pytest.fixture(scope="module")
def resolver() -> OdsResolver:
    return OdsResolver()


# ── Roles must actually differ ────────────────────────────────────────────────


@pytest.mark.parametrize("envelope", [RTX_5090, GPU_8GB, CPU_ONLY, STRIX])
def test_roles_are_not_degenerate(resolver: OdsResolver, envelope: dict) -> None:
    """The regression itself: five roles must not collapse to one model."""
    picks = {
        role: resolver.resolve_role(role, profile="qwen", **envelope).selected.id
        for role in ("fast", "balanced", "reasoning", "coding", "long_context")
    }
    assert len(set(picks.values())) >= 3, f"roles collapsed: {picks}"


@pytest.mark.parametrize(
    "role,expected_specialty",
    [
        ("fast", "Fast"),
        ("reasoning", "Reasoning"),
        ("long_context", "Long Context"),
        ("chat", "Chat"),
    ],
)
def test_role_matches_its_specialty_when_one_fits(
    resolver: OdsResolver, role: str, expected_specialty: str
) -> None:
    """POSITIVE assertion: on a large box the role gets its own specialty.

    `coding` is deliberately absent — its specialty group is EXHAUSTED on a box
    this size, which the two tests below pin in both directions.
    """
    rec = resolver.resolve_role(role, profile="qwen", **RTX_5090)
    assert rec.selected.specialty == expected_specialty
    assert f"role-{role}" in rec.policy
    assert f"matched specialty '{expected_specialty}'" in rec.reason


def test_coding_keeps_the_dedicated_coder_when_it_fills_the_box(
    resolver: OdsResolver,
) -> None:
    """The direction that must NOT change: a small box keeps its coder.

    Without this half the underfill fallback could be widened until it fires
    everywhere, which would delete specialty matching for coding entirely while
    the other test still passed.
    """
    rec = resolver.resolve_role("coding", profile="qwen", **GPU_8GB)
    assert rec.selected.specialty == "Code"
    assert "tops out at" not in (rec.reason or "")


def test_coding_takes_the_larger_general_pick_when_its_group_is_exhausted(
    resolver: OdsResolver,
) -> None:
    """A 1.9GB coder is the wrong answer for an agent on a 32GB card.

    The library's entire Code specialty is three records of 3B and under, so
    this is not a ranking fault that reordering could fix — the group has
    nothing bigger. The pick must be the larger general model AND must SAY it
    passed over the specialty, so a degraded answer is labelled rather than
    silent.
    """
    coder = resolver.resolve_role("coding", profile="qwen", **GPU_8GB).selected
    rec = resolver.resolve_role("coding", profile="qwen", **RTX_5090)

    assert rec.selected.vram_required_gb > coder.vram_required_gb * 2
    assert "tops out at" in rec.reason
    assert "'Code'" in rec.reason, "the note must name the specialty passed over"
    assert rec.selected.id == resolver.resolve(profile="qwen", **RTX_5090).selected.id


def test_underfill_fallback_leaves_the_small_roles_alone(
    resolver: OdsResolver,
) -> None:
    """`fast` and `long_context` must never be pulled up to the general pick.

    Measured while writing the rule: applying it to every role turned `fast`
    into the LARGEST model on the box and collapsed `chat` onto `balanced` —
    five roles answering with one model, the degeneracy this suite exists for.
    """
    general = resolver.resolve(profile="qwen", **RTX_5090).selected
    for role in ("fast", "long_context", "chat"):
        rec = resolver.resolve_role(role, profile="qwen", **RTX_5090)
        assert rec.selected.id != general.id, f"{role} collapsed onto the general pick"
        assert "tops out at" not in (rec.reason or "")


def test_balanced_role_is_upstream_pick_unchanged(resolver: OdsResolver) -> None:
    """`balanced` narrows nothing by design — it must equal plain resolve()."""
    baseline = resolver.resolve(profile="qwen", **RTX_5090)
    balanced = resolver.resolve_role("balanced", profile="qwen", **RTX_5090)
    assert balanced.selected.id == baseline.selected.id
    assert "narrows nothing" in balanced.reason


def test_role_never_selects_a_model_upstream_rejected(resolver: OdsResolver) -> None:
    """A role reorders the feasible set; it must never widen it."""
    tiny = dict(backend="nvidia", memory_type="discrete", vram_mb=4096, ram_gb=8)
    for role in ROLE_PREFERENCES:
        rec = resolver.resolve_role(role, profile="qwen", **tiny)
        # 4GB + upstream's fit tolerance; nothing larger may be selected.
        assert rec.selected.vram_required_gb <= rec.memory_capacity_gb + 0.25, (
            f"{role} picked {rec.selected.id} needing "
            f"{rec.selected.vram_required_gb}GB in a {rec.memory_capacity_gb}GB envelope"
        )


def test_role_fallback_is_labelled_not_silent(resolver: OdsResolver) -> None:
    """No Reasoning model fits 4GB — the degraded answer must SAY so."""
    tiny = dict(backend="nvidia", memory_type="discrete", vram_mb=4096, ram_gb=8)
    rec = resolver.resolve_role("reasoning", profile="qwen", **tiny)
    assert "fell back to the overall best-fit pick" in rec.reason


def test_arch_policy_still_applies_to_a_role_pick(resolver: OdsResolver) -> None:
    """The coding role picks coder-next; unified memory must substitute it.

    This is the composition that matters: role narrowing must not become a back
    door around a HARDWARE-SAFETY substitution (coder-next emits all-`?` tokens
    on unified-memory backends).
    """
    rec = resolver.resolve_role("coding", profile="qwen", **STRIX)
    assert rec.selected.llm_model_name != "qwen3-coder-next"
    assert "unified-memory-coder-next-a3b-v1" in rec.policy


def test_embedding_role_refuses_rather_than_faking(resolver: OdsResolver) -> None:
    """The catalog has zero embedding models — asking must raise, not guess."""
    with pytest.raises(OdsError, match="no embedding models"):
        resolver.resolve_role("embedding", profile="qwen", **RTX_5090)


def test_unknown_role_raises(resolver: OdsResolver) -> None:
    with pytest.raises(OdsError, match="Unknown ODS role"):
        resolver.resolve_role("telepathy", profile="qwen", **RTX_5090)


def test_tokens_per_sec_is_populated_from_the_catalog(resolver: OdsResolver) -> None:
    """upstream's normalize_model() drops this field; the re-join must work."""
    rec = resolver.resolve_role("fast", profile="qwen", **RTX_5090)
    assert rec.selected.tokens_per_sec_estimate > 0


# ── The vendored hardware data must be load-bearing ────────────────────────────


def test_known_gpu_corrects_a_wrong_probe() -> None:
    """A Strix Halo APU probes as discrete AMD with little VRAM; specs win."""
    host = classify_host(
        gpu_name="AMD Radeon 8060S Graphics",
        vendor="amd",
        memory_type="discrete",
        vram_mb=512,
        ram_gb=128,
    )
    assert host.source == "known_gpu"
    assert host.memory_type == "unified"
    assert host.tier == "SH_LARGE"
    assert host.vram_mb == 98304


def test_gb10_classifies_as_nv_ultra_which_unlocks_the_spark_guard() -> None:
    """The whole point of wiring tier: NV_ULTRA + arm64 fires the substitution."""
    host = classify_host(gpu_name="NVIDIA GB10 Grace Blackwell", vendor="nvidia", ram_gb=128)
    assert host.tier == "NV_ULTRA"

    rec = OdsResolver().resolve(
        backend=host.backend,
        memory_type=host.memory_type,
        vram_mb=host.vram_mb,
        ram_gb=128,
        profile="qwen",
        tier=host.tier,
        host_arch="arm64",
    )
    assert "spark-aarch64-nv-ultra-a3b-v1" in rec.policy

    # And the counter-proof: with the old hardcoded tier the guard is dead.
    stale = OdsResolver().resolve(
        backend=host.backend,
        memory_type="discrete",
        vram_mb=host.vram_mb,
        ram_gb=128,
        profile="qwen",
        tier="1",
        host_arch="arm64",
    )
    assert "spark-aarch64" not in stale.policy


@pytest.mark.parametrize(
    "vendor,memory_type,vram_mb,ram_gb,expected_tier",
    [
        ("nvidia", "discrete", 98304, 128, "NV_ULTRA"),
        ("nvidia", "discrete", 49152, 128, "T4"),
        ("nvidia", "discrete", 24576, 64, "T3"),
        ("nvidia", "discrete", 12288, 32, "T2"),
        ("nvidia", "discrete", 2048, 16, "T0"),   # low-VRAM CPU fallback band
        ("amd", "discrete", 24576, 64, "T3"),
        ("apple", "unified", 0, 64, "T3"),
        ("cpu", "none", 0, 16, "T1"),
    ],
)
def test_heuristic_ladder_places_hosts(
    vendor: str, memory_type: str, vram_mb: int, ram_gb: int, expected_tier: str
) -> None:
    host = classify_host(
        vendor=vendor, memory_type=memory_type, vram_mb=vram_mb, ram_gb=ram_gb
    )
    assert host.tier == expected_tier
    assert host.source in {"heuristic_class", "known_gpu"}


def test_cpu_vendor_reaches_the_cpu_rung_not_the_default() -> None:
    """ADK says 'cpu', ODS writes 'none' — the map must bridge that."""
    host = classify_host(vendor="cpu", memory_type="discrete", vram_mb=0, ram_gb=16)
    assert host.source == "heuristic_class"
    assert host.id == "cpu_only"


def test_unknown_host_degrades_conservatively_without_raising() -> None:
    host = classify_host(gpu_name="Something Nobody Ships", vendor="", vram_mb=0, ram_gb=8)
    assert host.source == "unknown"
    assert host.backend == "cpu"
    assert host.tier == "T1"


def test_overlays_key_on_backend_not_platform() -> None:
    """Overlays follow the resolved BACKEND, on every platform.

    An earlier revision looked overlays up in hardware-classes.json by
    platform+vendor+VRAM and returned NOTHING for a Windows-native NVIDIA host,
    because upstream only enumerates linux/wsl/macos classes there. Upstream's
    classifier keys on the backend (OVERLAY_MAP) with one macOS override, and
    its own contract test asserts hardware-classes.json merely MIRRORS that map
    — see test_classify_differential.py.
    """
    for platform_id in ("windows", "linux", "wsl", "unknown"):
        host = classify_host(
            vendor="nvidia", memory_type="discrete", vram_mb=24576, ram_gb=64,
            platform_id=platform_id,
        )
        assert host.compose_overlays == (
            "docker-compose.base.yml", "docker-compose.nvidia.yml"
        ), platform_id


def test_apple_gets_the_macos_overlay_only_on_macos() -> None:
    kwargs = dict(vendor="apple", memory_type="unified", vram_mb=0, ram_gb=64)
    on_mac = classify_host(platform_id="macos", **kwargs)
    on_linux = classify_host(platform_id="linux", **kwargs)
    assert on_mac.compose_overlays[1] == "installers/macos/docker-compose.macos.yml"
    assert on_linux.compose_overlays[1] == "docker-compose.apple.yml"


def test_current_platform_id_is_a_known_token() -> None:
    assert current_platform_id() in {"linux", "wsl", "macos", "windows", "unknown"}


# ── Role picks must respect upstream's own agent-viability evidence ────────────


def _raw_catalog() -> dict:
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "model-library.json"
    return {m["id"]: m for m in json.loads(path.read_text(encoding="utf-8"))["models"]}


ALL_ENVELOPES = [
    ("rtx5090", RTX_5090),
    ("gpu8gb", GPU_8GB),
    ("cpu16gb", CPU_ONLY),
    ("strix", STRIX),
    ("gpu24gb", dict(backend="nvidia", memory_type="discrete", vram_mb=24576, ram_gb=64)),
    ("gpu12gb", dict(backend="nvidia", memory_type="discrete", vram_mb=12288, ram_gb=32)),
]


@pytest.mark.parametrize("label,envelope", ALL_ENVELOPES)
def test_no_role_recommends_a_model_upstream_proved_not_agent_viable(
    resolver: OdsResolver, label: str, envelope: dict
) -> None:
    """ADK is an AGENT SDK; a model upstream's release probes recorded as
    `not_agent_viable` is the wrong answer even when it fits and matches.

    Measured before the filter existed: 14 picks across these six envelopes
    landed on such a model (qwen2.5-1.5b, phi3-mini-128k, phi4-mini-reasoning).
    """
    raw = _raw_catalog()
    offenders = []
    for role in ROLE_PREFERENCES:
        rec = resolver.resolve_role(role, profile="qwen", **envelope)
        entry = (raw.get(rec.selected.id, {}).get("app_compatibility") or {}).get(
            "agent_viability"
        )
        status = entry.get("status") if isinstance(entry, dict) else None
        if status and status != "verified":
            offenders.append((role, rec.selected.id, status))
    assert not offenders, f"{label}: {offenders}"


def test_agent_viability_filter_does_not_collapse_the_roles(
    resolver: OdsResolver,
) -> None:
    """The filter must not create a new role degeneracy.

    Requiring a *verified* status (only 3 of 52 records have one) would do
    exactly that, which is why `is_agent_viable` treats untested as allowed.
    """
    picks = {
        role: resolver.resolve_role(role, profile="qwen", **RTX_5090).selected.id
        for role in ROLE_PREFERENCES
    }
    assert len(set(picks.values())) >= 4, picks


def test_untested_models_are_allowed_only_explicit_failures_are_not() -> None:
    from adk.ods.resolver import agent_viability, is_agent_viable

    assert is_agent_viable({}) is True                      # never tested
    assert is_agent_viable({"_app_compatibility": {}}) is True
    assert is_agent_viable(
        {"_app_compatibility": {"agent_viability": {"status": "verified"}}}
    ) is True
    assert is_agent_viable(
        {"_app_compatibility": {"agent_viability": {"status": "not_agent_viable"}}}
    ) is False
    assert agent_viability({"_app_compatibility": {"agent_viability": {"status": "x"}}}) == "x"
    assert agent_viability({"_app_compatibility": {"agent_viability": "junk"}}) is None


def test_app_compatibility_reaches_the_typed_record(resolver: OdsResolver) -> None:
    """It was ALWAYS empty: upstream's normalize_model() drops the key, and
    _to_record read the plain name. A silent no-op on a field the filter needs.
    """
    seen = 0
    for role in ROLE_PREFERENCES:
        rec = resolver.resolve_role(role, profile="qwen", **RTX_5090)
        if rec.selected.app_compatibility:
            seen += 1
    assert seen > 0, "app_compatibility is empty on every record — re-join broken"


def test_opting_out_restores_the_unfiltered_pick(resolver: OdsResolver) -> None:
    """The filter is a default, not a cage — and the opt-out must really differ."""
    on = resolver.resolve_role("fast", profile="qwen", **RTX_5090).selected.id
    off = resolver.resolve_role(
        "fast", profile="qwen", require_agent_viable=False, **RTX_5090
    ).selected.id
    assert on != off, (on, off)
