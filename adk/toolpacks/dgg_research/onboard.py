"""Self-onboarding: pick the brain the machine can actually run, then say so.

The ask this answers: someone who does not have Claude, and may not have a GPU,
should get the same research agent — so the pack has to look at the host and
choose, rather than shipping a config the operator is expected to edit before
anything works.

WHY IT IS A LADDER AND NOT A DEFAULT.

A default that assumes a GPU fails on the laptop where most of this research
actually happens, and a default that assumes CPU wastes a 5090 that was sitting
right there. Both are "works on my machine" in opposite directions. So the tiers
are ORDERED and the host picks: an operator's own endpoint wins if they declared
one (they are paying for it and know why), then a GPU big enough for Bonsai-27B,
then CPU llama.cpp, which runs anywhere and is honest about being slower.

WHAT IT REFUSES TO DO.

It never silently falls back to a hosted API. If nothing local is available and
the operator declared no endpoint, it says so and stops. A research agent that
quietly starts posting a private corpus to somebody's cloud because the local
model would not load is the one failure here that cannot be walked back.

Detection is best-effort and its uncertainty is REPORTED, not hidden: an
unreadable GPU is "unknown", never "absent", because those two lead to opposite
correct actions.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

#: Ordered worst-acceptable LAST. The resolver walks this top down and takes the
#: first tier the host satisfies.
TIERS = (
    {
        "id": "byo",
        "why": "the operator declared their own endpoint; they are paying for "
               "it and know why",
        "requires": "OPENAI_BASE_URL or AITHER_INFERENCE_URL set",
    },
    {
        "id": "gpu-bonsai-27b",
        "why": "same pack, same tools, same method -- only the reasoning gets "
               "better",
        "requires": "a GPU with >= 13 GB usable VRAM",
        "vram_gb": 13,
        "backend": "llamacpp",
        "model": "Bonsai-27B",
    },
    {
        "id": "cpu-bonsai-4b",
        "why": "runs on any laptop, and a research agent that needs a "
               "datacentre is not available to the people doing the research",
        "requires": "nothing",
        "ram_gb": 2,
        "backend": "llamacpp",
        "model": "Bonsai-4B-Q1",
    },
)


def declared_endpoint() -> str:
    for var in ("AITHER_INFERENCE_URL", "OPENAI_BASE_URL", "DGG_INFERENCE_URL"):
        v = (os.environ.get(var) or "").strip()
        if v:
            return v
    return ""


def gpu_vram_gb():
    """(gb, how). gb is None when we genuinely could not look.

    None and 0 are different answers and must not be collapsed: 0 means "no
    usable GPU, use the CPU tier", None means "we could not tell", and telling
    an operator they have no GPU when the query failed sends them to fix the
    wrong thing.
    """
    exe = shutil.which("nvidia-smi")
    if not exe:
        return 0.0, "nvidia-smi not on PATH"
    try:
        p = subprocess.run(
            [exe, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"nvidia-smi failed: {e}"
    if p.returncode != 0:
        return None, f"nvidia-smi exit {p.returncode}"
    best = 0.0
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            best = max(best, float(line) / 1024.0)
        except ValueError:
            return None, f"unparsable nvidia-smi output: {line[:40]!r}"
    return best, "nvidia-smi"


def choose(endpoint: str, vram_gb, headroom: float = 0.85) -> dict:
    """PURE. The tier decision, given what was detected.

    `headroom` exists because total VRAM is not usable VRAM -- a card reporting
    16 GB with a desktop session on it will not hold a 13 GB model, and
    discovering that as an OOM mid-run is worse than starting on the CPU.
    """
    if endpoint:
        t = dict(TIERS[0])
        t["endpoint"] = endpoint
        t["chosen_because"] = "an endpoint was declared, which wins over detection"
        return t
    gpu = dict(TIERS[1])
    if vram_gb is None:
        cpu = dict(TIERS[2])
        cpu["chosen_because"] = (
            "GPU state could not be read, and UNKNOWN is not ABSENT -- the CPU "
            "tier runs either way, so it is the safe direction")
        cpu["uncertain"] = True
        return cpu
    if vram_gb * headroom >= gpu["vram_gb"]:
        gpu["chosen_because"] = (
            f"{vram_gb:.1f} GB detected, {vram_gb * headroom:.1f} GB usable at "
            f"{int(headroom * 100)}% headroom, >= {gpu['vram_gb']} GB needed")
        return gpu
    cpu = dict(TIERS[2])
    cpu["chosen_because"] = (
        f"{vram_gb:.1f} GB detected; {gpu['vram_gb']} GB needed for "
        f"{gpu['model']} with headroom" if vram_gb
        else "no usable GPU detected")
    return cpu


def plan() -> dict:
    """What this host will run, and why. Writes nothing."""
    endpoint = declared_endpoint()
    vram, how = (None, "not queried") if endpoint else gpu_vram_gb()
    tier = choose(endpoint, vram)
    return {
        "tier": tier["id"],
        "backend": tier.get("backend", "declared endpoint"),
        "model": tier.get("model", tier.get("endpoint", "")),
        "why": tier["why"],
        "chosen_because": tier["chosen_because"],
        "detected": {"vram_gb": vram, "via": how,
                     "declared_endpoint": bool(endpoint)},
        "uncertain": tier.get("uncertain", False),
        "never": ("no hosted API is used as a fallback -- if nothing local "
                  "loads and no endpoint is declared, this stops and says so"),
    }


def self_test() -> int:
    fails = []

    def arm(n, c):
        if not c:
            fails.append(n)

    arm("a declared endpoint beats detection",
        choose("http://x", 80.0)["id"] == "byo")
    arm("a big GPU takes the 27B tier",
        choose("", 32.0)["id"] == "gpu-bonsai-27b")
    arm("a small GPU falls to CPU, not to a hosted API",
        choose("", 6.0)["id"] == "cpu-bonsai-4b")
    arm("no GPU falls to CPU", choose("", 0.0)["id"] == "cpu-bonsai-4b")
    # 16 GB * 0.85 = 13.6 >= 13 -> GPU. 15 GB * 0.85 = 12.75 -> CPU. Headroom is
    # the difference between running and an OOM discovered mid-run.
    arm("headroom is applied, not ignored",
        choose("", 16.0)["id"] == "gpu-bonsai-27b"
        and choose("", 15.0)["id"] == "cpu-bonsai-4b")
    arm("UNKNOWN vram is not treated as absent",
        choose("", None)["uncertain"] is True)
    arm("an unknown host still gets a working tier",
        choose("", None)["id"] == "cpu-bonsai-4b")
    arm("every tier says why it exists",
        all(t.get("why", "").strip() for t in TIERS))

    for n in fails:
        print("  SELF-TEST FAIL: " + n)
    print("onboard self-test: %d/8 arms passed" % (8 - len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    print(json.dumps(plan(), indent=2))
