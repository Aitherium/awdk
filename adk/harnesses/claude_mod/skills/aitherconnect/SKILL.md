---
name: aitherconnect
description: >-
  Awconnect — wire your machine, agent, and browser into AitherOS. Awconnect is the onboarding layer: it connects a box you own to AitherOS — detecting local LLMs, setting up the gateway, joining the desktop/agent mesh, and (optionally) linking your browser — so a node you stood up locally becomes reachable and usable from the rest of the fleet and the portal.
---

# awconnect — wire your machine, agent, and browser into AitherOS

Awconnect is the onboarding layer: it connects a box you own to AitherOS — detecting local LLMs,
setting up the gateway, joining the desktop/agent mesh, and (optionally) linking your browser — so a
node you stood up locally becomes reachable and usable from the rest of the fleet and the portal.

## Set it up

With [awdk](awdk.md) installed:

```bash
adk connect                     # detect local LLMs, set up the gateway, join the desktop mesh
adk connect --api-key <key>     # connect using a cloud inference key instead of a local model
```

To make *this machine's* local agent reachable both ways (the fleet can call into it, and it can
call out), register its MCP endpoint with the gateway:

```bash
adk fleet connect-local         # bidirectional: register this machine's local agent MCP endpoint
```

## Join the mesh (even behind NAT / a firewall)

The mesh overlay (Conductor-assigned `10.77.0.0/16`) is how nodes reach each other privately:

```bash
adk mesh onboard --role worker  # join this node into the AitherMesh WireGuard overlay
adk mesh join --headscale       # NAT/CGNAT/firewall-friendly transport when raw WireGuard UDP is blocked
adk mesh ls                     # list the peers your agents can now reach
```

`--headscale` routes the tunnel through a Headscale control plane when raw WireGuard `UDP:51820`
isn't viable; the overlay IP is still Conductor-assigned, and it falls back to raw WireGuard
automatically if Headscale setup fails. See `adk mesh join --help` for the transport and
control-plane options.

## Connect your browser

```bash
adk deploy connect              # set up the Awconnect browser extension
```

## Use it

After connecting, the node appears in **aitherium.com → Workstation**, and agents anywhere in
your fleet can route to its inference and capabilities. `adk doctor` reports gateway + mesh health.

## Part of one substrate

awconnect is the seam between a local box and the wider fabric: it links an
[awnode](aithernode.md) to the control plane, lets [awdk](awdk.md) agents reach it,
and rides the same mesh that [OmniNode](omninode-node.md) uses to pool compute — while
[AitherZero](aitherzero.md) provisions what's underneath. One motion, not five.

MIT-licensed, like everything in `awskills`.
