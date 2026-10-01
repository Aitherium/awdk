---
name: aithermesh
description: >-
  Stand up a private mesh and onboard nodes + agents to it. AitherMesh is a private overlay network (WireGuard, 10.77.0.0/16) that lets machines you own — anywhere, behind any NAT or firewall — reach each other as if they were on one LAN, and lets your agents run *across* them. This skill uses [AitherZero](aitherzero.md)'s automation-scripts to do the whole thing: create the control plane, join nodes to the overlay, and deploy mesh-native agents onto them — idempotent, one playbook, or step by step.
---

# aithermesh — stand up a private mesh and onboard nodes + agents to it

AitherMesh is a private overlay network (WireGuard, `10.77.0.0/16`) that lets machines you own —
anywhere, behind any NAT or firewall — reach each other as if they were on one LAN, and lets your
agents run *across* them. This skill uses [AitherZero](aitherzero.md)'s automation-scripts to do the
whole thing: create the control plane, join nodes to the overlay, and deploy mesh-native agents onto
them — idempotent, one playbook, or step by step.

## Set up the mesh (one command)

With the AitherZero environment loaded — clone
[AitherZero](https://github.com/Aitherium/AitherZero), run `./build.ps1` (it generates the
manifest; there is no committed one), then `Import-Module ./AitherZero.psd1 -Force`. Full setup in
the [`aitherzero`](aitherzero.md) skill:

```powershell
# End-to-end: ensure the control plane, join every node in your fleet, deploy agents, verify.
Invoke-AitherPlaybook deploy-mesh-agent

# Preview first (no changes made):
Invoke-AitherPlaybook deploy-mesh-agent -Variables @{ DryRun = $true }
```

`deploy-mesh-agent` runs the full sequence: **ensure control plane (Headscale) reachable → join the
overlay (Linux + WSL2 nodes) → deploy the mesh agents → verify agents registered on the mesh.**

## Or do it step by step

Each stage is its own numbered automation-script, runnable with `az <number>`:

```powershell
az 3225   # Ensure-MeshControlPlane   — create/ensure the control plane, reachable by remote nodes
az 3218   # Join-AitherNet-Overlay    — onboard a remote node to the overlay (10.77 WireGuard)
az 3201   # Join-Mesh                 — join this node to the mesh and configure replication
az 3229   # Deploy-AdkMeshAgent       — deploy a mesh-native agent onto a remote node (over SSH)
az 3217   # Onboard-AdkAgent          — expose a locally self-hosted agent to the control plane
az 3231   # Register-MeshEndpoint     — register/upsert a node's mesh endpoint
```

Node-type-specific joins (pick the one matching the target):

```powershell
az 3224   # Join-Headscale-LinuxNode  — a native Linux node (e.g. a server / DGX)
az 3220   # Join-Headscale-WSL2Node   — a WSL2-hosted Docker node (Windows box)
az 3214   # Onboard-ClusterNode       — onboard this machine as a secure cluster node
az 3215   # Onboard-WindowsCpuNode     — onboard a remote Windows CPU-offload node
```

## Define your fleet once

The nodes you onboard come from a single editable file, `config/nodes.yaml` — one entry per machine
(host, SSH user, shell, credential reference). The playbook and the join scripts read it, so adding
a machine to the mesh is: add a row, re-run the playbook (it's idempotent).

## Manage + verify

```powershell
az 3103   # Manage-NodeFleet    — inspect and manage the fleet of onboarded nodes
az 3102   # Watch-MeshFailover  — watch the mesh for node failover
```

The playbook's final step verifies agents are registered on the mesh; a healthy run ends with the
nodes reachable over `10.77.x.x` and the agents discoverable to each other.

## Part of one substrate

The mesh is the fabric the rest ride on: [Awconnect](awconnect.md) is the per-machine seam
onto it (`adk mesh onboard`), an [awnode](aithernode.md) is a machine made useful on it,
[awdk](awdk.md) is the agent runtime that spans it, [AitherZero](aitherzero.md) provides
the automation-scripts this skill drives, and [OmniNode](omninode-node.md) pools the nodes into one
compute fabric over it. One motion, not five.

MIT-licensed, like everything in `awskills`.
