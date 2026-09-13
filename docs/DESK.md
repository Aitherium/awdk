# Desktop Avatar (Desk)

Desk is a VRM avatar bridge that lets self-hosted ADK agents drive a desktop avatar running on the local machine.

## Overview

When running an ADK agent on the **host machine** (not in a container), you can control a Desk avatar via HTTP loopback:

- **Live animation** — trigger emotional states (IDLE, GREETING, TALK, HAPPY, FINGER_GUN, DANCE) or custom `.vrma` motion captures
- **Character switching** — swap between 19+ preloaded avatars (D:\desk\characters\*)
- **State sync** — mirror speaking/listening/idle states, microphone and output mute status
- **Audio visualization** — drive audio-level indicators from live mic input

All tools are **fire-and-forget**: if Desk is unavailable, tools silently no-op and return `{sent: false}`.

## Architecture & Access Constraints

**Desk runs on loopback-only** (127.0.0.1:47831). This is intentional:
- Containerized agents CANNOT reach it without a host relay
- Host relay (tunnel from container to host Desk bridge) is an owner-gated item.
- Host relay (tunnel from container to host Desk bridge) is not yet available
- Only HOST-run adk agents can use Desk tools directly

If you need a containerized agent to drive Desk:
1. This is in the backlog — the tunnel relay is not yet built
2. For now, run your agent on the host (`adk up` locally, or in WSL2 with host-network access)

## Setup

### 1. Start Desk

Desk is an Electron app running at D:\desk. Start it with:

```powershell
cmd /c D:\desk\desk-start.cmd
```

(Runs detached in a background process.)

**Verify it's live:**

```powershell
curl http://127.0.0.1:47831/health
# Expected: {"ok":true,"lastState":null}
```

### 2. Enable in Your Agent

Desk tools are **auto-enabled** by default. Disable them with an env var:

```bash
export ADK_PERSONA=0  # Disable desk tools (default is 1)
```

### 3. Register the Tools

```python
from adk import AitherAgent
from adk.builtin_tools import register_builtin_tools

agent = AitherAgent("lyra")

# Register desk + other tools
register_builtin_tools(agent, categories=["file_io", "web", "desk"])

# Or let auto-detect pick for you (if identity supports it)
# register_builtin_tools(agent)  # Uses IDENTITY_DEFAULTS
```

**Desk is included by default for:**
- (Currently: opt-in only. Add your identity to IDENTITY_DEFAULTS in adk/builtin_tools.py if you want auto-inclusion.)

## Available Tools

All return JSON strings.

### persona_status()
**Get liveness of the avatar.**

```python
# Returns: {available: true/false, ok: true/false, lastState: {...}}
result = await agent.tool("persona_status", {})
```

### persona_animate(animation)
**Trigger an animation.**

- `animation`: One of `IDLE`, `GREETING`, `TALK`, `HAPPY`, `FINGER_GUN`, `DANCE`, or `FILE:<name>.vrma`
  - `FILE:` prefix references a `.vrma` file in D:\desk\characters\<char>\animations\

```python
result = await agent.tool("persona_animate", {"animation": "TALK"})
# Returns: {sent: true/false, animation: "TALK"}
```

### persona_set_character(name)
**Switch to a different avatar character.**

- `name`: Directory under D:\desk\characters\ (e.g., `"default"`, `"angel"`)

```python
result = await agent.tool("persona_set_character", {"name": "angel"})
# Returns: {sent: true/false, character: "angel"}
```

### persona_speak_state(activity)
**Update speech activity state.**

- `activity`: One of `"speaking"`, `"listening"`, `"idle"`

```python
result = await agent.tool("persona_speak_state", {"activity": "speaking"})
# Returns: {sent: true/false, activity: "speaking"}
```

### persona_audio_level(level)
**Visualize mic input level (0.0–1.0).**

```python
result = await agent.tool("persona_audio_level", {"level": 0.5})
# Returns: {sent: true/false, level: 0.5}
```

### persona_mute_microphone(muted)
**Visual mute indicator (does NOT affect actual audio input).**

```python
result = await agent.tool("persona_mute_microphone", {"muted": true})
# Returns: {sent: true/false, muted: true}
```

### persona_mute_output(muted)
**Visual mute indicator for output (does NOT affect actual audio output).**

```python
result = await agent.tool("persona_mute_output", {"muted": false})
# Returns: {sent: true/false, muted: false}
```

## Integration with Awconnect (Self-Hosted)

When running a self-hosted agent pack on your machine:

1. **Start Desk** — run `desk-start.cmd`
2. **Run ADK agent with Desk tools** — register `"desk"` category
3. **The agent can now drive your avatar** — during conversations, long-running tasks, or on your own schedule

Example: An agent that syncs its internal reasoning state to avatar animations:

```python
from adk import AitherAgent
from adk.builtin_tools import register_builtin_tools

agent = AitherAgent("lyra")
register_builtin_tools(agent, categories=["file_io", "desk"])

# During a task:
response = await agent.chat(
    "Plan a week of meetings, and show me your thinking on the avatar",
    system_prompt="Use persona_animate() during planning phases to show TALK state"
)
```

## Example: Conversational Avatar

```python
import asyncio
from adk import AitherAgent
from adk.builtin_tools import register_builtin_tools

async def avatar_chat():
    agent = AitherAgent("iris")
    register_builtin_tools(agent, categories=["web", "desk"])
    
    # Start with a greeting
    await agent.tool("persona_animate", {"animation": "GREETING"})
    await agent.tool("persona_speak_state", {"activity": "listening"})
    
    # Have a conversation
    response = await agent.chat("What do you think about AI?")
    
    # React to response
    await agent.tool("persona_speak_state", {"activity": "speaking"})
    await agent.tool("persona_animate", {"animation": "TALK"})
    
    print(response.text)

asyncio.run(avatar_chat())
```

## Troubleshooting

### "desk tools not available"
- Verify Desk is running: `curl http://127.0.0.1:47831/health`
- Check ADK_PERSONA env var is not set to `0`
- Check that httpx is installed (required for HTTP bridge)

### Tool call returns `{sent: false}`
- Desk likely unavailable or timed out (1-second timeout per request)
- Look at the `error` field in the response for details
- Restart Desk and try again

### "127.0.0.1 connection refused"
- Desk process crashed or didn't start
- Restart: `cmd /c D:\desk\desk-start.cmd`

## Future Work (Owner-Gated)

- Host relay tunnel — container agents → host Desk bridge
- Host relay tunnel — container agents → host Desk bridge (planned)
  - Enables containerized agents (e.g., in Docker, Kubernetes) to drive avatars on the host
  - Status: backlog

## See Also

- **Desk app** — D:\desk (Electron fork, MIT license)
- **Voice integration** — pair with `adk.voice` tools (hear/say_to_file) for full voice-avatar loop
- **Lyra identity** — voice + desk-native agent profile (TBD)
