"""
Measure inter-node RTT over the overlay mesh.

The placement solver needs actual measured latency to avoid placing shards on
unreachable nodes. This probe measures RTT from this node to its peers and
caches results with TTL.

CRITICAL: a peer that cannot be reached returns None, NOT a large number.
A solver that reads "unreachable" as "slow" will happily place a shard on a
dead node. Return None for unknown/unreachable peers.

ASYMMETRY is expected and documented: this returns the LOCAL VIEW. Peer A
measuring to B may see different latency than B measuring to A, due to
asymmetric network paths, load, or one-way drops.

Public API:
  measure_node_latencies(self_id, peers: list[dict]) -> {peer_id: rtt_ms or None}
  __main__ for CLI testing.

All measurements include per-peer timeout; a peer that does not respond within
the timeout maps to None. Cache results with 60s TTL to avoid hammering the
network.
"""
from __future__ import annotations

import json
import logging
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CachedLatency:
    """Cached latency measurement with expiration."""

    rtt_ms: float | None
    cached_at: float
    ttl_seconds: int = 60

    def is_expired(self) -> bool:
        """Check if this measurement has expired."""
        return time.time() - self.cached_at > self.ttl_seconds


# Global cache, keyed by (self_id, peer_id)
_latency_cache: dict[tuple[str, str], CachedLatency] = {}


def _measure_ping(
    target_addr: str, samples: int = 3, timeout_per_sample: float = 2.0
) -> float | None:
    """Measure RTT via ICMP ping if available."""
    try:
        # Use ping -c (Unix) or ping -n (Windows)
        count_arg = "-c" if shutil.which("ping") else "-n"
        if sys.platform == "win32":
            count_arg = "-n"
            timeout_ms = int(timeout_per_sample * 1000)
            cmd = ["ping", "-n", str(samples), "-w", str(timeout_ms),
                   target_addr]
        else:
            timeout_sec = int(timeout_per_sample)
            cmd = ["ping", "-c", str(samples), "-W", str(timeout_sec),
                   target_addr]

        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout_per_sample * samples * 1.5,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        if result.returncode != 0:
            return None

        # Parse output for min/avg/max on Unix or min/max/avg on Windows
        rtts = []
        for line in result.stdout.split("\n"):
            if "time=" in line and "ms" in line:
                try:
                    parts = line.split("time=")
                    if len(parts) > 1:
                        time_part = parts[1].split()[0]
                        rtt = float(time_part)
                        rtts.append(rtt)
                except (ValueError, IndexError):
                    pass

        if rtts:
            return sorted(rtts)[len(rtts) // 2]

        return None

    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _measure_tcp_connect(
    target_addr: str, port: int = 443, samples: int = 3,
    timeout_per_sample: float = 2.0
) -> float | None:
    """Measure RTT via TCP connect timing."""
    try:
        rtts = []
        for _ in range(samples):
            start = time.perf_counter()
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout_per_sample)
            try:
                sock.connect((target_addr, port))
                elapsed_ms = (time.perf_counter() - start) * 1000
                rtts.append(elapsed_ms)
            except (socket.timeout, OSError):
                return None
            finally:
                sock.close()

        if rtts:
            return sorted(rtts)[len(rtts) // 2]

        return None

    except Exception:
        return None


def measure_node_latencies(
    self_id: str, peers: list[dict[str, Any]]
) -> dict[str, float | None]:
    """
    Measure RTT from this node to each peer.

    Args:
        self_id: this node's identifier (for caching)
        peers: list of peer dicts with at least 'id' and 'address' keys

    Returns:
        {peer_id: rtt_ms or None}. A peer that times out maps to None, not
        a large number. The solver must distinguish between "slow" and
        "unreachable".

    Caching: results cached for 60s per (self_id, peer_id) pair to avoid
    hammering the network. Expired results are re-measured.
    """
    results = {}

    for peer in peers:
        peer_id = peer.get("id")
        target_addr = peer.get("address") or peer.get("host")

        if not peer_id or not target_addr:
            logger.warning(f"peer missing 'id' or 'address': {peer}")
            results[peer_id or "unknown"] = None
            continue

        cache_key = (self_id, peer_id)
        cached = _latency_cache.get(cache_key)

        if cached and not cached.is_expired():
            results[peer_id] = cached.rtt_ms
            continue

        # Measure: try ping first, fall back to TCP connect
        rtt_ms = _measure_ping(target_addr)
        if rtt_ms is None:
            rtt_ms = _measure_tcp_connect(target_addr)

        results[peer_id] = rtt_ms

        # Cache the result
        _latency_cache[cache_key] = CachedLatency(
            rtt_ms=rtt_ms,
            cached_at=time.time(),
            ttl_seconds=60,
        )

    return results


def clear_cache() -> None:
    """Clear all cached latency measurements."""
    global _latency_cache
    _latency_cache.clear()


def test_measure_without_network() -> bool:
    """Verify measure_node_latencies works without actual network access."""
    try:
        peers = [
            {"id": "peer-1", "address": "192.0.2.1"},
            {"id": "peer-2", "address": "192.0.2.2"},
        ]
        result = measure_node_latencies("self", peers)

        if not isinstance(result, dict):
            print(f"FAIL measure_node_latencies() returned {type(result)}, "
                  f"expected dict")
            return False

        if set(result.keys()) != {"peer-1", "peer-2"}:
            print(f"FAIL unexpected peer ids: {result.keys()}")
            return False

        for peer_id, rtt in result.items():
            if rtt is not None and not isinstance(rtt, (int, float)):
                print(f"FAIL peer '{peer_id}' rtt is {type(rtt)}, "
                      f"expected float or None")
                return False

        print("PASS measure_node_latencies() works (returns dict with "
              f"None for unreachable peers)")
        return True

    except Exception as e:
        print(f"FAIL measure_node_latencies() raised {type(e).__name__}: {e}")
        return False


def test_cache_expiry() -> bool:
    """Verify cache expiry works as expected."""
    try:
        clear_cache()

        cache_key = ("self", "test-peer")
        _latency_cache[cache_key] = CachedLatency(
            rtt_ms=10.0,
            cached_at=time.time() - 100,
            ttl_seconds=60,
        )

        cached = _latency_cache[cache_key]
        if not cached.is_expired():
            print("FAIL cache entry should be expired")
            return False

        _latency_cache[cache_key] = CachedLatency(
            rtt_ms=10.0,
            cached_at=time.time(),
            ttl_seconds=60,
        )

        cached = _latency_cache[cache_key]
        if cached.is_expired():
            print("FAIL fresh cache entry should not be expired")
            return False

        print("PASS cache expiry works as expected")
        clear_cache()
        return True

    except Exception as e:
        print(f"FAIL cache test raised {type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        test1 = test_measure_without_network()
        test2 = test_cache_expiry()
        passed = test1 and test2
        sys.exit(0 if passed else 1)

    if len(sys.argv) > 1 and sys.argv[1] == "--probe":
        # CLI mode: probe a list of addresses
        if len(sys.argv) < 3:
            print("Usage: latency_probe.py --probe <addr1> [<addr2> ...]")
            sys.exit(1)

        peers = [
            {"id": f"peer-{i}", "address": addr}
            for i, addr in enumerate(sys.argv[2:])
        ]
        result = measure_node_latencies("self", peers)
        print(json.dumps(result, indent=2, default=str))
        sys.exit(0)

    print("Usage:")
    print("  latency_probe.py --self-test          # Run self-tests")
    print("  latency_probe.py --probe <addr> ...   # Measure latency "
          "to addresses")
