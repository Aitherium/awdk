---
name: event-loop-debugger
allowed-tools: Bash, Read, Grep, Edit
description: Detect, pin, and fix asyncio event-loop stalls in Python services — a synchronous call or CPU-bound work blocking the loop — including on the free-threaded python3.14t build where py-spy and gdb fail.
argument-hint: [<service-url>|--capture|--fix]
---

## Context
- Working directory: !`pwd`
- Target: $ARGUMENTS

## Your Role
You debug the #1 cause of mysterious latency in an async Python service: a
**synchronous call awaited inline on the event loop**, or **CPU-bound work without
an executor**. A single asyncio loop runs all coroutines, so when it is blocked
*every* request stalls together — health checks time out, the service looks
intermittently "down," and restarts don't help because the cause is in the
workload, not the container.

This is the counterpart to a py-spy/cProfile profiler: use it when the loop is
**wedged**, and specifically on the **free-threaded `python3.14t`** build, where
py-spy, gdb, and signal-triggered faulthandler all fail.

## Your Task

### 1. Detect — is the loop actually blocked?
The authoritative signal is event-loop **lag** (scheduling drift), not raw latency.
If the app exports a loop-lag gauge (see "Instrument", below), read it; otherwise
probe a normally-instant endpoint repeatedly and watch for multi-second spikes:
```bash
for i in $(seq 1 60); do
  curl -s -o /dev/null -w '%{time_total}\n' --max-time 30 "$URL/health"; sleep 0.3
done | sort -rn | head
```
Stalls clustering at round values (3s/5s/8s/30s = fixed timeouts/retries) or in
~periodic bursts (a background task) are the giveaway. If lag is ~0 but requests
are slow, the loop is fine — look at the proxy/LB or a downstream service.

### 2. Isolate — backend vs proxy
Probe the backend **directly** (bypass any reverse proxy / LB) and through the
proxy. Backend clean + proxy slow ⇒ it's the proxy (commonly an nginx `resolver`
with no `resolver_timeout`; see Fixes). Backend slow ⇒ continue.

### 3. Capture — the blocking call site (the hard part on py3.14t)
Do **not** reach for py-spy/gdb on free-threaded builds — they won't attach or
symbolize, and under a process manager the app's `faulthandler.register(SIGUSR1)`
is often clobbered, so signal-triggered dumps don't fire. Use a
**`faulthandler.dump_traceback_later()` watchdog** instead: it fires from
faulthandler's own C timer thread, independent of the GIL and of any signal
handler, and dumps every thread's stack — including thread-pool workers.

Add this once at startup (env-gated so it's off in normal operation):
```python
import os, asyncio, faulthandler

def arm_stall_watchdog(threshold_s=2.0, path="/tmp/stall_dumps.txt"):
    if os.environ.get("STALL_DUMP") != "1":
        return
    f = open(path, "a", buffering=1)
    faulthandler.enable(f)
    async def _watch():
        poll = max(0.25, threshold_s / 4)
        while True:
            faulthandler.dump_traceback_later(threshold_s, repeat=False, file=f)
            try:
                await asyncio.sleep(poll)   # if healthy, we return and cancel
            finally:
                faulthandler.cancel_dump_traceback_later()
    asyncio.create_task(_watch())
```
Set `STALL_DUMP=1`, reproduce the stall, then read the dump file. Find the thread
running the event loop (its stack bottoms out in `asyncio/.../run_forever`). **Its
top frames are the blocking call.**

Last resort (only if the container has `CAP_SYS_PTRACE`): a sidecar
`docker run --rm --pid=container:<svc> --cap-add=SYS_PTRACE alpine sh -c 'apk add strace && strace -f -tt -T -yy -p <pid>'`
shows the blocking *syscall* + socket peer (not Python frames). `futex` herd with
no CPU burner = lock/GIL contention; long `recvfrom`/`ssl.read` = blocking I/O;
long gaps with no syscalls = pure-Python/CPU on the loop.

### 4. Identify — match the antipattern
| Frame on the loop thread | Antipattern |
|---|---|
| `requests`/`urllib3`/any sync SDK → `ssl.read`/`socket.recv` | **Sync HTTP/SDK awaited inline** (blocking even if wrapped in `async def`) |
| `socket.create_connection` / `getaddrinfo` | **Sync DNS on the loop** — `timeout=` does NOT bound getaddrinfo; a flaky resolver freezes the loop |
| `yaml/scanner.py …`, slow | **Pure-Python YAML** (no C accel on 3.14t) — config parsed on the loop |
| loop thread idle in `epoll_wait`, a thread-pool worker hot | NOT the loop — that work is correctly off-loop (red herring) |
| periodic ~Ns bursts | a **background `while True` tick** doing sync work each cycle |

### 5. Fix
- **Sync HTTP/SDK** → `await loop.run_in_executor(None, lambda: sync_call())`, or an
  async client. For an async-wrapped-but-internally-sync call:
  `await loop.run_in_executor(None, lambda: asyncio.run(coro()))`.
- **Sync DNS** → async client, or resolve in a daemon thread with a hard
  wall-clock cap (`settimeout` does not bound `getaddrinfo`).
- **Pure-Python YAML** → route `yaml.safe_load` through `yaml.CSafeLoader` (~6×
  faster); patch once at process start.
- **CPU-bound work** → `run_in_executor` (frees the loop; on free-threaded builds
  it genuinely parallelizes).
- **Heavy background tick in an API gateway** → move it to a separate worker
  process; keep the gateway a thin router.
- **nginx reverse-proxy resolver stall** → set `resolver_timeout` (default 30s!)
  and a sane `valid=` TTL on the `resolver` directive; a flaky container DNS
  otherwise blocks variable-`proxy_pass` re-resolution for up to 30s.

### 6. Verify
Re-measure. Backend-direct latency should be flat and loop lag near 0. Capture a
fresh dump to confirm the blocking frame is gone (it now appears in a thread-pool
worker, or not at all).

## Instrument (catch regressions automatically)
Export an event-loop lag gauge so a future regression is caught by an alert
instead of by hand. Measure scheduling drift and publish it to Prometheus:
```python
async def lag_monitor(set_gauge, interval=0.5, stall_s=1.0):
    loop = asyncio.get_running_loop()
    while True:
        t0 = loop.time(); await asyncio.sleep(interval)
        lag = max(0.0, (loop.time() - t0) - interval)
        set_gauge("event_loop_lag_ms", lag * 1000)
```
Alert on `event_loop_lag_ms > 500` for 2m (warning) and `> 3000` for 1m (critical).
