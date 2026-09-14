"""Deep differential sweep: OdsResolver vs the vendored upstream selector.

`adk/ods/tests/test_differential.py` runs 20 curated envelopes on every test run.
THIS tool runs a much wider randomised sweep (default 250 cases) over
tier-threshold VRAM values, installable_only, size ceilings, and unknown
backends/arches. It is deliberately NOT part of the default pytest run because
each case spawns a subprocess (~1.5s); a full sweep takes several minutes.

RUN THIS AFTER EVERY RE-VENDOR, and after any change to resolver.py.

    python adk/ods/tools/sweep_differential.py            # 250 cases
    python adk/ods/tools/sweep_differential.py 1000       # deeper

Exit code 0 = every case agrees with upstream. Non-zero = divergence, printed
with the exact envelope that broke.

Why it exists: an earlier resolver reimplementation passed a curated unit suite
while disagreeing with upstream on 16 of 20 envelopes. Curated cases prove only
what the author thought to check; the sweep hunts the thin spots.
"""

from __future__ import annotations

import itertools
import json
import random
import subprocess
import sys
from pathlib import Path

_ODS_DIR = Path(__file__).resolve().parent.parent
_REPO = _ODS_DIR.parent.parent
CATALOG = _ODS_DIR / "model-library.json"

if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from adk.ods.model_types import OdsError  # noqa: E402
from adk.ods.resolver import ROLE_PREFERENCES, OdsResolver  # noqa: E402

# Threshold-adjacent VRAM values (ODS tier edges sit at 6/8/12/24/40/48/90GB),
# probed at n-1 / n / n+1 so off-by-one comparisons surface.
VRAMS = [
    0, 1, 2047, 2048, 4095, 4096, 6143, 6144, 6145, 8191, 8192, 8193,
    12287, 12288, 12289, 16384, 24575, 24576, 24577, 32607, 40959, 40960,
    49151, 49152, 65536, 92159, 92160, 92161, 98304, 131072,
]
RAMS = [1, 2, 4, 8, 16, 24, 32, 48, 64, 96, 128, 256]
PROFILES = ["qwen", "gemma4", "auto"]
TIERS = ["0", "1", "2", "3", "4", "CLOUD", "NV_ULTRA", "SH_LARGE", "SH_COMPACT",
         "ARC", "ARC_LITE", None]
BACKENDS = ["cpu", "nvidia", "amd", "apple", "intel", "unknown", "none"]
MEM_TYPES = ["discrete", "unified", None]
ARCHES = ["x86_64", "arm64", "unknown"]
CEILINGS = [None, 100.0, 5000.0, 50000.0]


def _upstream(case: tuple) -> tuple[str, str]:
    backend, mem, vram, ram, prof, tier, arch, inst, ceil = case
    cmd = [
        sys.executable, "-m", "adk.ods._upstream_select",
        "--catalog", str(CATALOG),
        "--backend", backend, "--vram-mb", str(vram), "--ram-gb", str(ram),
        "--profile", prof, "--tier", tier or "1", "--host-arch", arch,
    ]
    if mem:
        cmd += ["--memory-type", mem]
    if inst:
        cmd += ["--installable-only"]
    if ceil:
        cmd += ["--max-size-mb", str(ceil)]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(_REPO), check=False)
    if proc.returncode != 0:
        tail = proc.stderr.strip().splitlines()[-1][:120] if proc.stderr else "rc!=0"
        return ("error", tail)
    return ("ok", json.loads(proc.stdout)["selected"]["id"])


def _ours(resolver: OdsResolver, case: tuple) -> tuple[str, str]:
    backend, mem, vram, ram, prof, tier, arch, inst, ceil = case
    try:
        rec = resolver.resolve(
            backend=backend, memory_type=mem, vram_mb=vram, ram_gb=ram,
            profile=prof, tier=tier, host_arch=arch,
            installable_only=inst, max_size_mb=ceil,
        )
    except OdsError as e:
        return ("error", str(e)[:120])
    return ("ok", rec.selected.id)


def _role_invariants(resolver: OdsResolver, case: tuple) -> list[str]:
    """Check what CAN be checked about role picks.

    There is no upstream reference for `resolve_role()` — upstream has no notion
    of roles — so this cannot be a differential. What it can assert is the
    invariant that makes role selection safe: a role pick must come from the
    feasible set upstream produced for the SAME envelope (or be upstream's own
    arch-policy substitution). A role that widens the candidate set could hand a
    host a model that does not fit.
    """
    backend, mem, vram, ram, prof, tier, arch, inst, ceil = case
    kwargs = dict(
        backend=backend, memory_type=mem, vram_mb=vram, ram_gb=ram,
        profile=prof, tier=tier, host_arch=arch,
        installable_only=inst, max_size_mb=ceil,
    )
    try:
        baseline = resolver.resolve(**kwargs)
    except OdsError:
        return []  # envelope rejected outright; roles are expected to match
    feasible = {m["id"] for m in resolver._envelope(  # noqa: SLF001 - tool, not API
        backend, mem, vram, ram, prof, tier, arch, ceil, inst,
    ).ranked}
    feasible.add(baseline.selected.id)  # arch-policy substitution is legitimate

    problems = []
    for role in ROLE_PREFERENCES:
        try:
            rec = resolver.resolve_role(role, **kwargs)
        except OdsError as e:
            problems.append(f"role={role} raised: {str(e)[:80]}")
            continue
        if rec.selected.id not in feasible:
            problems.append(
                f"role={role} picked {rec.selected.id}, which is NOT in upstream's "
                f"feasible set for this envelope"
            )
        if rec.selected.vram_required_gb > rec.memory_capacity_gb + 0.25:
            # The one exception upstream itself allows: when nothing fits, it
            # returns the smallest model anyway rather than nothing.
            if rec.selected.id != baseline.selected.id:
                problems.append(
                    f"role={role} picked {rec.selected.id} needing "
                    f"{rec.selected.vram_required_gb}GB in a "
                    f"{rec.memory_capacity_gb}GB envelope"
                )
    return problems


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 250
    # Fixed seed: a sweep that shuffles differently every run makes a failure
    # impossible to reproduce from the exit code alone.
    rng = random.Random(20260725)
    pool = list(itertools.product(
        BACKENDS, MEM_TYPES, VRAMS, RAMS, PROFILES, TIERS, ARCHES,
        [False, True], CEILINGS,
    ))
    rng.shuffle(pool)
    cases = pool[:n]

    resolver = OdsResolver(catalog_path=str(CATALOG))
    match = divergent = both_err = 0
    failures: list[tuple] = []
    role_problems: list[str] = []

    for i, case in enumerate(cases, 1):
        role_problems.extend(f"{case}: {p}" for p in _role_invariants(resolver, case))
        u_kind, u_val = _upstream(case)
        o_kind, o_val = _ours(resolver, case)
        if u_kind == "ok" and o_kind == "ok":
            if u_val == o_val:
                match += 1
            else:
                divergent += 1
                failures.append((case, u_val, o_val))
        elif u_kind == "error" and o_kind == "error":
            both_err += 1
        else:
            divergent += 1
            failures.append((case, f"{u_kind}:{u_val}", f"{o_kind}:{o_val}"))
        if i % 25 == 0:
            print(f"  ...{i}/{len(cases)} match={match} divergent={divergent} "
                  f"both_errored={both_err}", flush=True)

    print(f"\nmatch={match}  divergent={divergent}  both_errored={both_err}  "
          f"total={len(cases)}")
    print(f"role-invariant violations={len(role_problems)}")
    for case, up, ours_ in failures[:15]:
        print(f"  DIVERGE {case}\n     upstream={up}\n     ours    ={ours_}")
    for problem in role_problems[:15]:
        print(f"  ROLE {problem}")
    if divergent or role_problems:
        print("\n[FAIL] resolver.py disagrees with the vendored upstream selector "
              "or violates a role invariant.")
        return 1
    print("[OK] resolver matches upstream on every sampled envelope, and every "
          "role pick came from upstream's feasible set")
    return 0


if __name__ == "__main__":
    sys.exit(main())
