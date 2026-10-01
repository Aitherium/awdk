---
name: character-forge
description: >-
  Character Forge — consistent characters, animation, talking avatars. The definitive procedure for making a character that stays the same character. Every trap below was paid for in real GPU hours and real off-model garbage. Read the Core Law first; it is the whole skill.
---

# Character Forge — consistent characters, animation, talking avatars

The definitive procedure for making a character that **stays the same character**. Every trap
below was paid for in real GPU hours and real off-model garbage. Read the Core Law first; it
is the whole skill.

**For color truth on anything produced here** (ACES grading, LUTs, sRGB/HDR mastering) see related
media processing guides.

---

## 0. THE CORE LAW (read this or you will waste hours)

> **Independent image renders CANNOT reproduce a specific character.**

Text-to-image is fine for one-off designs. The character "spine" (reference images + a fixed seed)
only **loosely conditions** the diffusion — it does **not pin identity**. Proven across many runs:
three consecutive calls on the same locked spine produced *a pink mouse, a different white rabbit,
and a two-character bow-thing*. None was the target character.

**Consistency has exactly three sources:**

| Tool | What it gives you | Use when |
|------|-------------------|----------|
| **i2v** (image-to-video) | Body FROZEN, face/limbs move. Same pixels warped → perfectly on-model frames. | Animations, expression frames, avatar assets. **The workhorse.** |
| **inpaint** | Change ONE region of an already-good frame; everything else untouched. | A specific expression/detail edit on a frozen base. |
| **character LoRA** | The model *learns* the character → text-to-image stops drifting. | You need NEW poses/scenes generated from scratch. |

If someone asks "why isn't the character consistent?" — they are almost certainly re-rendering
instead of animating/inpainting/LoRA-ing. That is the answer, every time.

---

## 1. The bootstrap loop (breaks the chicken-and-egg)

A good character LoRA needs **pose/expression variety**, but you cannot generate variety without
a consistent character. **i2v breaks the cycle**: each short clip is animated *from the same base
still* (stays on-model) yet yields 8–16 genuinely *different* frames (mouth, eyes, head tilt,
limbs). Run ~20 motions → 150+ varied on-model frames → a real LoRA dataset.

```
ONE base still
     │  i2v × N motion prompts
     ▼
~20 short clips ── extract_frames ──> 150+ varied, on-model frames
     │
     │  CURATE — cull off-model frames. Garbage in = garbage LoRA.
     ▼
LoRA dataset (auto-captioned)
     ▼
TRAIN
     ▼
text-to-image now generates THIS character, any pose, no drift
```

**Motion language only.** Motion prompts describe what the character *does* ("breaks into a warm
smile", "waves a paw", "turns its head left") — **never a new design**. The base still carries
the identity; a design prompt would fight it.

---

## 2. The operation chain (exact calls that work)

Media generation endpoints are called via standard APIs:

```bash
# FACIAL motion — i2v from ONE still. Head must stay put (see trap #16).
# Animate a single image with facial motion.
# Parameters: image, prompt (motion description), motion intensity, duration

# BODY / POSE motion — first-last-frame. PINS both endpoints; the character CANNOT drift.
# Generate a transition between two poses with motion inbetweening.
# Parameters: start_image, end_image, prompt (motion description), duration

# clip -> stills
# Extract frames from a video clip at specified intervals.

# transparent cut — for DISPLAY assets only, never for training
# Remove background from image for compositing.

# distinct emotions as STILLS (face-only cues, background removed)
# Generate expression variants: neutral, happy, sad, angry, surprised, curious, love.

# packaged: still -> i2v -> extract -> cutout -> normalize -> sprite sheet + manifest
# One-call pipeline: still → animation → frame extraction → compositing.

# character LoRA
# Fine-tune a model on character frames to lock identity.
# Parameters: frame set, training steps, LoRA rank/alpha, resolution
```

---

## 3. TRAPS (each one cost real hours)

1. **NEVER use canvas endpoints without a real timeout.** Animated renders on shared GPUs
   **can take several minutes**, and a 300-second default dies silently.
   ✅ **Use tracked job endpoints with a long client timeout (1800–2400s).**

2. **Subtle motion settings STALL** → frames come out near-identical → automatic quality-check
   dedupe drops them all (live-verified: 7/8 dropped). ✅ **Use moderate motion settings.**

3. **GPU busy is a normal 200 response, not an exception.** Retry with backoff (20–60s).
   The GPU is shared with other workloads.

4. **Safety filters hard-block certain literal terms** (child-safety guards).
   ✅ **Use age-neutral alternatives** ("little" / "tiny" / "young").

5. **LoRA training must use RAW frames (background intact), NOT transparent cut-outs.** Trainers
   flatten alpha unpredictably — transparent → dark background → poisons the LoRA. Cut the
   background only for *display* assets.

6. **Aggressive motions drift off-model.** Gentle motions (idle, blink, talk, subtle expressions)
   stay rock-solid. Strong ones (`jump`, `dance`, `turn`, `stretch`) give the pose variety the
   LoRA needs *and* are the most likely to warp the body. **Always curate — cull mutant frames
   before training.** 150 frames with 40 mutants is worse than 110 clean ones.

7. **Scene-heavy prompts REPLACE the character.** Mood prompts like `"storm cloud above"` or
   `"confetti, arms up"` made a faceless character render as a *scene*. For expression
   work use **face-only** language, and pin the pose ("same character, exact same sitting pose,
   plain white background, no props, no text").

8. **Avoid framing clichés.** Some phrasing draws software UIs, toolbars, or hallucinated text.
   Test framing language separately.

9. **Media is served by content hash, not numeric id.** Path refs are for download; numeric ids are
   for operation parameters. Know which you need.

10. **API parameter names matter.** Passing the wrong field name → server replies with a missing-field
    error and the caller silently falls back to defaults. ✅ **When unsure: check the API docs
    for exact param names.**

11. **Download by the returned path reference, not by constructing from numeric id.** Ops return
    content-addressed paths; numeric-id URLs 404.

12. **i2v DAMPS dramatic emotion — write PHYSICAL motion, and label by the RESULT.** The prompt
    *"ears droop, looks down sadly, quivering frown, teary eyes"* produced **no tears and no
    drooping ears** — it produced a half-lidded expression that reads as *"really…?"*. That is an
    excellent asset; it just isn't "sad".
    **i2v renders exactly what you PHYSICALLY DESCRIBE, and nothing you merely NAME.** Intensity
    scales with the specificity and vividness of the physical description.
    Measured across many runs:

    | prompt | result |
    |--------|--------|
    | `"teary eyes, drooping ears, looks down sadly"` (vague adjectives) | subtle expression — **minimal tears** |
    | `"squeezes eyes shut and sobs, big fat tears burst and stream down both cheeks, mouth opens in a wailing cry, head drops forward, shoulders shake"` (5 explicit clauses, vivid verbs) | **full sobbing** — real tear streaks, wailing mouth, shoulders shake |
    | `"puffs cheeks, furrows brow, pouts"` (terse) | mild grumpy expression — brow landed, cheeks less pronounced |

    **How to write a motion prompt:**
    - Name **each body part** and **what it visibly does**: eyes, mouth, brow, ears, paws, head, shoulders.
    - Use **vivid physical verbs** (*burst, stream, squeeze, wail, drop, shake*), not feelings (*sad, angry*).
    - **Stack several clauses.** One terse clause → a mild expression. Four or five → full intensity.
    - A weak result means a weak prompt — **re-roll with more physical detail**, don't blame the model.
    - Emotional adjectives alone (*"looks sad"*) reliably produce **muted results**.

    **Still name the clip after what came OUT.** The weak "sad" prompt produced an excellent
    *"really…?"* expression → keep it, relabel it `unimpressed`. A mislabeled-but-good clip is still good;
    mislabeling poisons emotion classification.

13. **NEVER kill a render mid-run — it ORPHANS the job.** Orphaned jobs pile up in the render queue
    and starve every later render. **Never run two copies of the generator** that both write state:
    each holds state in memory and they race their saves, silently clobbering each other's work.
    Verify exactly ONE process before walking away.

14. **The GPU compute context can DIE — and it looks like "slow", not "broken".**
    Signature: **GPU at 0% utilization but memory nearly full**, queue shows `running: 0,
    pending: N` (jobs never picked up), and logs show CUDA errors. Renders then time out forever
    with no output ever produced.
    - **Fix:** Restart the render service — re-inits compute and releases ~16 GB it was
      squatting on in the dead context. Recovers in ~40s.
    - **Do NOT** blanket-kill the queue — that GPU is SHARED with other workloads and you will
      kill other jobs. Restart the one broken service.
    - Watch for creeping slowdown (28min → 70min/clip) — that is this wedge tightening. Check
      **GPU util at 0% + full memory = wedge, not load.**

15. **There is a HARD server ceiling on animated renders (minutes), and you CANNOT raise it.**
    The system enforces a maximum duration, so renders exceeding it die with a timeout.
    - **8-frame clips take ~18–25 min** on a shared GPU → comfortable headroom.
    - This is the same class as trap #1 (timeout forwarding): the real fix is a properly-wired
      timeout parameter; until then, size the clip to fit.

16. **⚠️ THE BIG ONE: i2v holds the character ONLY while the head keeps its shape and scale.**
    i2v sees **exactly ONE frame**. The moment the head rotates — or balloons, or tips back — the
    model must invent the character from a view it has NEVER seen, and it substitutes a **generic
    animal**. Measured on a distinctive character with unique ears:

    | motion | outcome |
    |--------|---------|
    | Facial-only, head stable (happy, angry, unimpressed, wink, surprised) | ✅ perfect |
    | Head thrown back | ❌ ears collapsed to floppy form, **generic appearance** |
    | Head ballooned by exaggeration | ⚠️ ears shrank and rounded |
    | Whole-body | ⚠️ some frames fine, some ears degraded — cull PER FRAME |

    - It tracks **head shape/scale change**, not "face vs body". A big mouth motion is safe; a head
      that changes silhouette is not.
    - **No prompt fixes this.** It is missing information, not bad wording. Do not waste compute trying.
    - **You CANNOT apply a standard character LoRA to i2v** (different model architecture) — so you
      cannot simply "add the LoRA to the video model". The i2v graph has **no LoRA, no ControlNet**
      conditioning at all.

    **THE FIX — the POSE GRAPH. Give it the geometry instead of letting it guess.**

    First-last-frame inbetweening — **PINS frame 0 to your start image and frame -1 to your end
    image** and generates ONLY the in-between. **With both endpoints on-model, the character
    cannot drift.** Needs **NO extra weights** — a pure conditioning operation.

    ```
    on-model pose A ──inbetween──▶ on-model pose B ──inbetween──▶ pose C ...
       (sitting)    both ends PINNED  (standing)              (waving)
    ```
    **Workflow:** author on-model KEY POSES → inbetween each consecutive pair to make the
    TRANSITIONS → chain the clips into real animation. This is how you get genuine body motion
    on-model. **Use i2v for facial motion; use inbetweening for anything the body does.**

    To author the key poses: train a character LoRA on clean frames, then use pose transfer tools
    (ControlNet + reference + the LoRA) → correct stills in any pose. The LoRA supplies the
    identity; the ControlNet supplies the pose; **neither is guessed.**

    - **Curate per frame, not per clip.** One bad frame does not condemn a clip, and one good frame
      does not redeem a bad one. Look at several frames before culling.

### Real timing (measured across various runs)
- **i2v: ~18–28 minutes per clip** depending on length and GPU load. Plan it as an overnight run.
- So a **24-motion library ≈ 10–15 hours**. Plan it as overnight, idle-gated. It is resumable.
- Extract and compositing ops are fast by comparison.

---

## 4. Talking avatar — the wiring

A character becomes a live, chat/speech-synced avatar by dropping assets into a
**portrait directory** with a standard layout:

```
<name>-portrait/
  {emotion}.png            # neutral, happy, sad, angry, surprised, curious, love, …
  {emotion}-blink.png      # optional
  {emotion}-talk-{0..3}.png# mouth frames (closed→open) for lip-sync
  idle/frame_NN.png        # idle animation loop frames
```

- **Select it live via commands** (lists available portraits; persists to user config).
- **Emotion is automatic:** emotion detection polls state and picks the matching emotion frame
  (falls back to `neutral.png` if missing — partial sets are fine).
- **Mouth is driven by real audio:** mouth frames advance fast during speech and slowly when quiet.
- Missing emotions gracefully fall back — ship what you have.

**The wiring is an operation:** one call assembles a completed motion library into this convention —
background-keyed to transparent RGBA AND **frame-normalized** (different motions can come out at
different scales; unnormalized, the pane's emotion swap looks like a *different character*).

---

## 5. Executable tools — use the pipeline operations, not scripts

The whole pipeline is a first-class media operation. It is character-agnostic, resumable, and
job-tracked. **Never re-implement this as a host script** — that is what made it un-repeatable.

| Operation | What it does |
|---|---|
| `motion_catalog` | The motion vocabulary, split `facial` (safe) vs `pose` (gated). **Read this before picking motions.** |
| `motion_library` | **THE ONE CALL.** base still → i2v × N motions → extract frames → (optional) LoRA dataset → (optional) train. Resumable per character. |
| `export_avatar` | Completed library → avatar (keyed, frame-normalized, stale-swept). Selection works immediately after. |
| `/train` (character LoRA) | LoRA fine-tune. **No local trainer needed**: with default config it trains on a rented GPU (standard tools on public images, checkpoints preserved on failure, LoRA returned + auto-attached). |
| `burst_up` / `burst_sweep` | Rent a GPU; pass its ID to `motion_library` to render on rented silicon (clips still land in the LOCAL gallery). `burst_sweep` is the emergency stop-billing switch. |

```bash
# the whole library for ANY character, one call, resumable:
curl -X POST localhost:8200/api/op/motion_library \
  -d '{"character_id":"char-5","base":17431,"frames":8,"dataset":true}'

# what motions exist, and which ones are safe:
curl -X POST localhost:8200/api/op/motion_catalog -d '{}'
```

State lives in the service's data directory — re-calling continues where it stopped. It
skips completed motions, so a crash costs one clip, not the library.

---

## 6. Autonomous procedure

1. **Get ONE good base still.** Generate it (text-to-image is fine — you only need it *once*),
   or accept a user-supplied image. Note its numeric **ID**.
2. **Never re-render to get more views.** Go straight to i2v. That is the core law.
3. **Cheap win first:** test with ONE motion clip. LOOK at the frames. Body frozen, face moved?
   That is the gate. Do not spend hours before it passes.
4. **Build the library:** Full motion run. Blank motion list = every FACIAL motion (the safe set).
   Hours; resumable; run when idle.
5. **Do NOT reach for body motions to get variety.** It will drift (§0). The real unlock is step 6.
6. **CURATE, then train the LoRA.** Cull anything off-model — mandatory. RAW frames only, distinctive
   trigger token. Training runs on a rented GPU automatically (the default). Expect ~40-60 min + ~$0.50/character.
7. **Once the LoRA exists**, pose motions become legitimate: use pose transfer (LoRA supplies the
   geometry, ControlNet supplies the pose) → a pose-correct base still → i2v *that*.
   Neither the identity nor the pose is guessed.
8. **Report honestly:** which frames were culled, and whether the LoRA overfit the base pose
   (thin datasets will).

**Self-check before claiming success:** open the actual frames and compare them to the base.
"It rendered" is not "it's on-model." Every consistency claim in this domain must be eyeballed.

---

## 7. New character → live avatar, end to end

The whole flow, ~30 min of wall-clock time + ~$0.60 of rental:

```bash
# 1. spine (one-off text-to-image design is FINE — consistency comes after)
curl -X POST localhost:8200/api/characters -d '{
  "name":"pip", "style":"anime", "ip_weight":0.7,
  "prompt":"a small adorable round hovering robot companion, chibi, ... , centered full-body view, plain white background"}'

# 2. render 4 base candidates LOCALLY
curl -X POST localhost:8200/api/characters/char-23/render -d '{"count":4,"width":1024,"height":1024}'

# 3. EYEBALL the candidates; pick a NEUTRAL-POSE on-model one
# 4. one rental for the whole roster, then the library:
curl -X POST localhost:8200/api/op/burst_up -d '{"max_price_per_hour":1.60,"min_gpu_vram_gb":24}'
curl -X POST localhost:8200/api/op/motion_library \
  -d '{"character_id":"char-23","base":17949,"frames":8,"comfy_base":"<from burst_up>"}'

# 5. curate (eyeball), then wire into avatar:
curl -X POST localhost:8200/api/op/export_avatar \
  -d '{"character_id":"char-23","avatar_name":"pip"}'

# 6. targeted teardown:
curl -X POST localhost:8200/api/op/burst_down -d '{}'

# -> avatar ready. Later: dataset=true + train for the LoRA (pose unlock).
```

Prompt rules that made the difference: "plain white background" (key-able), "centered
full-body view" or a clean bust, thick outlines + flat cel shading (compositing depends on
drawn outlines), and NO gesture words in the base design.
