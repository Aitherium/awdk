"""Pool role planner: what one machine should be in a split-model pool.

This is the first piece of `adk grid join`: one command that joins a node to a
pooled model such as DeepSeek-V4.1-Flash. It covers step (c)/(d) of that flow.
It reads the node's MEASURED memory, cores, GPUs and the round-trip time to the
coordinator, then emits:

* the pool role(s) this node can hold: ``rpc-vram`` (a GPU ggml-rpc backend),
  ``rpc-cpu`` (a DDR5 ggml-rpc backend), ``kvholder`` (lends memory for old KV
  pages), ``local`` (this node IS the coordinator: its GPU is the coordinator
  llama-server's own front ``--device CUDA0``, never an rpc-server), or nothing;
* how many pool blocks each backend can hold;
* the exact ``rpc-server`` / ``adk kvholder serve`` command for each role;
* per-node tuning advice (threads, governor, hugepages, NUMA, NIC offloads).
  It is advisory only and never applied.

It plans and never runs anything, because a planner that starts processes
cannot be dry-run. Admission (an awnboard invite, ``adk.pool_admission``) is
checked before the probe runs. The parts that act come later in the flow:
registration (pool_registry), and adoption (pool_join_driver).

The rules it applies are the fleet's measured ones, not new ones:

* **Bind guard.** ``rpc-server`` is unauthenticated. A non-private bind gets no
  rpc role (``kimi_coordinator.assert_rpc_bind``).
* **Latency gate.** A node is ``REPLICA_ONLY`` when its median RTT to the
  coordinator is above 10 ms, or cannot be measured (AitherZero
  3240_Measure-NodeCapability, measured 2026-08-01). A REPLICA_ONLY node never
  holds layers. It can still be a kvholder.
* **Block math.** A block is one transformer layer, held whole. Its size comes
  from the model's inference profile (``--profile FILE``: the ``pool:`` section
  and the quant's ``pool_block_gib`` / ``pool_blocks`` / ``pool_other_gib``,
  read from the GGUF tensor table). The built-in specs below are copies of those
  profiles. Per backend: 0.6 GiB rpc-server overhead, and CPU RSS of 2.75x the
  assigned blocks (measured 2026-10-01: a CPU worker given 5 blocks reached
  26.9 GB RSS).
* **Coordinator.** The coordinator plans ``local`` with the placement planner's
  own budget: available memory minus the profile's coordinator reserve, minus
  the non-block tensors it always holds.

Usage::

    python -m adk.pool_role --self-test
    python -m adk.pool_role --model deepseek-v4.1-flash-1017 --bind 192.168.1.50 \
        --coordinator 192.168.1.121:8001 --json
    adk grid join --model deepseek-v4.1-flash-1017 --bind 100.64.0.7 --coordinator ...

Admission comes first: ``adk grid join`` refuses to probe or plan until the node
presents a per-person awnboard invite for this pool (see ``adk.pool_admission``).

Exit codes: 0 means a plan was produced (it may be "no role"). 1 means the
invite was refused (missing, revoked, expired, forwarded) or the self-test
failed. 2 means it could not judge, for example an unknown model, no measurable
RAM, or no ledger to check revocation against.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

from adk.kimi_coordinator import validate_rpc_bind

GIB = 2**30

#: AitherZero 3240_Measure-NodeCapability -MaxSplitRttMs default (measured LAN
#: 2.8-4.3 ms median, nearest WAN 14.8 ms).
MAX_SPLIT_RTT_MS = 10.0

#: Ports in launch-pool.sh: the 5090 VRAM backend is 50054 and the DDR5 backend is 50055.
RPC_VRAM_PORT = 50054
RPC_CPU_PORT = 50055

#: A kvholder below this much spare RAM is not worth the join round-trip.
KVHOLDER_MIN_GIB = 2.0

#: Roles the coordinator's registry records as pool backends. ``local`` is the
#: coordinator itself and ``kvholder`` joins through its own approval flow.
REGISTERABLE_ROLES = ("rpc-vram", "rpc-cpu")


@dataclass(frozen=True)
class PoolSpec:
    """Block math for one pooled model. Data only, no node names."""

    model: str
    block_gib: float
    total_blocks: int
    rpc_overhead_gib: float
    rpc_cpu_rss_factor: float
    measured: bool
    source: str
    #: Non-block tensors (token_embd, output, globals) the coordinator always holds.
    other_gib: float = 0.0
    #: Memory the coordinator keeps back (OS, KV, compute buffers). None = the
    #: generic RAM reserve (8 GiB or 10%).
    coordinator_reserve_gib: Optional[float] = None
    quant: str = ""

    @classmethod
    def from_profile(cls, profile: dict[str, Any], quant: str = "",
                     source: str = "profile") -> "PoolSpec":
        """Block math from an inference profile mapping (its ``pool:`` section plus
        the chosen quant's ``pool_*`` fields). Raises ValueError on anything missing:
        a profile without measured block sizes cannot be planned against."""
        if not isinstance(profile, dict):
            raise ValueError(f"{source}: not a mapping")
        meta = profile.get("profile") or {}
        pool = profile.get("pool")
        model = meta.get("id") or ""
        if not model or not isinstance(pool, dict):
            raise ValueError(f"{source}: needs profile.id and a pool: section")
        want = quant or pool.get("default_quant") or ""
        quants = {q.get("id"): q for q in profile.get("quants") or [] if isinstance(q, dict)}
        q = quants.get(want)
        if q is None:
            raise ValueError(f"{source}: quant {want!r} not in {sorted(quants)}")
        try:
            block = float(q["pool_block_gib"])
            blocks = int(q["pool_blocks"])
            other = float(q.get("pool_other_gib", 0.0))
            overhead = float(pool["rpc_overhead_gib"])
            rss = float(pool["rpc_cpu_rss_factor"])
            reserve = pool.get("coordinator_reserve_gib")
            reserve_f = None if reserve is None else float(reserve)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{source}: quant {want!r} block math incomplete: {exc}") from exc
        if block <= 0 or blocks <= 0 or other < 0 or overhead < 0 or rss <= 0:
            raise ValueError(f"{source}: quant {want!r} block math out of range")
        return cls(model=model, block_gib=block, total_blocks=blocks,
                   rpc_overhead_gib=overhead, rpc_cpu_rss_factor=rss,
                   measured=bool(q.get("built", True)),
                   source=f"inference profile {model} quant {want} ({source})",
                   other_gib=other, coordinator_reserve_gib=reserve_f, quant=want)


def load_profile_spec(path: str, quant: str = "") -> PoolSpec:
    """Read a profile file (YAML or JSON) and return its block math."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        data = json.loads(text)
    except ValueError:
        import yaml

        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"{path}: neither JSON nor YAML: {exc}") from exc
    return PoolSpec.from_profile(data, quant, source=os.path.basename(path))


POOL_SPECS: dict[str, PoolSpec] = {
    "deepseek-v4-flash": PoolSpec(
        model="deepseek-v4-flash",
        block_gib=1.969,
        total_blocks=43,
        rpc_overhead_gib=0.6,
        rpc_cpu_rss_factor=2.75,
        measured=True,
        source="platform pool posture `pool:` block (V4-Flash-0731 UD-IQ2_M)",
    ),
    # A copy of the deepseek-v4.1-flash-1017 inference profile's default quant
    # (UD-IQ2ish, read from the built GGUF's tensor table 2026-10-19). A test pins
    # this entry to the profile; `--profile FILE [--quant ID]` reads it directly.
    "deepseek-v4.1-flash-1017": PoolSpec(
        model="deepseek-v4.1-flash-1017",
        block_gib=3.6005,
        total_blocks=40,
        rpc_overhead_gib=0.6,
        rpc_cpu_rss_factor=2.75,
        measured=True,
        source="inference profile deepseek-v4.1-flash-1017 quant UD-IQ2ish (built-in copy)",
        other_gib=1.31,
        coordinator_reserve_gib=16.0,
        quant="UD-IQ2ish",
    ),
}
MODEL_ALIASES = {
    "deepseek-v4.1": "deepseek-v4.1-flash-1017",
    "v41": "deepseek-v4.1-flash-1017",
    "deepseek-v4": "deepseek-v4-flash",
}


@dataclass
class GpuCaps:
    index: int
    name: str
    free_gib: float
    total_gib: float
    #: True when the GPU shares system RAM (DGX Spark GB10). nvidia-smi reports its
    #: memory as "[N/A]"; the planner budgets it from RAM and never also plans a CPU
    #: backend on the same bytes.
    unified: bool = False


@dataclass
class NodeCaps:
    """What the probe measured. Every field comes from the machine, not from config."""

    os: str
    ram_total_gib: float
    ram_available_gib: float
    physical_cores: int
    logical_cores: int
    numa_nodes: int = 1
    gpus: list[GpuCaps] = field(default_factory=list)
    rtt_ms: Optional[float] = None  # None = not measured / unmeasurable


@dataclass
class Backend:
    role: str  # rpc-vram | rpc-cpu | kvholder | local
    blocks: int
    budget_gib: float
    command: list[str]
    note: str = ""


@dataclass
class RolePlan:
    model: str
    spec_measured: bool
    spec_source: str
    admission: str  # SPLIT_ELIGIBLE | REPLICA_ONLY | REFUSED | COORDINATOR
    reasons: list[str]
    backends: list[Backend]
    tuning: list[dict[str, str]]
    caps: NodeCaps

    @property
    def roles(self) -> list[str]:
        return [b.role for b in self.backends]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["roles"] = self.roles
        d["blocks_offered"] = sum(b.blocks for b in self.backends if b.role.startswith("rpc"))
        return d


def _debug(msg: str) -> None:
    """Probe fallbacks are expected; say so on stderr only when asked."""
    if os.environ.get("ADK_POOL_ROLE_DEBUG"):
        print(f"pool_role: {msg}", file=sys.stderr)


# ── probe ───────────────────────────────────────────────────────────────────


def _probe_ram() -> tuple[float, float]:
    try:
        import psutil

        vm = psutil.virtual_memory()
        return vm.total / GIB, vm.available / GIB
    except Exception as exc:  # noqa: BLE001 - psutil missing or sandboxed
        _debug(f"psutil unavailable ({exc!r}); reading /proc/meminfo")
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) * 1024
        return info["MemTotal"] / GIB, info.get("MemAvailable", 0) / GIB
    except Exception:  # noqa: BLE001
        return 0.0, 0.0


def _probe_cores() -> tuple[int, int]:
    import os

    logical = os.cpu_count() or 1
    try:
        import psutil

        physical = psutil.cpu_count(logical=False) or logical
    except Exception:  # noqa: BLE001
        physical = logical
    return physical, logical


def _probe_numa() -> int:
    try:
        import glob

        n = len(glob.glob("/sys/devices/system/node/node[0-9]*"))
        return max(1, n)
    except Exception:  # noqa: BLE001
        return 1


def _probe_gpus(run: Callable[..., Any] = subprocess.run) -> list[GpuCaps]:
    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = run(
            ["nvidia-smi", "--query-gpu=index,name,memory.free,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if getattr(out, "returncode", 1) != 0:
        return []
    return parse_nvidia_smi(out.stdout)


def parse_nvidia_smi(text: str) -> list[GpuCaps]:
    gpus = []
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            index = int(parts[0])
        except ValueError:
            continue
        if parts[2] == "[N/A]" and parts[3] == "[N/A]":
            # Unified memory (GB10): the GPU is real, its memory is system RAM.
            # Dropping it here made the DGX plan as a GPU-less CPU node.
            gpus.append(GpuCaps(index, parts[1], 0.0, 0.0, unified=True))
            continue
        try:
            gpus.append(GpuCaps(index, parts[1],
                                float(parts[2]) / 1024, float(parts[3]) / 1024))
        except ValueError:
            continue
    return gpus


def measure_rtt_ms(host: str, port: int, samples: int = 9,
                   timeout_s: float = 2.0) -> Optional[float]:
    """Median TCP-connect RTT, using 9 samples like 3240. Returns None when any sample fails."""
    rtts = []
    for _ in range(samples):
        start = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout_s):
                rtts.append((time.perf_counter() - start) * 1000)
        except OSError:
            return None
    rtts.sort()
    return rtts[len(rtts) // 2]


def probe_node(coordinator: Optional[str] = None) -> NodeCaps:
    total, avail = _probe_ram()
    physical, logical = _probe_cores()
    rtt = None
    if coordinator:
        host, _, port = coordinator.rpartition(":")
        if host and port.isdigit():
            rtt = measure_rtt_ms(host, int(port))
    return NodeCaps(
        os=platform.system().lower(),
        ram_total_gib=round(total, 2),
        ram_available_gib=round(avail, 2),
        physical_cores=physical,
        logical_cores=logical,
        numa_nodes=_probe_numa() if platform.system() == "Linux" else 1,
        gpus=_probe_gpus(),
        rtt_ms=None if rtt is None else round(rtt, 3),
    )


# ── planner (pure) ──────────────────────────────────────────────────────────


def resolve_model(name: str) -> Optional[PoolSpec]:
    return POOL_SPECS.get(MODEL_ALIASES.get(name, name))


def is_coordinator_endpoint(coordinator: str, bind: str = "") -> bool:
    """True when ``coordinator`` names THIS node: a loopback host, or the bind address.

    The coordinator's own GPU is the llama-server's local device. Planning it as an
    rpc-server would put the coordinator's weights one network hop from itself.
    """
    host = coordinator.rpartition(":")[0] if ":" in coordinator else coordinator
    host = host.strip("[]").lower()
    if not host:
        return False
    if host == "localhost":
        return True
    if bind and host == bind.strip("[]").lower():
        return True
    try:
        import ipaddress

        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def coordinator_blocks(budget_gib: float, spec: PoolSpec) -> int:
    """Whole blocks the coordinator holds after the non-block tensors it always keeps.

    The same charge the pool placement planner makes against the coordinator:
    ``other`` first, then whole layers of ``block_gib``.
    """
    room = budget_gib - spec.other_gib
    if room <= 0:
        return 0
    return min(spec.total_blocks, max(0, math.floor(room / spec.block_gib)))


def _plan_coordinator(caps: NodeCaps, spec: PoolSpec, reasons: list[str],
                      ram_usable: float, vram_reserve_gib: float,
                      ram_free_gib: float, ram_reserve_gib: float) -> list[Backend]:
    """The coordinator's own memory, as the pool placement planner sees it.

    Its GPU is the front ``--device CUDA0`` (``-ngl 99``) of the coordinator
    llama-server; its CPU keeps the mmap'd tables. Neither is an rpc-server, and
    there is no rpc-cpu on the coordinator: the remote tiers are other nodes.
    A unified-memory GPU (GB10) is budgeted as the placement planner budgets it:
    ``--dgx-free-mb`` is the MEASURED available memory and ``--dgx-reserve-mb``
    the reserve; the planner subtracts the reserve itself, so it is passed, never
    pre-subtracted.
    """
    out: list[Backend] = []
    unified_planned = False
    for g in caps.gpus:
        if g.unified:
            if unified_planned:
                continue
            budget = ram_usable
            unified_planned = True
            planner = (f"--dgx-free-mb {int(max(0.0, ram_free_gib) * 1024)} "
                       f"--dgx-reserve-mb {int(ram_reserve_gib * 1024)}")
        else:
            budget = g.free_gib - vram_reserve_gib
            planner = (f"--dgx-free-mb {int(max(0.0, g.free_gib) * 1024)} "
                       f"--dgx-reserve-mb {int(vram_reserve_gib * 1024)}")
        out.append(Backend(
            role="local", blocks=coordinator_blocks(budget, spec),
            budget_gib=round(max(0.0, budget), 2), command=[],
            note=(f"{g.name}; coordinator llama-server --device CUDA{g.index} -ngl 99, "
                  f"placement planner {planner}; holds {spec.other_gib} GiB non-block "
                  "tensors; no rpc-server"),
        ))
    if not out:
        budget = max(0.0, ram_usable)
        out.append(Backend(
            role="local", blocks=coordinator_blocks(budget, spec),
            budget_gib=round(budget, 2), command=[],
            note="no GPU; coordinator llama-server holds its share in RAM; no rpc-server"))
    reasons.append("this node is the coordinator: its memory is the llama-server's own "
                   "device, never an rpc backend")
    return out


def _ram_reserve_gib(caps: NodeCaps) -> float:
    """RAM left for the OS and everything else: 8 GiB, or 10% of total if that is larger."""
    return max(8.0, 0.10 * caps.ram_total_gib)


def plan_roles(
    caps: NodeCaps,
    spec: PoolSpec,
    bind: str,
    *,
    rpc_server: str = "rpc-server",
    want: str = "auto",
    ram_cap_gib: Optional[float] = None,
    vram_reserve_gib: float = 1.0,
    max_split_rtt_ms: float = MAX_SPLIT_RTT_MS,
    is_coordinator: bool = False,
) -> RolePlan:
    """Decide the roles from measured caps. Pure function: no I/O.

    ``is_coordinator`` means this node runs the coordinator llama-server: its
    GPU is planned ``local`` (the server's own CUDA0), matching the pool
    placement planner, and it gets no rpc role.

    ``want`` is auto, rpc-vram, rpc-cpu or kvholder. Anything other than auto
    narrows the plan to that one role, and never widens it past the gates.
    ``ram_cap_gib`` is a cgroup or slice MemoryMax the CPU worker runs under.
    The fleet's aither.slice is the reason this exists.
    """
    reasons: list[str] = []
    backends: list[Backend] = []

    if is_coordinator:
        reserve = (spec.coordinator_reserve_gib if spec.coordinator_reserve_gib is not None
                   else _ram_reserve_gib(caps))
        free = caps.ram_available_gib
        if ram_cap_gib is not None:
            free = min(free, ram_cap_gib)
        backends = _plan_coordinator(caps, spec, reasons, free - reserve, vram_reserve_gib,
                                     free, reserve)
        return RolePlan(
            model=spec.model, spec_measured=spec.measured, spec_source=spec.source,
            admission="COORDINATOR", reasons=reasons, backends=backends,
            tuning=tuning_advice(caps, ["rpc-vram"] if caps.gpus else []), caps=caps,
        )

    bind_ok = validate_rpc_bind(bind)
    if not bind_ok:
        reasons.append(f"bind {bind!r} is not private/overlay; rpc-server is "
                       "unauthenticated, so no rpc role")
    if caps.rtt_ms is None:
        split_ok = False
        reasons.append("coordinator RTT unmeasurable -> REPLICA_ONLY (never treat silence as fast)")
    elif caps.rtt_ms > max_split_rtt_ms:
        split_ok = False
        reasons.append(f"RTT {caps.rtt_ms} ms > {max_split_rtt_ms} ms -> REPLICA_ONLY")
    else:
        split_ok = True

    admission = ("REFUSED" if not bind_ok else
                 "SPLIT_ELIGIBLE" if split_ok else "REPLICA_ONLY")
    rpc_allowed = bind_ok and split_ok

    threads = max(1, caps.physical_cores - 2)
    numa_prefix = (["numactl", "--interleave=all"] if caps.numa_nodes > 1 else [])

    ram_usable = caps.ram_available_gib - _ram_reserve_gib(caps)
    if ram_cap_gib is not None:
        ram_usable = min(ram_usable, ram_cap_gib)
    unified_planned = False

    if rpc_allowed and want in ("auto", "rpc-vram"):
        for g in caps.gpus:
            if g.unified:
                if unified_planned:
                    continue  # one RAM pool; a second unified GPU cannot claim it again
                budget = ram_usable - spec.rpc_overhead_gib
            else:
                budget = g.free_gib - spec.rpc_overhead_gib - vram_reserve_gib
            blocks = max(0, math.floor(budget / spec.block_gib)) if budget > 0 else 0
            if blocks > spec.total_blocks:
                reasons.append(f"GPU{g.index} {g.name}: room for {blocks} blocks, "
                               f"model has {spec.total_blocks}; offering {spec.total_blocks}")
                blocks = spec.total_blocks
            if blocks == 0:
                have = (f"{ram_usable:.1f} GiB usable RAM (unified)" if g.unified
                        else f"{g.free_gib:.1f} GiB free")
                reasons.append(f"GPU{g.index} {g.name}: {have} < one block + overhead")
                continue
            port = RPC_VRAM_PORT + 2 * g.index
            if g.unified:
                unified_planned = True
            backends.append(Backend(
                role="rpc-vram", blocks=blocks, budget_gib=round(budget, 2),
                command=[rpc_server, "-H", bind, "-p", str(port),
                         "-d", f"CUDA{g.index}", "-c"],
                note=(f"{g.name}; unified memory, budget is system RAM"
                      if g.unified else
                      f"{g.name}; hot experts land here via the coordinator's -ot"),
            ))

    if unified_planned and want == "auto":
        reasons.append("unified-memory GPU holds the RAM budget; no rpc-cpu on the same bytes")
    if rpc_allowed and want in ("auto", "rpc-cpu") and not (unified_planned and want == "auto"):
        usable = ram_usable
        per_block = spec.block_gib * spec.rpc_cpu_rss_factor
        blocks = max(0, math.floor(usable / per_block)) if usable > 0 else 0
        blocks = min(blocks, spec.total_blocks)
        if blocks:
            backends.append(Backend(
                role="rpc-cpu", blocks=blocks, budget_gib=round(usable, 2),
                command=numa_prefix + [rpc_server, "-H", bind, "-p", str(RPC_CPU_PORT),
                                       "-d", "CPU", "-t", str(threads), "-c"],
                note=(f"{blocks} x {spec.block_gib} GiB at {spec.rpc_cpu_rss_factor}x RSS "
                      "= expert-cache misses tier"),
            ))
        else:
            reasons.append(f"RAM: {usable:.1f} GiB usable < one CPU block "
                           f"({per_block:.1f} GiB RSS)")

    if want in ("auto", "kvholder") and (not backends or want == "kvholder"):
        spare = caps.ram_available_gib - min(4.0, 0.25 * caps.ram_total_gib)
        if spare >= KVHOLDER_MIN_GIB:
            backends.append(Backend(
                role="kvholder", blocks=0, budget_gib=round(spare, 2),
                command=["adk", "kvholder", "serve", "--mesh", "--store", "tq4"],
                note="owner approves the join code (awsh_kvholder_join)",
            ))
        else:
            reasons.append(f"spare RAM {spare:.1f} GiB < kvholder minimum {KVHOLDER_MIN_GIB}")

    return RolePlan(
        model=spec.model, spec_measured=spec.measured, spec_source=spec.source,
        admission=admission, reasons=reasons, backends=backends,
        tuning=tuning_advice(caps, [b.role for b in backends]), caps=caps,
    )


def tuning_advice(caps: NodeCaps, roles: list[str]) -> list[dict[str, str]]:
    """Per-node aw*stack optimizations. Advisory only, never applied.

    There are no kernel modules, nothing is persistent, and nothing here needs
    a restart.
    """
    out: list[dict[str, str]] = []
    rpc = [r for r in roles if r.startswith("rpc")]
    if not rpc:
        return out
    if caps.os == "linux":
        out.append({"what": "CPU governor -> performance",
                    "cmd": "cpupower frequency-set -g performance",
                    "why": "decode is latency-bound; ondemand ramps add jitter per token"})
        if "rpc-cpu" in rpc:
            out.append({"what": "transparent hugepages for the CPU worker",
                        "cmd": "echo madvise > /sys/kernel/mm/transparent_hugepage/enabled",
                        "why": "multi-GiB expert buffers; fewer TLB misses on expert scans"})
        if caps.numa_nodes > 1:
            out.append({"what": "interleave across NUMA nodes",
                        "cmd": "numactl --interleave=all (already prefixed on rpc-cpu)",
                        "why": f"{caps.numa_nodes} NUMA nodes; "
                               "one-node allocation halves bandwidth"})
        out.append({"what": "NIC offloads + ring size",
                    "cmd": "ethtool -K <iface> gro on gso on tso on && "
                           "ethtool -G <iface> rx 4096 tx 4096",
                    "why": "ggml-rpc ships activations per token; coalescing cuts per-packet cost"})
        if "rpc-vram" in rpc:
            out.append({"what": "GPU persistence mode",
                        "cmd": "nvidia-smi -pm 1",
                        "why": "avoids driver re-init stalls between pool relaunches"})
    elif caps.os == "windows":
        out.append({"what": "power plan -> High performance",
                    "cmd": "powercfg /setactive SCHEME_MIN",
                    "why": "Balanced parks cores under the rpc-cpu worker"})
    if "rpc-vram" in rpc:
        out.append({"what": "keep pinned host memory on",
                    "cmd": "leave GGML_CUDA_NO_PINNED unset",
                    "why": "pinned staging is the fast path for RPC tensor copies"})
    return out


# ── output ──────────────────────────────────────────────────────────────────


def render_text(plan: RolePlan) -> str:
    c = plan.caps
    lines = [
        f"  model       {plan.model}"
        + ("" if plan.spec_measured else "   [PROVISIONAL block math]"),
        f"  node        {c.os}  RAM {c.ram_available_gib:.1f}/{c.ram_total_gib:.1f} GiB  "
        f"cores {c.physical_cores}p/{c.logical_cores}l  NUMA {c.numa_nodes}  "
        f"GPUs {len(c.gpus)}  RTT {c.rtt_ms if c.rtt_ms is not None else 'n/a'} ms",
        f"  admission   {plan.admission}",
    ]
    for r in plan.reasons:
        lines.append(f"    - {r}")
    if not plan.backends:
        lines.append("  roles       none")
    for b in plan.backends:
        lines.append(f"  role        {b.role:9s} blocks={b.blocks:<3d} budget={b.budget_gib} GiB")
        if b.command:
            lines.append(f"              $ {' '.join(b.command)}")
        if b.note:
            lines.append(f"              ({b.note})")
    if plan.tuning:
        lines.append("  tuning (advisory, not applied)")
        for t in plan.tuning:
            lines.append(f"    - {t['what']}: {t['cmd']}")
    lines.append("  next        admission (awnboard invite) -> `--register POOL_ID` records "
                 "the rpc roles -> the coordinator adopts; this planner starts nothing")
    return "\n".join(lines)


# ── self-test ───────────────────────────────────────────────────────────────


def self_test() -> int:
    """Prove the gates can still fail. Returns 0 on pass, 1 on any failure."""
    spec = POOL_SPECS["deepseek-v4.1-flash-1017"]
    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    # 5090 host as measured: about 30 GiB free VRAM, 64 GiB DDR5, LAN RTT 3 ms.
    host = NodeCaps("linux", 64.0, 56.0, 16, 32, 1,
                    [GpuCaps(0, "RTX 5090", 30.0, 32.6)], rtt_ms=3.0)
    p = plan_roles(host, spec, "192.168.1.50")
    check(p.admission == "SPLIT_ELIGIBLE", "5090 host must be SPLIT_ELIGIBLE")
    check(p.roles == ["rpc-vram", "rpc-cpu"], f"5090 host roles {p.roles}")
    vram = p.backends[0]
    check(vram.blocks == math.floor((30.0 - 0.6 - 1.0) / spec.block_gib), "vram block math")
    check(vram.command[:7] == ["rpc-server", "-H", "192.168.1.50", "-p", "50054", "-d", "CUDA0"],
          f"vram command {vram.command}")
    cpu = p.backends[1]
    check(cpu.blocks == math.floor((56.0 - 8.0) / (spec.block_gib * 2.75)),
          "cpu block math uses 2.75x RSS")
    check("-t" in cpu.command and cpu.command[cpu.command.index("-t") + 1] == "14",
          "threads = phys-2")

    # A slice cap bounds the CPU worker: 44 GiB fleet slice, 13 blocks would OOM.
    capped = plan_roles(host, spec, "192.168.1.50", ram_cap_gib=12.0)
    want_capped = math.floor(12.0 / (spec.block_gib * 2.75))
    check(capped.backends[1].blocks == want_capped,
          f"ram cap -> {want_capped} blocks, got {capped.backends[1].blocks}")

    # A public bind is never given an rpc role, even on big hardware.
    pub = plan_roles(host, spec, "1.1.1.1")
    check(pub.admission == "REFUSED", "public bind must be REFUSED")
    check(all(not r.startswith("rpc") for r in pub.roles), "public bind got an rpc role")
    check(not validate_rpc_bind("0.0.0.0"), "0.0.0.0 must not validate")

    # WAN RTT, or RTT that cannot be measured, means REPLICA_ONLY: kvholder only.
    for rtt in (25.0, None):
        wan = NodeCaps("linux", 64.0, 56.0, 16, 32, 1,
                       [GpuCaps(0, "RTX 5090", 30.0, 32.6)], rtt_ms=rtt)
        w = plan_roles(wan, spec, "100.64.0.7")
        check(w.admission == "REPLICA_ONLY", f"rtt={rtt} must be REPLICA_ONLY")
        check(w.roles == ["kvholder"], f"rtt={rtt} roles {w.roles}")

    # A phone has no GPU and little RAM: kvholder, or nothing at all.
    phone = NodeCaps("linux", 8.0, 5.0, 8, 8, 1, [], rtt_ms=6.0)
    ph = plan_roles(phone, spec, "100.64.0.9")
    check(ph.roles == ["kvholder"], f"phone roles {ph.roles}")
    tiny = NodeCaps("linux", 4.0, 1.0, 4, 4, 1, [], rtt_ms=6.0)
    check(plan_roles(tiny, spec, "100.64.0.9").roles == [], "tiny device must get no role")

    # A busy GPU (another model holds it) gets no VRAM block.
    busy = NodeCaps("windows", 64.0, 56.0, 16, 32, 1,
                    [GpuCaps(0, "RTX 5090", 2.5, 32.6)], rtt_ms=3.0)
    b = plan_roles(busy, spec, "192.168.1.50")
    check("rpc-vram" not in b.roles, "busy GPU must not get an rpc-vram block")

    # NUMA box: the CPU worker is interleaved.
    numa = NodeCaps("linux", 512.0, 480.0, 64, 128, 2, [], rtt_ms=1.0)
    n = plan_roles(numa, spec, "10.0.0.5")
    check(n.backends[0].command[:2] == ["numactl", "--interleave=all"], "numa prefix missing")

    # DGX Spark GB10 as measured 2026-10-19: nvidia-smi says "[N/A]" for memory,
    # 112.4 of 121.7 GiB RAM available. The GPU must survive the parse, and its RAM
    # budget must not be planned twice (rpc-vram AND rpc-cpu on the same bytes).
    gb10 = parse_nvidia_smi("0, NVIDIA GB10, [N/A], [N/A]")
    check(len(gb10) == 1 and gb10[0].unified, f"GB10 parse {gb10}")
    spark = NodeCaps("linux", 121.7, 112.4, 20, 20, 1, gb10, rtt_ms=0.5)
    sp = plan_roles(spark, spec, "100.64.0.38")
    check(sp.roles == ["rpc-vram"], f"GB10 roles {sp.roles}")
    check(bool(sp.roles) and sp.backends[0].blocks
          == min(spec.total_blocks,
                 math.floor((112.4 - _ram_reserve_gib(spark) - 0.6) / spec.block_gib)),
          "GB10 block math = RAM budget at 1x, capped at the model's blocks")
    check(all(bk.blocks <= spec.total_blocks for bk in sp.backends),
          "a backend was offered more blocks than the model has")

    # The coordinator's own GB10 is the llama-server's CUDA0, never an rpc-server
    # (the pool placement planner puts it at the front of --device). Planned as an
    # rpc backend, the coordinator would reach its own weights over the network.
    coord = plan_roles(spark, spec, "100.64.0.38", is_coordinator=True)
    check(coord.admission == "COORDINATOR", f"coordinator admission {coord.admission}")
    check(coord.roles == ["local"], f"coordinator GB10 roles {coord.roles}")
    check(all(not bk.command for bk in coord.backends), "coordinator got an rpc-server command")
    check(not any(r in REGISTERABLE_ROLES for r in coord.roles),
          "coordinator would register itself as a pool backend")
    # ...budgeted as the placement planner budgets the coordinator: MemAvailable -
    # reserve, the non-block tensors first, then whole blocks; the planner gets the
    # RAW free figure.
    want_local = math.floor((112.4 - 16.0 - spec.other_gib) / spec.block_gib)
    check(bool(coord.backends) and coord.backends[0].blocks == want_local,
          f"coordinator blocks {coord.backends[0].blocks if coord.backends else None} "
          f"!= placement-planner budget {want_local}")
    check(bool(coord.backends) and f"--dgx-free-mb {int(112.4 * 1024)} "
          "--dgx-reserve-mb 16384" in coord.backends[0].note,
          "coordinator note must pass the measured free memory and the reserve")

    from dataclasses import replace

    tight = replace(spec, block_gib=2.0, other_gib=1.5, total_blocks=40)
    check(coordinator_blocks(10.0, tight) == 4,
          "coordinator must charge the non-block tensors before whole blocks")

    # Block math comes from a profile, and a profile without it is refused.
    prof = {"profile": {"id": "m"}, "pool": {"default_quant": "q", "rpc_overhead_gib": 0.5,
            "rpc_cpu_rss_factor": 2.0, "coordinator_reserve_gib": 4.0},
            "quants": [{"id": "q", "pool_block_gib": 2.5, "pool_blocks": 7,
                        "pool_other_gib": 0.25, "built": True}]}
    ps = PoolSpec.from_profile(prof)
    check((ps.block_gib, ps.total_blocks, ps.other_gib, ps.coordinator_reserve_gib, ps.quant)
          == (2.5, 7, 0.25, 4.0, "q"), f"from_profile {ps}")
    try:
        PoolSpec.from_profile({**prof, "quants": [{"id": "q", "pool_blocks": 7}]})
        check(False, "a quant without pool_block_gib must be refused")
    except ValueError:
        pass
    check(is_coordinator_endpoint("127.0.0.1:8001") and is_coordinator_endpoint("localhost:1")
          and is_coordinator_endpoint("100.64.0.38:8001", "100.64.0.38")
          and not is_coordinator_endpoint("100.64.0.38:8001", "100.64.0.7"),
          "coordinator endpoint detection")

    # `want` narrows the plan; it never bypasses a gate.
    only_cpu = plan_roles(host, spec, "192.168.1.50", want="rpc-cpu")
    check(only_cpu.roles == ["rpc-cpu"], f"want=rpc-cpu roles {only_cpu.roles}")
    check(plan_roles(host, spec, "8.8.8.8", want="rpc-vram").roles == [],
          "want cannot bypass bind gate")

    # Tuning is advice only and never names a kernel module.
    check(all("modprobe" not in t["cmd"] and "insmod" not in t["cmd"] for t in p.tuning),
          "tuning must not load kernel modules")

    check(parse_nvidia_smi("0, NVIDIA GeForce RTX 5090, 30720, 32607\n")[0].free_gib == 30.0,
          "nvidia-smi parse")
    check(resolve_model("v41") is spec and resolve_model("nope") is None, "model resolve")
    check(json.loads(json.dumps(p.to_dict()))["blocks_offered"] == vram.blocks + cpu.blocks,
          "json roundtrip")

    for f in failures:
        print(f"FAIL {f}", file=sys.stderr)
    print(f"pool_role self-test: {'PASS' if not failures else 'FAIL'} "
          f"({len(failures)} failure(s))")
    return 1 if failures else 0


# ── CLI ─────────────────────────────────────────────────────────────────────


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--model", default="deepseek-v4.1-flash-1017",
                   help=f"pooled model ({', '.join(POOL_SPECS)})")
    p.add_argument("--role", default="auto",
                   choices=["auto", "rpc-vram", "rpc-cpu", "kvholder"])
    p.add_argument("--bind", default="", help="overlay/LAN address rpc-server binds (private only)")
    p.add_argument("--coordinator", default="",
                   help="host:port of the coordinator, for the RTT gate")
    p.add_argument("--ram-cap-gib", type=float, default=None,
                   help="cgroup/slice MemoryMax the CPU worker runs under")
    p.add_argument("--rpc-server", default="rpc-server", help="path to ggml rpc-server")
    p.add_argument("--json", action="store_true", help="machine-readable plan")
    p.add_argument("--profile", default="",
                   help="inference profile (YAML/JSON) to take the block math from "
                        "(default $ADK_POOL_PROFILE; else the built-in copy)")
    p.add_argument("--quant", default="",
                   help="quant id inside --profile (default: its pool.default_quant)")
    p.add_argument("--coordinator-local", action="store_true",
                   help="this node runs the coordinator llama-server (auto when "
                        "--coordinator is loopback or equals --bind)")
    p.add_argument("--register", metavar="POOL_ID", default="",
                   help="record the planned rpc roles in the coordinator's pool registry "
                        "(a record only: starts nothing)")
    p.add_argument("--registry-url", default="",
                   help="base URL of the pool registry route (default $ADK_POOL_REGISTRY_URL)")
    p.add_argument("--node-id", default="", help="mesh node id recorded with the backend")
    p.add_argument("--apply", action="store_true",
                   help="start the roles (NOT BUILT: the coordinator adopts registered roles)")
    from adk.pool_admission import add_arguments as _admission_args

    _admission_args(p)


def _default_admit(args: argparse.Namespace, model: str) -> Any:
    from adk.pool_admission import admit_from_args

    return admit_from_args(args, model)


def registration_payload(plan: RolePlan, node_id: str = "") -> dict[str, Any]:
    """What `--register` sends: the plan as measured, plus who measured it.

    The registry re-validates every field it records (admission, bind, role);
    the coordinator's adoption gate then re-measures reachability itself.
    """
    return {"plan": plan.to_dict(), "node_id": node_id or socket.gethostname()}


def _post_json(url: str, body: dict[str, Any], headers: dict[str, str],
               timeout: float = 15.0) -> tuple[int, str]:
    """One POST -> (status, text). Tests replace this."""
    import httpx

    from adk._tls import tls_verify

    with httpx.Client(timeout=timeout, verify=tls_verify()) as client:
        resp = client.post(url, json=body, headers=headers)
    return resp.status_code, resp.text


def register_plan(plan: RolePlan, pool_id: str, registry_url: str, *, node_id: str = "",
                  bearer: Optional[str] = None,
                  send: Optional[Callable[..., tuple[int, str]]] = None) -> int:
    """Record the plan's rpc roles in the pool registry. Returns an exit code.

    0 registered, 1 nothing to register (the plan holds no rpc role), 2 could not
    register (no URL, no credential, transport error, or the registry refused).
    Registration is a record: it starts no process and adopts nothing.
    """
    rpc = [b for b in plan.backends if b.role in REGISTERABLE_ROLES]
    if not rpc:
        print(f"nothing to register: admission {plan.admission}, roles "
              f"{plan.roles or ['none']} hold no rpc backend", file=sys.stderr)
        return 1
    if not registry_url:
        print("--register needs --registry-url or $ADK_POOL_REGISTRY_URL", file=sys.stderr)
        return 2
    if bearer is None:
        try:
            from adk.devices import resolve_bearer

            bearer = resolve_bearer()
        except Exception as exc:  # noqa: BLE001 - no credential is a refusal, not a crash
            print(f"could not resolve a credential: {exc}", file=sys.stderr)
            bearer = ""
    if not bearer:
        print("no credential: run `adk login` or set AITHER_NODE_TOKEN", file=sys.stderr)
        return 2
    from urllib.parse import quote

    base = registry_url.rstrip("/")
    quoted_id = quote(pool_id, safe="")
    url = f"{base}/pools/{quoted_id}/backends"
    try:
        status, text = (send or _post_json)(
            url, registration_payload(plan, node_id),
            {"Authorization": f"Bearer {bearer}"})
    except Exception as exc:  # noqa: BLE001 - network failure = could not judge
        print(f"registry unreachable at {url}: {exc}", file=sys.stderr)
        return 2
    if status != 200:
        print(f"registry refused ({status}): {text[:300]}", file=sys.stderr)
        return 2
    try:
        got = json.loads(text)
    except ValueError:
        print(f"registry answered 200 with no JSON: {text[:200]}", file=sys.stderr)
        return 2
    registered = got.get("registered") or []
    if not registered:
        print(f"registry recorded nothing: {got.get('refused') or got}", file=sys.stderr)
        return 2
    for hp in registered:
        print(f"  registered  {hp} in pool {pool_id} (status=registered; adoption is the "
              "coordinator's decision)")
    for r in got.get("refused") or []:
        print(f"  refused     {r}", file=sys.stderr)
    return 0


def run(args: argparse.Namespace, probe: Callable[[Optional[str]], NodeCaps] = probe_node,
        admit: Callable[[argparse.Namespace, str], Any] = _default_admit,
        send: Optional[Callable[..., tuple[int, str]]] = None) -> int:
    spec = resolve_model(args.model)
    if spec is None:
        print(f"unknown model {args.model!r}; known: {', '.join(POOL_SPECS)}", file=sys.stderr)
        return 2
    profile_path = getattr(args, "profile", "") or os.environ.get("ADK_POOL_PROFILE", "")
    if profile_path:
        try:
            prof_spec = load_profile_spec(profile_path, getattr(args, "quant", "") or "")
        except (OSError, ValueError) as exc:
            print(f"could not read block math from {profile_path}: {exc}", file=sys.stderr)
            return 2
        if prof_spec.model != spec.model:
            print(f"profile is for {prof_spec.model!r}, not {spec.model!r}", file=sys.stderr)
            return 2
        spec = prof_spec
    # Admission first: nothing is probed, planned or registered for a node that has
    # not presented a valid per-person invite for THIS pool (adk.pool_admission).
    adm = admit(args, spec.model)
    if not adm.admitted:
        print(adm.message(), file=sys.stderr)
        return adm.exit_code
    if getattr(args, "apply", False):
        print("--apply is not built yet: the coordinator adopts registered roles. This "
              "command plans, and with --register records; it starts nothing.", file=sys.stderr)
        return 2
    caps = probe(args.coordinator or None)
    if caps.ram_total_gib <= 0:
        print("could not measure RAM; refusing to plan on an unmeasured node", file=sys.stderr)
        return 2
    coordinator_local = bool(getattr(args, "coordinator_local", False)) or (
        is_coordinator_endpoint(args.coordinator or "", args.bind or ""))
    plan = plan_roles(caps, spec, args.bind or "", rpc_server=args.rpc_server,
                      want=args.role, ram_cap_gib=args.ram_cap_gib,
                      is_coordinator=coordinator_local)
    if args.json:
        out = plan.to_dict()
        out["admission_invite"] = adm.to_dict()
        print(json.dumps(out, indent=2))
    else:
        print(adm.message())
        print(render_text(plan))
    pool_id = getattr(args, "register", "") or ""
    if pool_id:
        url = getattr(args, "registry_url", "") or os.environ.get("ADK_POOL_REGISTRY_URL", "")
        return register_plan(plan, pool_id, url, node_id=getattr(args, "node_id", "") or "",
                             send=send)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="adk.pool_role", description=__doc__.split("\n")[0])
    add_arguments(p)
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args(argv)
    if args.self_test:
        return self_test()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
