# Deploy an Image-Gen Backend Skill

Stand up your own ComfyUI or SANA backend with the `imagegen_*` tools, instead of
waiting for someone to provision one. This is the image-generation twin of the
node-bootstrap flow used for LLM inference.

## The five-step loop

### 1. Detect — what can this box actually run?

```python
hw = imagegen_detect_hardware()
# {"system_info": {...}, "vram_band": "large"|"medium"|"small"|"none"|"unified",
#  "recommended_recipe": "cuda-comfyui-24gb",
#  "capability": {"sdxl_1024": True, "video_wan22": True, ...}}
```

`vram_band` is the number that matters. It decides which model profile is viable:

| band | VRAM | what runs |
|---|---|---|
| `large` | 24GB+ | full studio SDXL **+ WAN 2.2 video** |
| `medium` | 12-23GB | full studio SDXL, no video |
| `small` | 6-11GB | minimal profile, `--lowvram`, batch 1 |
| `unified` | Apple | studio SDXL, **native install only** |
| `none` | no GPU | CPU turbo checkpoints, seconds-to-minutes per image |

### 2. Resolve — which recipe?

```python
r = imagegen_resolve_recipe()                       # auto
r = imagegen_resolve_recipe(prefer_engine="sana")   # fast one-step instead
r = imagegen_resolve_recipe(recipe_id="cuda-comfyui-12gb")  # explicit
```

`prefer_engine` is a **tiebreaker, not a hard gate**. Asking for `sana` on a box
where no SANA recipe fits returns the best overall match plus a warning — it does
not fail. Check `warnings` before you act on the result.

### 3. Plan — see it before you do it

```python
plan = imagegen_plan_deployment(recipe_id="cuda-comfyui-12gb")
# {"steps": [...], "compose_yaml": "...", "env": {...},
#  "secret_env_keys": ["AITHER_MODEL_DOWNLOADS"], "notes": [...]}
```

Pure — no side effects. **Read `notes`.** The most common note is that some model
URLs resolve to fleet-internal hosts (`aitheros-minio:9000`) that a self-hosted
box cannot reach; those downloads fail silently and you end up with a backend that
runs and generates nothing.

Note `secret_env_keys` reports only key NAMES. The resolved URLs are presigned
credentials and are deliberately never returned in a tool result — `imagegen_apply`
writes them to a `0600` env file instead.

### 4. Apply

```python
imagegen_apply(recipe_id="cuda-comfyui-12gb", dry_run=True)   # commands only
imagegen_apply(recipe_id="cuda-comfyui-12gb")                 # actually deploy
```

Three modes, and they behave differently on purpose:
- **docker-compose** — really runs `docker compose up -d`.
- **native** (Apple Silicon) — returns the manual steps. Docker on macOS has **no
  Metal passthrough**, so a containerised ComfyUI silently runs on CPU ~100x
  slower. This recipe is native on purpose; do not "fix" it into compose.
- **delegate** (cloud burst) — reports the exact fleet command rather than
  pretending to run tooling the public pack does not ship.

### 5. Verify — the only step that proves anything

```python
v = imagegen_verify(base_url="http://localhost:8188")
# {"status": "healthy", "models_loaded": True, "model_count": 17,
#  "sample_models": ["Juggernaut-XL-v9.safetensors", ...]}
```

**Health alone is not proof.** ComfyUI answers 200 on `/system_stats` with zero
checkpoints installed and generates nothing. Verify probes the capability path
(`/object_info/CheckpointLoaderSimple`, or `/v1/backends` for SANA) and asserts at
least one model is really loaded.

- `status: "healthy"` — up **and** has models. Only now can you promise a render.
- `status: "degraded"` — up but **zero models**. The profile download did not land.
  Report this; never call it ready. The CLI exits `2` on degraded so CI catches it.

## Registering with the fleet

```python
imagegen_register_backend(
    base_url="http://localhost:8188",
    backend_type="comfyui",
    token=...,           # or AITHER_AUTH_TOKEN
)
```

Fail-closed: missing token or Genesis URL returns an error, never an anonymous
POST. An unauthenticated register would either 401 silently or let any caller
point fleet image routing at an arbitrary URL. Genesis authorises server-side —
this client only ever presents identity, it never decides permission.

## From the shell

```bash
python -m adk.toolpacks.image_bootstrap detect
python -m adk.toolpacks.image_bootstrap resolve --prefer-engine sana
python -m adk.toolpacks.image_bootstrap apply --recipe-id cuda-comfyui-12gb --dry-run
python -m adk.toolpacks.image_bootstrap verify --base-url http://localhost:8188
```

Exit codes: `0` healthy, `1` error, `2` degraded.

## Discipline

- Never promise an image before `imagegen_verify` says `healthy`.
- `model_count: 0` is a **deployment** failure, not a prompt failure. Check it
  before you start rewriting prompts.
- Recipes carry `platform_traps` — they surface in `warnings`. Read them; each one
  is a real failure someone already hit (SANA CPU offload under WSL2, macOS Metal
  passthrough, 6GB OOM on batch>1).
