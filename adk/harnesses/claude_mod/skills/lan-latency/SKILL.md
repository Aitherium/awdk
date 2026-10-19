---
name: lan-latency
description: >-
  Fix a wired desktop or node that pings its own router at tens of milliseconds with jitter and zero NIC errors. The cause is NIC power saving (Energy-Efficient Ethernet, Green Ethernet, Advanced EEE, Power Saving Mode, Gigabit Lite). Covers the check, the fix via `awsettings network apply`, the Windows elevation trap, and the numbers that prove it worked.
---
<!-- Generated from the awskills pack (skills/lan-latency.md, github.com/Aitherium/awskills). Edit the pack, never this copy. -->

# lan-latency — a wired link that naps between packets

A desktop on wired gigabit (or 2.5 GbE) pings its router at **10–160 ms, avg ~50 ms**.
A second machine on the same switch sees 1 ms. The NIC's error and discard counters are
zero, the cable is fine, the link speed is right. Nothing looks broken, so people blame
the router, the switch, or the cable.

The NIC is saving power. Energy-Efficient Ethernet (802.3az) and the vendor extras on top
of it put the link into low-power idle between packets, and every wake costs latency. On
a machine that is on AC all day the saving is a few hundred milliwatts.

> Measured (2026-10): Realtek 2.5 GbE desktop, avg 50 ms / max 159 ms to the router.
> After turning EEE and Green Ethernet off: **avg 3 ms**, and 4 ms to a LAN peer. An
> Intel I219 node on the same switch was already at 1 ms.

## Check

```bash
awsettings network check              # power-saving switches + 10 pings to the gateway
awsettings network check --count 30   # a longer latency sample
awsettings network check --json       # machine-readable
awsettings network check --no-probe   # switches only, no pings at all
```

Exit **0** ok · **1** drift (a switch is on, Wi-Fi is up beside wired, wired avg
latency over the limit (default 5 ms), mesh overlay overhead, or an old mesh client) ·
**2** could not judge (no wired adapter, PowerShell/ethtool missing, every ping lost).
Never 0 when it could not look.

Power-saving switches are judged on **physical wired adapters only**; Bluetooth,
VPN/tunnel and virtual switch adapters are never written, and Wi-Fi is touched only by
the dual-homed rule below. On a Hyper-V host whose NIC is bound to an
external virtual switch, the default route lives on the `vEthernet (<switch>)` adapter;
the latency probe follows it and labels the path `vEthernet (...) -> <NIC>`.

Matched by name, because vendors differ: `Energy-Efficient Ethernet` / `Energy
Efficient Ethernet` (Realtek, Intel I219/I225/I226), `Advanced EEE`, `Green Ethernet`,
`Power Saving Mode`, `Gigabit Lite`. Only on/off switches, only toward off. A property
whose values are not a switch (`EEE Max Support Speed`) is left alone. A driver that
exposes none of them reports `no power-saving switch exposed by the driver` — that box
is not this problem.

## Two more causes `check` looks for

**Dual-homed: wired AND Wi-Fi up on the same subnet.** Both adapters hold an address on
the LAN, so the OS (and a mesh VPN choosing its direct path) may answer peers over
Wi-Fi. Measured: LAN peers replied in 0.9–2 s while the wired link was healthy.
`check` flags any Wi-Fi adapter that is up on a subnet a wired link that is UP also
holds; `apply` disables that Wi-Fi adapter, only while the wired link is up. Opt out
per machine or everywhere with `"wifi_when_wired": "keep"`.

**Mesh overhead.** For each mesh (Tailscale) peer reached over a DIRECT path to a
private LAN address, `check` pings the overlay address and the raw LAN address and
flags overlay avg > LAN avg + 5 ms. It also reports the local client version and flags
it when it is older than the newest online peer's. Measured: overlay to a LAN GPU node
went **188 ms -> 2 ms** after Wi-Fi was turned off beside the wired link and the client
was upgraded (1.98 -> 1.104). Relayed, offline and internet-direct peers are not judged
(no raw LAN round trip to compare); no mesh client installed is `n/a`, not a failure.
`--no-mesh` skips this probe.

## Fix

```bash
awsettings network apply --dry-run    # what it would change
awsettings network apply              # one UAC / sudo prompt for every change
```

Idempotent: nothing drifted means no command runs and no prompt appears. Each touched
adapter is restarted ONCE (all properties set with `-NoRestart`, then one restart): the
link drops for a few seconds. Do not run it over a remote session you cannot afford to
lose for that long. The verdict is a re-read of the adapters, not the elevated child's
say-so.

## The elevation trap (Windows)

- `Set-NetAdapterAdvancedProperty` without an admin token fails with **System Error 5**.
- Windows `sudo` is often disabled. `apply` does not need it: it writes a plan file and
  runs one elevated PowerShell via `Start-Process -Verb RunAs`, which writes a result
  file back (an elevated child's stdout cannot be read by the caller).
- Declining the UAC prompt exits **2**, not 0.
- Over SSH as an administrator the session already holds a full token; `apply` detects
  that and runs without a prompt.

Linux: one `sudo` for every `ethtool --set-eee <if> eee off`. ethtool settings do not
survive a reboot; persist them with your network manager (a udev rule or a
NetworkManager dispatcher script).

## Verify

Right after `apply` the link has just restarted and the first minute can look WORSE
(measured: avg 19–97 ms for ~2 minutes, then 3–4 ms). Wait a few minutes, then:

```bash
awsettings network check --count 30
```

Expect avg under 5 ms to the router on a quiet wired link. If every switch is off and
latency is still high, it is not this: look at load on the link (bufferbloat under a
big transfer), the switch, or the router itself — compare against a second wired
machine on the same switch.

## Intent travels, adapter names do not

The `network` settings domain (`~/.aither/network.json`) syncs only the intent:

```json
{"version": 1, "wired_power_saving": "off", "latency_max_ms": 5,
 "wifi_when_wired": "off", "adapters": {"Ethernet 2": "unmanaged"}}
```

`wired_power_saving` is `off` (default when the file is absent) or `unmanaged`;
`wifi_when_wired` is `off` (default) or `keep`.
`adapters` holds per-adapter overrides keyed by THIS machine's adapter names; it stays
home and is refused if it arrives in a synced profile.

```bash
awsettings --domain network push      # share the intent
awsettings --domain network pull      # take it on another machine, then `network apply`
awsettings network show               # the intent and the file it lives in
```
