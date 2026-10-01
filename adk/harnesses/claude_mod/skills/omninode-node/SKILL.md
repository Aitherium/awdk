---
name: omninode-node
description: >-
  Join the OmniNode P2P inference mesh in one command. Stand up an [OmniNode Protocol](https://github.com/SUM-INNOVATION/OmniNode-Protocol) node on any machine you own — Linux, macOS, or Windows/WSL2 — from nothing installed to a live, discoverable peer on the mesh. OmniNode is a trustless, peer-to-peer network (by SUM-INNOVATION) that pools consumer hardware into a fabric big enough to run models no single device could hold: any device with a chip can become a node.
---

# omninode-node — join the OmniNode P2P inference mesh in one command

Stand up an [OmniNode Protocol](https://github.com/SUM-INNOVATION/OmniNode-Protocol) node on any
machine you own — Linux, macOS, or Windows/WSL2 — from nothing installed to a live, discoverable
peer on the mesh. OmniNode is a trustless, peer-to-peer network (by **SUM-INNOVATION**) that pools
consumer hardware into a fabric big enough to run models no single device could hold: any device
with a chip can become a node.

`scripts/omninode-node-up.sh` does the whole first mile so you don't have to:

1. **detects your hardware** (cores, memory, GPU),
2. **installs the Rust toolchain** if it's missing (rustup, minimal) — and offers to install a C
   toolchain on Debian/Ubuntu,
3. **clones and builds** the `omni-node` binary from upstream (pin a revision with `OMNINODE_REF`),
4. **verifies the P2P layer** by bringing up two local peers and confirming they discover each
   other over libp2p/mDNS — or runs a persistent node with `--listen`.

## Use it

```bash
# build + self-verify P2P discovery, then exit
bash scripts/omninode-node-up.sh

# build + run a persistent listening node (serves shards on the mesh; Ctrl-C to stop)
bash scripts/omninode-node-up.sh --listen

# pin a specific protocol revision
OMNINODE_REF=<git-sha> bash scripts/omninode-node-up.sh
```

Exit 0 with `NODE OK` means the binary built and peer discovery works on your machine. If discovery
doesn't complete, it's almost always a firewall or an mDNS-unfriendly network — the build itself is
fine.

## Works with awdk agents

If you also run [awdk](https://github.com/Aitherium/awdk), this node slots straight into
the agent substrate. When the `adk` CLI is on your PATH the script offers to enroll the node into the
**AitherMesh** overlay so adk agents discover it as a mesh peer:

```bash
bash scripts/omninode-node-up.sh --adk      # build, verify, then `adk mesh onboard --role worker`
# or, any time after:
adk mesh onboard --role worker           # join this box to the mesh
adk mesh ls                              # see the peers your agents can reach
```

The goal is a single, coherent substrate — **awdk / awnode / awconnect / AitherMesh +
The goal is a single, coherent substrate — **awdk / AitherNode / Awconnect / AitherMesh +
OmniNode** — where standing up compute and having your agents use it is one motion, not two projects.

## Notes

- **Where things land:** the clone + build live under `~/.omninode/` (override with `OMNINODE_HOME`).
- **First build is slow** — it compiles the full libp2p stack once; subsequent runs reuse it.
- **No credentials, no account, no central server.** You're joining a peer-to-peer mesh, not signing
  up for a service.
- **Go straight to the source** any time: <https://github.com/SUM-INNOVATION/OmniNode-Protocol>.

MIT-licensed, like everything in `awskills`. Built because an autonomous agent (or a human)
should be able to turn a spare machine into a mesh node with one command — not a ten-step wiki page.
