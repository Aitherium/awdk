---
name: ps5-bridge
description: >-
  Let an agent play a PS5 game you own: convert your own dumped PS5 executable with AnyPS5, start it with its Aither bridge switched on, and drive it with the ps5-bridge tool pack (watch frames, hold buttons, measure fps). Use when an agent should play, test, record or learn from a real PS5 title, or when wiring a game into an agent-training loop.
---
<!-- Generated from the awskills pack (skills/ps5-bridge.md, github.com/Aitherium/awskills). Edit the pack, never this copy. -->

# ps5-bridge — an agent plays a PS5 game you own

[AnyPS5](https://github.com/wizzense/AnyPS5) converts a PS5 executable into a native
Windows or Linux program. It does not emulate anything: the game runs as a normal process
against reimplemented system libraries. Its **Aither bridge** opens a local socket. The
bridge reports every completed frame and accepts button presses. The `ps5-bridge` tool
pack lets an agent use it.

```
[your dumped game + AnyPS5 + bridge]  <-- frames / button presses -->  [agent + ps5-bridge pack]
         its own process (GPL-2.0)            127.0.0.1 only               your agent
```

## Hard limits (read first)

- **Bring your own dump.** You need a decrypted executable that you dumped from a console
  you own. Nothing here provides games or keys, or dumps or decrypts anything, and you
  should not ask an agent to find them.
- **AnyPS5 is GPL-2.0-only.** Run it as its own process. Do not copy its code into a
  proprietary product. The pack only speaks the socket protocol.
- **Early project.** Few titles run so far. Check the fork's `docs/user/COMPATIBILITY.md`
  before you start.

## 1. Build and convert

Follow the fork's `docs/dev/BUILD.md`. On Windows, only the MinGW-w64 GCC 15.2.0 toolchain
it names is supported. Build the libraries too, because `libs` is a separate target:

```bash
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
cmake --build build --target libs --parallel
relinker --windows source/eboot.elf app.exe     # drop --windows for Linux
```

Lay out the output as `docs/user/USAGE.md` describes: `libs/*.prx` and `app0/`.

## 2. Start the game with the bridge on

```bash
APS5_AGENT_BRIDGE=47500 ./app.exe
```

The bridge stays off when the variable is unset. A value that is not a port number stops
the game.

## 3. Drive it

```bash
pip install awdk
adk pack install ./awpack/packs/ps5-bridge
```

| tool | use |
|---|---|
| `ps5_connect(port=47500)` | attach |
| `ps5_fps(seconds=2)` | confirm frames are flowing; run this first |
| `ps5_frame()` | wait for the next frame (flip count, size, timestamp) |
| `ps5_press(buttons, frames=N, lx, ly, rx, ry, l2, r2)` | hold buttons and sticks for N frames, then release |
| `ps5_disconnect()` | detach; held buttons are released |

Buttons: `cross circle square triangle up down left right l1 r1 l2 r2 l3 r3 options touchpad`.
Sticks run from 0 to 255 and are centred at 128. Hold a stick at `lx=0` to push left.

## Check it actually works

1. `ps5_fps` returns roughly the game's real frame rate. **0 means no frames reached the
   agent.** In that case the variable was not set, the libraries are stale (`--target libs`
   was not rebuilt), or the wrong port was used.
2. `ps5_press(["cross"], frames=30)` makes something visible happen in the game. A
   `last_frame.n` that keeps advancing proves the game is running. Only the screen proves
   the input landed.
3. After `ps5_disconnect`, check that no button stays held.
