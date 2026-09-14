# Image Generation Skill

Choosing an engine, generating through it, and proving the result is real.

## Choose the engine before you generate

| need | engine | why |
|---|---|---|
| ControlNet, IPAdapter, LoRA, inpaint, custom workflow | **ComfyUI** | the whole control ecosystem lives here |
| many images, fast, no fine control | **SANA** | one-step Sprint ~1s/image; ~10x cheaper per image |
| video (i2v) | **ComfyUI + WAN 2.2** | needs the 24GB band |
| 3D mesh from an image | **ComfyUI-3D** (Hunyuan3D) | separate service, port 8289 |

Say which engine you picked and why. "I used SANA Sprint because you asked for
twenty thumbnails and none of them need pose control" is a useful sentence.

SANA has **no** ControlNet/LoRA/IPAdapter ecosystem. If the ask contains any word
like pose, reference, style-match, or consistent, the answer is ComfyUI.

## Before generating: is there a backend?

```python
v = imagegen_verify(base_url=..., backend_type="comfyui")
if v["status"] != "healthy":
    # do not promise an image — see the deploy-image-gen skill
```

`model_count: 0` means the backend is running with **zero checkpoints**. Every
prompt will fail or return garbage, and no amount of prompt rewriting fixes it.
Check this first — it is the most common failure and the easiest to misdiagnose.

## Generating

Route through the blessed image path (`image_router` → AitherCanvas → ComfyUI),
not a raw HTTP poke at a sampler. The router handles workflow selection, model
resolution, and output placement; bypassing it means reimplementing all three
badly.

Always record what produced the image:

- **checkpoint** — which base model
- **seed** — the single most important field for reproducibility
- **prompt / negative** — as actually sent, after any enhancement
- **cfg, steps, sampler** — the knobs that change the result

An image you cannot reproduce is a screenshot, not an asset.

## Resolution and batch, per VRAM band

| band | safe resolution | batch |
|---|---|---|
| `large` (24GB+) | up to 1536 | 4-8 |
| `medium` (12-23GB) | 1024 | 1-4 |
| `small` (6-11GB) | 1024, `--lowvram` | **1 only** |
| `none` (CPU) | 1024, 4-8 step turbo | 1 |

On the `small` band, batch>1 or resolution>1024 will OOM — that is a recipe
`platform_trap`, not a guess. On CPU, a default 30-step SDXL workflow looks
exactly like a hang; use a turbo/lightning checkpoint at 4-8 steps.

## After generating: LOOK at it

A returned path is not proof the image is right. A black frame, a mangled hand,
and a masterpiece are all `200 OK`. Use `vision` to inspect the output before
declaring success, especially for:

- hands and faces (the classic failure regions — this is what ADetailer is for)
- text rendering (diffusion models still mangle it)
- whether the thing the user actually asked for is present in the frame

## Prompting notes that actually matter

- **Checkpoint dictates prompt dialect.** Illustrious/anime bases are booru-tag
  native (`1girl, solo, ...`); photoreal SDXL bases want natural language. Using
  the wrong dialect for the checkpoint is a bigger quality lever than any
  adjective you add.
- **Negative embeddings** (e.g. `negativeXL_D`) do more than a long hand-written
  negative prompt.
- **CFG above ~9** on SDXL burns contrast and saturates. If it looks fried, drop
  CFG before you touch the prompt.

## Discipline

- Never claim an image was generated without a path or bytes to show.
- Never report a backend as ready when verify says `degraded`.
- Record checkpoint + seed on every delivered image.
- If the user wants a consistent character across a set, stop and read the
  character-consistency skill — the technique is different and the intuitive
  approach does not work.
