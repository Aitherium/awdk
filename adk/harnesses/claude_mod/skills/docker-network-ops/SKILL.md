---
name: docker-network-ops
description: >-
  Diagnose container DNS and networking without fooling yourself. Container networking faults are latency-shaped, load-shaped and client-specific. Almost every instinctive way to measure them returns a confident wrong answer. This skill is the set of measurements that survive, plus the tool that runs them in the right order.
---

# docker-network-ops — diagnose container DNS and networking without fooling yourself

Container networking faults are **latency-shaped, load-shaped and client-specific**. Almost
every instinctive way to measure them returns a confident wrong answer. This skill is the
set of measurements that survive, plus the tool that runs them in the right order.

> Live-proven (2026-07-27, ~168-container fleet): Docker's embedded resolver `127.0.0.11`
> was measured **failing 46-78 of every 120 queries — 38-65%, sustained across every
> sampling round** — with clean 2000ms timeouts, while `conntrack` sat at 11% with zero
> "table full" events and `/proc/net/snmp` reported `Udp InErrors=0 RcvbufErrors=0`.
> Two separate one-shot probes before that returned **150/150 perfect** and were believed.

## The tool

[`tools/docker-net-doctor.py`](../tools/docker-net-doctor.py) — pure stdlib, no deps.
It runs the checks in the order that actually finds the fault, not the order you'd guess.

```bash
python tools/docker-net-doctor.py            # everything
python tools/docker-net-doctor.py pressure   # load vs cores + uncapped CPU consumers
python tools/docker-net-doctor.py kernel     # conntrack + UDP counters (rule OUT the theories)
python tools/docker-net-doctor.py arp        # GLOBAL ARP table vs the fleet's total entries
python tools/docker-net-doctor.py binds      # dnsmasq --bind-interfaces race detector
python tools/docker-net-doctor.py policy     # true resolv.conf per container
python tools/docker-net-doctor.py clients    # musl vs glibc vs nginx semantics
```

Exit `0` clean, `1` defect found, **`2` = could not determine, which is a FAILURE, not a
pass.** A check that could not run tells you nothing and must never read as green.

## The embedded resolver is a goroutine, not a service

`127.0.0.11` is served by a userspace goroutine inside `dockerd`. That single fact explains
the whole failure class:

- It is **not** in the kernel's data path, so **no kernel counter ever reports its losses.**
  conntrack, `Udp InErrors`, `RcvbufErrors`, `netstat -s` — all clean while it drops half
  your queries. Checking them is still worth doing, but as *exclusion*, not detection.
- It competes with every other goroutine in `dockerd`, which is also servicing your
  `docker ps`, your image pulls and your healthchecks. Under load it is not scheduled
  before the client's 2s timeout.
- You cannot tune it. There is no cache size, no worker count, no timeout knob.

**So the durable fix is architectural: stop putting it in the hot path.** Run a dnsmasq on
the container network, point every container's `resolv.conf` at it first, and feed it a
`hostsdir` map of container-name → IP so a fleet lookup is answered from a file:

```conf
# dnsmasq.conf — the directory is auto-watched; drop in a file, no reload needed
hostsdir=/etc/dnsmasq.hosts
```
```bash
# regenerate the map from the live fleet
docker ps --format '{{.Names}}' | while read -r n; do
  ip=$(docker inspect "$n" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
  [ -n "$ip" ] && echo "$ip $n"
done > hosts/fleet.hosts
```

### The `--strict-order` trap

If your fallback resolver is configured `--strict-order --server=127.0.0.11 --server=1.1.1.1`,
it queries the failing embedded resolver **first on every cache miss** and pays its full 2s
timeout before falling through. Its failure rate simply *is* the embedded resolver's,
attenuated only by its own cache hit rate. **A backup that fails whenever the thing it
backs up fails is not a backup.**

But do not just delete the flag: the `--server=//127.0.0.11` pin usually exists so a public
resolver can never authoritatively NXDOMAIN an internal service name. Change it, then
measure both dotted and dotless names.

## Three DNS clients, three different behaviours

This is why "container A resolves and container B doesn't" is normal, not a contradiction.

| Client | Behaviour with multiple nameservers | Failure signature |
|---|---|---|
| **glibc** | walks the list in order, **fails over** | slow but succeeds — hides a dead resolver entirely |
| **musl** (alpine) | queries **all nameservers in PARALLEL**, first response wins | a fast *failure* beats a correct answer → `bad address` |
| **nginx** | its own `resolver` directive, round-robins, **no failover inside a query** | one bad resolver = ~1/N of requests 502 |

Consequences that have each cost a session:

- `getent`/`nslookup` succeeding **proves nothing** — that's glibc failover masking a dead
  primary. The app fails because its own timeout budget expires during the failover.
- `wget` failing inside an alpine container while `nginx` in the **same container** is fine
  is expected. Neither one is the truth on its own.
- **Never conclude from a single client.** Test with the client the failing app actually uses.

## The `--bind-interfaces` startup race

`dnsmasq --bind-interfaces` binds only the addresses that **exist at exec time**. If the
container's network IP is not attached yet, it binds loopback and then runs
`Up (healthy)` forever while serving **nothing** on the address every client is configured
to use. Nothing logs an error — a partial bind is not fatal to dnsmasq.

```bash
docker exec <dns-container> netstat -lnup | grep ':53 '   # compare against --listen-address
```

`docker-net-doctor.py binds` does this comparison for you. Two corollaries:

- **Point the healthcheck at the address it is supposed to SERVE**, never `127.0.0.1`.
  A loopback healthcheck passes in exactly the broken case.
- If you pin the IP in compose to close the race, check the service's own comments first —
  some resolvers are attached at runtime with `docker network connect --ip` precisely
  *because* declaring the address in compose forces a full-network recreate.

## Reading resolver policy without lying to yourself

**`docker inspect --format '{{.HostConfig.Dns}}'` is NOT the resolver policy.** It returns
empty for every container that bind-mounts `/etc/resolv.conf`, and the mount supersedes it.
Reading that field once "proved" a fleet's DNS was unwired when it was wired fleet-wide.

```bash
docker exec <c> cat /etc/resolv.conf      # the only truth
```

A known-good policy for an IPv4-only container fabric:

```
nameserver <fleet-primary>
nameserver <fleet-secondary>
nameserver 127.0.0.11
options no-aaaa ndots:0 timeout:1 attempts:2 use-vc
```

- `timeout:1 attempts:2` — glibc counts attempts over the **whole list** and **doubles the
  timeout each pass**. `timeout:2 attempts:3` costs 2+2, 4+4, 8+8 = **up to ~12s per
  lookup**, so any caller with a 3-10s budget fails outright instead of failing over.
- `no-aaaa` — without it glibc fires A+AAAA together and fails the **whole** lookup with
  `EAI_AGAIN` on an IPv4-only fabric, even though the A query succeeds by itself.
- `ndots:0` — bare service names resolve in one query, not one per search domain.
- `use-vc` — forces TCP. **Do not remove this to "speed things up."** UDP drops are silent,
  so all attempts vanish and glibc reports `EAI_AGAIN`; TCP surfaces the drop as an error.

### Editing a bind-mounted resolver file

Editing the file is **live, no restart** — the cheapest lever you have. But it is mounted
as a single **file, by inode**:

> **Edit it IN PLACE.** Any editor that writes-then-renames gives the file a new inode. The
> running containers keep reading the OLD one while the copy on disk looks correct. This is
> completely invisible from the host.

Verify from **inside** a container, never from the host. And if the same logical file is
mounted from more than one source tree, edit **every** tree — a half-updated pair diverges
silently.

## The wedge (black hole)

A wedged dnsmasq stays `Up (healthy)`, holds its IP, listens — and answers nothing.
Signature is a **UDP `Recv-Q` that never drains**:

```bash
docker exec <dns-container> netstat -an | grep ':53 '
```

Its healthcheck often cannot see this (it may assert a record on a different address),
which is why it survives autoheal. `docker restart` clears it for minutes, then it wedges
again — **restarting is not a fix**, it's a way to lose the evidence.

## Measurement traps (each one produced a wrong conclusion)

1. **A one-shot probe is not a verdict.** Sample continuously with per-resolver attribution.
   Two clean 150/150 probes preceded the discovery of a 38-65% sustained failure rate.
2. **Timing a lookup with `docker exec` around it measures Docker.** Exec startup is ~300ms
   and highly variable. **Loop INSIDE one exec.**
3. **`date +%s%N` in busybox measures nothing.** busybox `date` silently drops `%N` and
   returns whole seconds, so every delta truncates to `0ms` and every lookup looks instant.
   Check with `docker exec <c> date +%N` — empty output means every timing from that
   container is garbage. Use python3 `time.monotonic()`.
4. **Probe the network the containers are actually on.** A compose project prefixes its
   network name (`<project>_<network>`). Probing the unprefixed name makes every resolver
   look dead.
5. **`2>/dev/null` on a discovery command turns "path doesn't exist" into "not found".**
   A suppressed error and a timed-out search are the same bug: a negative result from a
   check that never ran. `ls` the thing you searched before trusting any absence.
6. **A clean run on an idle box proves little.** These faults are load-shaped. Re-run under
   real load — a large build, a recreate wave — before calling anything healthy.
7. **Your scan can generate the load that produces its own false positives.** This is the
   nastiest one, because the tool looks like it's working. A fleet-wide egress probe that
   spawns one throwaway container per target *is* a load event, and load is exactly what
   induces the fault being measured. Measured on a **healthy** 167-container fleet: the
   wide pass reported **108 severed**; adding in-loop retries still reported **57**; every
   named container passed the identical command when run alone. In-loop retries cannot
   rescue you — they fire while the storm is still running.
   **The fix is a two-phase shape: treat the wide pass as a SCREEN, then re-verify only
   the suspects sequentially after the load settles.** A severed veth stays severed; a
   container merely starved by your scan comes back.

## The container that is `Up`, `healthy`, and completely severed

A broken veth pair leaves a container with a correct routing table, its IP, a green
healthcheck — and no network at all. One ran that way for **19 hours** undetected.

**The tell that saves you an hour:** it also times out against `127.0.0.11`. That resolver
lives in the container's *own* network namespace, so a container cannot fail to reach it
*over the network*. If `127.0.0.11` times out too, stop looking at DNS — the interface is
dead. Confirm with raw TCP to a known-good IP; if that times out while `/proc/net/route`
is correct, it's the veth.

```bash
docker-net-doctor.py egress          # screens every container, then re-verifies quietly
docker restart <name>                # rebuilds the veth
```

`docker network disconnect` + `connect` does **not** reliably fix it (measured: same IP
reassigned, still severed). `docker inspect` shows the interface attached the whole time,
so the container-level view agrees with the lie.

The durable defect is the **blindness**, not the veth: a healthcheck that never leaves the
container passes in exactly this case. Any service with outbound dependencies needs a
healthcheck that performs a real dependency round-trip.

> **Before you accept "broken veth", read the next section.** On a large fleet the same
> symptom is far more often the GLOBAL ARP table, and the restart that "fixes the veth" is
> also what hides it.

## The per-container fault with a global cause: ARP table overflow

```bash
docker-net-doctor.py arp
```

Linux keeps **ONE GLOBAL `arp_tbl`** whose entry count is shared across **every network
namespace**, while `gc_thresh3` — the ceiling on it — defaults to **1024**. Every container
holds its own ARP entries, so on a large single-bridge fleet their *sum* crosses that
ceiling, `neigh_alloc` starts refusing new entries, and whichever container ARPs next
cannot resolve its next hop.

It presents as a **per-container** fault, and that is the whole trap. Measured on a
187-container fleet (2026-08-02), one container showed:

- `getaddrinfo` failing **instantly** with `EAI_AGAIN` (not slow — instant)
- **no ARP entry** for either resolver in `/proc/net/arp`
- raw **UDP *and* TCP** `:53` both timing out
- `Up (healthy)`, resolv.conf byte-identical to a working container, correct routes and
  prefix — while the resolver answered a different container on the same bridge in **12ms**

Every per-container signal was green and the resolver plane was genuinely healthy. The only
evidence was a kernel line nothing reads: **1,991** `neighbour: arp_cache: neighbor table
overflow!` in 3.4h.

**Why it hid for weeks:** `--force-recreate` fixed it every single time. A recreate *churns
neighbour entries and frees table space*, so the container returns 0/6 → 6/6, the fix looks
proven, and the table refills minutes later. One container was repaired to 6/6 and was dark
again within 15 minutes; a later repair attempt scored **0 → 0**. The measured fleet total
was **~2000 entries against the 1024 ceiling** — roughly 2× over.

Raising the thresholds fixed that container **with no recreate at all** (its resolver went
`TimeoutError` → `0ms` in the same shell) and simultaneously fixed an *unrelated* container
that could not reach the Docker API proxy. One kernel tunable, two "separate" outages.

```bash
# plain Linux Docker host
sysctl -w net.ipv4.neigh.default.gc_thresh1=4096
sysctl -w net.ipv4.neigh.default.gc_thresh2=8192
sysctl -w net.ipv4.neigh.default.gc_thresh3=16384
```

**Docker Desktop / WSL2 needs a different door.** A `--privileged --net=host` container
lands in the *engine's* netns, where `/proc/sys/net/ipv4/neigh/default` **does not exist** —
so the knob is unreadable and unwritable from there, and `nsenter -t 1` does not reach it
either. Use the distro's init netns:

```bash
wsl -d docker-desktop -e sh -c 'sysctl -w net.ipv4.neigh.default.gc_thresh3=16384'
```

Three things worth stealing from this:

- **Diagnose the fleet, not the container.** When N containers fail the same way one at a
  time, suspect a shared global limit before N independent per-container faults.
- **A remedy that works every time but never lasts is evidence about the remedy**, not the
  fault. "Recreate fixes it" meant the recreate was *relieving pressure*, not repairing.
- **Count overflow events in a recent window, never cumulatively.** `dmesg` is cumulative
  since boot, so a raw count stays non-zero forever after a fix — and a check that can never
  go green gets switched off rather than satisfied.

## Application layer — infra alone cannot fix this

**`EAI_AGAIN` is POSIX for "temporary failure — try again."** Treating it as a hard outage
is always a bug, and it is the single most common one. One shared HTTP client classified
`ECONNREFUSED`, `ECONNRESET` and connect-timeouts as retriable but **not** `EAI_AGAIN` or
`ENOTFOUND` — so the one error class that is transient *by definition* was the only class
that got no retry, and a single resolver blip rendered "service unavailable" to users.

When retrying DNS failures:
- Give them a **larger** attempt budget than connection errors — bursts last seconds.
- Do **not** let them trip a circuit breaker. The service is up; only its *name* failed to
  resolve. Opening the breaker converts a resolver hiccup into synthetic 503s for every
  caller of that host.

## Before claiming a networking issue is fixed

1. Sampled continuously for ≥20 minutes, not a one-shot — **and under load, not idle.**
2. Kernel counters read and used to *exclude* causes, not to declare health.
3. Every declared `--listen-address` confirmed actually bound.
4. Policy verified from **inside** a container, with the client the failing app uses.
5. Every source tree updated if the file is mounted from more than one.
6. The consuming application retries `EAI_AGAIN` instead of surfacing it.
7. **The global ARP table has headroom** — `gc_thresh3` comfortably above the fleet's total
   entries, and no `neighbor table overflow` in a recent window. Skip this and a
   many-container fleet keeps losing one container at a time, forever, to a fault that
   every per-container probe reports as healthy.
