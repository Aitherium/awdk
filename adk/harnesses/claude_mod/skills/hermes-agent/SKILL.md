---
name: hermes-agent
description: Install Nous Research's Hermes agent and run it on your own model — a self-improving agent with persistent memory, autonomous skill creation, cron automation and multi-platform messaging. Covers pointing it at a local OpenAI-compatible endpoint, adding the AitherOS toolset over MCP, and the exact config shapes that silently fail if you get them wrong.
---

# hermes-agent — Nous Research's self-improving agent, on your hardware

[Hermes](https://github.com/nousresearch/hermes-agent) (MIT) is an agent with a closed
learning loop: it creates its own skills, keeps persistent memory with user modeling, runs
scheduled automation via cron, and reaches you on Telegram, Discord, Slack, WhatsApp and
Signal. It's model-agnostic — which is what makes it worth pairing with your own inference.

## Install

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash    # Linux/macOS/WSL2/Termux
iex (irm https://hermes-agent.nousresearch.com/install.ps1)           # Windows PowerShell
```

The installer bundles what it needs — uv, Python 3.11, Node.js, ripgrep, ffmpeg (and Git Bash
on Windows) — so it doesn't matter much what you already have.

```bash
hermes           # start chatting
```

**Check:** `hermes` opens a prompt and answers. `~/.hermes/` exists.

## Point it at your own model

```bash
hermes model     # interactive picker
```

Choose **custom** and give it the endpoint from [`local-inference`](local-inference.md). To do
it in the config file instead, edit `~/.hermes/cli-config.yaml`:

```yaml
model:
  provider: "custom"
  base_url: "http://localhost:11434/v1"
  api_key: "not-needed-for-local"
  default: "qwen3:8b"
```

> ⚠️ **Two config shapes silently do nothing.** These were verified against Hermes' own
> `cli-config.yaml.example`:
>
> - The top-level key is **`model:` (a mapping)**, *not* `models:` (a list). A list is
>   **ignored without an error** — Hermes keeps using whatever it used before, and you'll
>   swear the config isn't being read. It is; it's the wrong shape.
> - `provider: "custom"` means *any OpenAI-compatible endpoint*. The aliases `ollama`,
>   `vllm`, and `llamacpp` also work.

**Check — ask Hermes something and confirm your own server served it:**

```bash
ollama ps        # or: check your vLLM / llama-server log for the request
```

## Add the AitherOS toolset over MCP

Hermes takes MCP servers in the same config file. Add alongside `model:`:

```yaml
mcp_servers:
  aitheros:
    url: "<your-mcp-url>"
    headers:
      Authorization: "Bearer <your-api-key>"
```

`mcp_servers` entries accept **either** `command`/`args` (stdio servers) **or** `url` (HTTP
servers) — not both. Get a scoped key with the toolkit rather than reusing a personal one:

```bash
pip install awdk
adk connect          # writes the gateway URL + a scoped key
```

Unlike OpenClaw, there is **no `aither integrate hermes` command yet** — Hermes wiring is the
config block above, merged by hand. If you want it automated, `aither integrate list` shows
what's currently supported.

**Check:** ask Hermes *"list your MCP tools"*. It should name AitherOS tools next to its own
40+ built-ins. Restart Hermes if nothing appears — MCP servers connect at startup.

## Install this skill pack

Hermes is compatible with the [agentskills.io](https://agentskills.io) standard, so this pack
drops straight in:

```bash
bash scripts/install-awskills.sh --target hermes
```

Or by hand: `~/.hermes/skills/<name>/SKILL.md`. See [`install-skills`](install-skills.md).

Hermes also **writes its own skills** as it learns. Skills from this pack sit alongside those;
they don't conflict. If Hermes generates a skill that overlaps one of these, prefer the one
with the sharper `description` — that's what decides which gets loaded.

## Useful commands

```bash
hermes model              # choose the LLM provider
hermes tools              # enable/disable tools
hermes config set <k> <v> # individual settings
hermes config get <k>
```

## Run Hermes as an agent pack under the toolkit

The toolkit bundles a Hermes-flavored pack, the same way it does OpenClaw:

```bash
adk install pack:hermes
adk run --agents hermes
```

That runs a Hermes-style agent inside the AitherOS loop — it does **not** install or configure
upstream Hermes. Use the installer above for the real thing; use the pack if `adk` is your
daily driver.

## Tool calling on a local model

Hermes' whole value is doing things, which means tool calls must actually parse. If Hermes
*describes* calling a tool instead of calling it, the problem is on the **serving** side, not
in Hermes:

```bash
vllm serve <model> --enable-auto-tool-choice --tool-call-parser hermes
```

The `hermes` parser is named after this model family's chat template and is the right choice
for Nous Hermes models and the many fine-tunes that adopted it. Full table in
[`local-inference`](local-inference.md) § tool calling.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Config edits do nothing | used `models:` (list) instead of `model:` (mapping) | fix the shape |
| Still hitting a paid provider | `provider` not set to `custom`/`ollama`/`vllm` | re-run `hermes model` |
| No MCP tools | server not reachable, or Hermes not restarted | check the URL, restart |
| Talks about tools, never calls them | tool-call parser mismatch on the server | set `--tool-call-parser` |
| Skills ignored | flat `.md` instead of `<name>/SKILL.md` | [`install-skills`](install-skills.md) |

## Next

- **[`openclaw`](openclaw.md)** — the same wiring for OpenClaw
- **[`local-inference`](local-inference.md)** — the endpoint Hermes points at
- **[`awdk`](awdk.md)** — the toolkit that provides the MCP tools
