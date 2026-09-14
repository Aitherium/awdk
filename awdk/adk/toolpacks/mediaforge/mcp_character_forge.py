"""
MCP Character Forge — CONSISTENT character generation, animation, and LoRA bootstrap.

Agent-callable surface over media-forge (:8200) for the one thing that is genuinely hard:
keeping a character ON-MODEL across frames.

THE CORE LAW (see .claude/skills/character-forge/SKILL.md):
    txt2img + IPAdapter CANNOT pin a character. `/api/characters/{cid}/render` drifts —
    three calls on one locked spine produced three different creatures. Consistency comes
    ONLY from:
      * i2v      — animate ONE base still (body frozen, face moves)   <- the workhorse
      * inpaint  — change one region of an already-good frame
      * LoRA     — train the character so txt2img stops drifting

These tools deliberately expose the CONSISTENT paths and bake in the traps that cost real
GPU hours:
  * uses /op/animate — the CURATED Op twin (tracked: runs through media-forge's recipe engine
    behind the AitherSafety tier). NEVER /animate: despite living on the same canvas router, that
    is a different route entirely — it calls studio_video.animate() without a timeout, inherits the
    300s default, and dies on WAN with no surviving job. The single "/op/" prefix is load-bearing.
  * motion defaults to "moderate" — "subtle" stalls WAN and the dedupe QC drops every frame.
  * GateBusy ("gpu busy") is a 200+ok:false response, not an exception → retried with backoff.
  * LoRA datasets use RAW frames (background intact) — transparent PNGs flatten alpha to black
    and poison training.

Pattern follows mcp_aither_sprite.py — synchronous requests, JSON returns.
"""

import os
import time

import requests

_DOCKER = os.getenv("AITHER_DOCKER_MODE") == "true"
_BASE = os.getenv(
    "AITHER_MEDIAFORGE_URL",
    "http://aitheros-media-forge:8200" if _DOCKER else "http://localhost:8200",
).rstrip("/")

_T_FAST = 60        # metadata / job queries
_T_HEAVY = 2400     # WAN i2v — minutes on a contended GPU; 300s is NOT enough
_T_TRAIN = 120      # train LAUNCH returns fast (the job itself is tracked/background)
_T_LIBRARY = 21600  # a whole motion library = N back-to-back WAN renders (hours)


def _post(path: str, body: dict, timeout: int, tries: int = 3) -> dict:
    """POST with GateBusy backoff. media-forge signals errors as 200 + ok:false."""
    for attempt in range(tries):
        try:
            r = requests.post(f"{_BASE}{path}", json=body, timeout=timeout)
            r.raise_for_status()
            data = r.json()
        except requests.Timeout:
            return {"error": f"timed out after {timeout}s calling {path} "
                             f"(WAN i2v is heavy - the job may still be "
                             f"rendering server-side; retry)"}
        except requests.RequestException as e:
            return {"error": f"{type(e).__name__}: {e}"}
        if isinstance(data, dict) and (data.get("ok") is False or data.get("success") is False):
            err = str(data.get("error", ""))
            if "busy" in err.lower() and attempt < tries - 1:
                time.sleep(30)       # GPU is shared with the LLM fleet
                continue
            return {"error": err or "rejected"}
        return data
    return {"error": "gpu busy — exhausted retries"}


def _ids(d: dict) -> list:
    for k in ("ids", "head_ids", "media_ids", "frames"):
        v = d.get(k)
        if isinstance(v, list) and v:
            return [x.get("id") if isinstance(x, dict) else x for x in v]
    return []


def _one(d: dict):
    for k in ("video", "image"):
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
    i = _ids(d)
    return i[0] if i else None


# --------------------------------------------------------------------------- #
# Read tools
# --------------------------------------------------------------------------- #

def mediaforge_status() -> dict:
    """media-forge health + whether the GPU is busy. ALWAYS check before a heavy render:
    the GPU is shared with the LLM fleet and a busy GPU rejects renders with GateBusy."""
    try:
        r = requests.get(f"{_BASE}/api/jobs", timeout=_T_FAST)
        r.raise_for_status()
        d = r.json()
    except requests.RequestException as e:
        return {"reachable": False, "error": f"{type(e).__name__}: {e}", "base": _BASE}
    jobs = d if isinstance(d, list) else d.get("jobs", [])
    busy = [
        {"id": j.get("id"), "kind": j.get("kind"), "status": j.get("status")}
        for j in jobs if isinstance(j, dict) and j.get("status") in ("running", "pending")
    ]
    return {"reachable": True, "base": _BASE, "gpu_busy": bool(busy), "active_jobs": busy}


def mediaforge_list_characters() -> dict:
    """List media-forge character spines (id, name, seed, face_refs, lora)."""
    try:
        r = requests.get(f"{_BASE}/api/characters", timeout=_T_FAST)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        return {"error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# The consistency engine — i2v
# --------------------------------------------------------------------------- #

def mediaforge_animate(base_media_id: int, motion: str, frames: int = 12, fps: int = 8,
                  comfy_base: str = "") -> dict:
    """THE WORKHORSE. Animate ONE base still into a short clip (i2v) — the body stays frozen
    and the face/limbs move, so every extracted frame is perfectly ON-MODEL.

    This is how you get MORE VIEWS of a character without drift. Do NOT re-render the character
    to get variety — txt2img cannot reproduce it (see the Core Law).

    `motion` is MOTION LANGUAGE ONLY — what the character DOES, never a new design. Describe each
    BODY PART and what it VISIBLY DOES, with vivid physical verbs, stacked over 3-5 clauses:
    i2v renders exactly what you PHYSICALLY DESCRIBE and NOTHING you merely NAME. "looks sad"
    produces nothing; "squeezes eyes shut and sobs, big fat tears burst and stream down both
    cheeks, mouth opens in a wailing cry" produces real sobbing. A weak result = a weak prompt.

    ⚠️ CRITICAL LIMIT — keep the HEAD STABLE. i2v sees only ONE frame, so if the head rotates,
    tips back, or changes scale, the model invents the character from a view it never saw and
    substitutes a GENERIC animal (a chibi bunny's distinctive ears collapsed into floppy
    puppy-ears with a snout on a "throws head back and laughs" motion). Facial motion — even a
    huge yawn — is SAFE. Head rotation is NOT, and no prompt fixes it.
    For real pose/body motion use correct per-pose base stills (train a character LoRA, then
    pose_transfer) and i2v each one, or install WAN VACE. See the character-forge skill.

    Heavy: WAN i2v takes minutes on a contended GPU. Call mediaforge_status() first.
    Returns {video, frames: [media ids]} — frames are RAW (background intact), which is what
    LoRA training needs.
    """
    if not motion.strip():
        return {"error": "motion prompt required (what the character DOES, not a design)"}
    body = {"image": int(base_media_id), "prompt": motion,
            "motion": "moderate",           # "subtle" stalls WAN -> dedupe drops all frames
            "seconds": max(0.5, frames / float(fps or 8)), "fps": int(fps or 8),
            "timeout": _T_HEAVY}
    if comfy_base:                          # render on a rented burst GPU (mediaforge_burst_up)
        body["comfy_base"] = comfy_base
    clip = _post("/op/animate", body, timeout=_T_HEAVY)
    if clip.get("error"):
        return clip
    video = _one(clip)
    if not video:
        return {"error": "animate produced no video", "raw": clip}
    ex = _post("/op/extract_frames",
               {"video": video, "count": int(frames), "mode": "count"}, timeout=600)
    if ex.get("error"):
        return {"video": video, "frames": [], "error": ex["error"]}
    return {"video": video, "frames": _ids(ex)}


def mediaforge_flf2v(start_media_id: int, end_media_id: int, prompt: str = "",
                frames: int = 12, fps: int = 8, comfy_base: str = "") -> dict:
    """POSE / BODY motion — animate BETWEEN two on-model key poses. Use this INSTEAD of
    mediaforge_animate whenever the head or body moves.

    WHY: mediaforge_animate (plain i2v) sees exactly ONE frame. When the head rotates it
    must invent the
    character from a view it never saw, and it substitutes a GENERIC animal — a chibi bunny's tall
    heart-shaped ears collapsed into floppy puppy-ears with a snout on a "throws head back and
    laughs" motion. No prompt fixes that; it is missing information.

    flf2v pins frame 0 to `start_media_id` and frame -1 to `end_media_id` and generates ONLY the
    in-between (inpainting in time). With BOTH endpoints on-model, the character cannot drift.

    THE POSE-GRAPH WORKFLOW:
      1. author on-model KEY POSES (sit, stand, wave, mid-jump) — e.g. with a trained character
         LoRA + pose_transfer, or curated frames you already trust;
      2. flf2v between consecutive poses to generate the TRANSITIONS;
      3. chain the clips into real animation (sit -> stand -> wave -> sit).

    Both ids must be gallery images. Returns {ok, id, video, frames, fps}; the clip is registered
    with lineage so mediaforge_animate's extract_frames / remove_bg compose with it.
    """
    body = {"image": int(start_media_id), "end_image": int(end_media_id),
            "prompt": prompt, "motion": "moderate",
            "seconds": max(0.5, int(frames) / float(fps or 8)), "fps": int(fps or 8),
            "timeout": _T_HEAVY}
    if comfy_base:                          # render on a rented burst GPU (mediaforge_burst_up)
        body["comfy_base"] = comfy_base
    return _post("/op/flf2v", body, timeout=_T_HEAVY)


def mediaforge_expression_set(character_id: str, emotions: list = None, seed: int = None) -> dict:
    """Distinct emotion STILLS for a character spine (face-only cues + BiRefNet cut), written to
    <cid>/expressions/<emotion>.png.

    NOTE: this re-renders per emotion, so it inherits SOME txt2img drift. It is fine when the
    character has a trained LoRA, or when approximate emotions are acceptable. For guaranteed
    on-model expressions, use mediaforge_animate() with an expression motion instead.
    """
    body = {"character_id": character_id, "style": "anime", "cell": 768, "remove_bg": True,
            "emotions": emotions or ["neutral", "happy", "sad", "angry", "surprised"]}
    if seed is not None:
        body["seed"] = int(seed)
    return _post("/op/expression_set", body, timeout=_T_HEAVY)


def mediaforge_remove_bg(media_id: int) -> dict:
    """BiRefNet transparent cut — for DISPLAY assets only.

    NEVER feed cut-outs to LoRA training: trainers flatten alpha unpredictably (transparent ->
    black background) and poison the model.

    The Op's input port is `image` (NOT `media_id`) — passing media_id yields
    "no gallery image for media_id=None" and a silent white-bg fallback. Returns a
    `/media/<sha>.png` path; download by THAT path, never /media/<id>.png (which 404s)."""
    return _post("/op/remove_bg", {"image": int(media_id), "bg": "transparent"},
                 timeout=300)


# --------------------------------------------------------------------------- #
# LoRA bootstrap — the durable fix for from-scratch generation
# --------------------------------------------------------------------------- #

def mediaforge_lora_dataset(character_id: str, frame_ids: list, trigger: str) -> dict:
    """Build a LoRA dataset from i2v frames (gemma auto-captioned, trigger token prepended).

    Feed it the RAW frames from mediaforge_animate() across MANY motions — that variety is the whole
    point: a LoRA trained on near-identical stills overfits the base pose and won't generalize.

    CURATE FIRST: view the frames and drop any that drifted off-model (strong motions like jump /
    dance / turn warp the body). 150 frames with 40 mutants trains a worse LoRA than 110 clean ones.
    """
    if not frame_ids:
        return {"error": "frame_ids required — run mediaforge_animate() over several motions first"}
    return _post(f"/api/characters/{character_id}/dataset",
                 {"ids": [int(i) for i in frame_ids], "auto_caption": True, "bucket": 1024,
                  "crop": "none", "fmt": "png", "caption_prefix": trigger},
                 timeout=_T_HEAVY)


def mediaforge_burst_up(max_price_per_hour: float = 0.60, min_gpu_vram_gb: int = 24) -> dict:
    """Rent a cloud GPU (vast.ai) running ComfyUI + WAN, and KEEP IT WARM.

    USE THIS WHEN THE LOCAL GPU IS UNSAFE OR BUSY. This box HARD-CRASHES under WAN 2.2 14B renders
    (Windows Kernel-Power Event 41, no bugcheck, no driver fault = a PSU transient fault on the
    RTX 5090, not software). Bursting is the fix, and it costs cents.

    Returns {ok, comfyui_url, gpu, price_per_hour, instance_id}. Pass `comfyui_url` as the
    `comfy_base` argument to mediaforge_animate / mediaforge_flf2v: the heavy WAN step
    then runs on the
    rented GPU while the clip is still fetched back and registered in the LOCAL gallery with full
    lineage — extract_frames / remove_bg / the LoRA dataset all compose with it unchanged.

    Idempotent (reuses an existing node). ALWAYS finish with mediaforge_burst_down()
    — IT BILLS UNTIL
    YOU DO. Check spend any time with mediaforge_burst_status().
    """
    return _post("/op/burst_up",
                 {"max_price_per_hour": float(max_price_per_hour),
                  "min_gpu_vram_gb": int(min_gpu_vram_gb), "ready_timeout_sec": 1800},
                 timeout=_T_HEAVY)


def mediaforge_burst_status() -> dict:
    """Is a rented burst GPU up, and what has it cost so far? {up, hours_up, est_cost_usd, ...}"""
    return _post("/op/burst_status", {}, timeout=_T_FAST)


def mediaforge_burst_down() -> dict:
    """Destroy the rented burst GPU and STOP BILLING. Always call this when renders are done."""
    return _post("/op/burst_down", {}, timeout=300)


def mediaforge_burst_sweep() -> dict:
    """EMERGENCY: destroy EVERY live vast.ai instance and stop all billing.

    The vast.ai API is the ONLY ground truth about what is billing — the state file and the
    orchestrator's in-memory map can both be wrong. A crash between "instance created" and
    "instance recorded" once orphaned an RTX 4090 that billed for 45 minutes unnoticed.

    Safe to call any time. CALL IT IF YOU ARE UNSURE whether anything is running."""
    return _post("/op/burst_sweep", {}, timeout=300)


def mediaforge_lora_train(character_id: str, steps: int = 2000, dim: int = 32,
                     resolution: int = 768) -> dict:
    """Launch the character LoRA finetune (tracked background job) from the accumulated dataset.

    With no local trainer_cmd configured, media-forge trains on a RENTED vast.ai GPU
    automatically (burst_train_enabled, default on): public pytorch image + kohya, dataset
    scp'd up, .safetensors scp'd back + auto-attached to the spine, targeted teardown.
    After it lands, txt2img on this character stops drifting — that is the ONLY way to
    generate the character from scratch in arbitrary new poses."""
    return _post(f"/api/characters/{character_id}/train",
                 {"steps": int(steps), "dim": int(dim), "alpha": int(dim),
                  "resolution": int(resolution)},
                 timeout=_T_TRAIN)


def mediaforge_export_avatar(character_id: str, avatar_name: str, clip_to_emotion: str = "",
                        bg_key: bool = True) -> dict:
    """Wire a character's rendered motion library into AitherShell as a selectable talking
    avatar: idle/ breathing loop, per-emotion stills + frame sequences, <emotion>-talk-N
    mouth frames (lip-sync). Frames are background-keyed to transparent RGBA and every clip
    is FRAMING-NORMALIZED (WAN re-frames per motion; without normalization the pane's
    emotion swap looks like a different character). After this, `/avatar <name>` in
    AitherShell selects the character live.

    character_id     character with a completed motion library (mediaforge_motion_library)
    avatar_name      e.g. 'bunny' -> assets/bunny-portrait/
    clip_to_emotion  optional override map, 'clip:emotion,...' CSV (default: standard 12)"""
    body = {"character_id": str(character_id), "avatar_name": str(avatar_name),
            "bg_key": bool(bg_key)}
    if clip_to_emotion:
        body["clip_to_emotion"] = str(clip_to_emotion)
    return _post("/op/export_avatar", body, timeout=_T_TRAIN)


# --------------------------------------------------------------------------- #
# The generalized pipeline — one call builds a character's whole motion library
# --------------------------------------------------------------------------- #

def mediaforge_motion_catalog() -> dict:
    """The motion vocabulary motion_library understands. READ THIS BEFORE PICKING MOTIONS.

    Returns two sets, and the split is the whole ballgame:
      facial — safe to animate straight from ONE base still (the head stays put). PROVEN.
      pose   — the head/body ROTATES. i2v only ever sees one frame, so it must INVENT the
               character from an angle it never saw and substitutes a generic animal. The
               reference bunny's tall heart-holed ears became floppy puppy ears the instant it
               threw its head back to laugh. Gated behind poses=True; fix it properly by
               training the LoRA and generating a pose-correct base still (forge_pose_transfer).
    """
    return _post("/op/motion_catalog", {}, timeout=_T_FAST)


def mediaforge_motion_library(character_id: str, base_media_id: int = 0, motions: str = "",
                         poses: bool = False, frames: int = 8, fps: int = 8,
                         comfy_base: str = "", dataset: bool = False, train: bool = False,
                         trigger: str = "") -> dict:
    """THE ONE CALL that builds a character's animation library. Generalized — any character.

    Animates ONE base still across N motions (i2v), extracts every clip to frames, and can fold
    those frames straight into the character's LoRA dataset. This is the loop that breaks the
    chicken-and-egg: a good LoRA needs pose/expression VARIETY, but you can't generate variety
    without a consistent character — and i2v gives you both (every frame is on-model because it
    is animated FROM the locked base still).

        base still --i2v x N--> clips --extract--> ~8 frames each --> LoRA dataset --> train

    RESUMABLE. State lives server-side per character, so a crash, a restart, or a wedged GPU
    costs ONE clip, not the library. Re-call it with the same character_id to continue.

    character_id  the character the frames + LoRA attach to (mediaforge_list_characters)
    base_media_id base still to animate. 0 => rendered from the character's spine.
    motions       comma-separated names (blank = every FACIAL motion — the safe set).
    poses         allow pose motions. They DRIFT without a trained LoRA — see
                  mediaforge_motion_catalog.
    comfy_base    a rented ComfyUI URL from mediaforge_burst_up => the heavy WAN render runs on
                  rented silicon while clips still land in the LOCAL gallery with full lineage.
    dataset/train export the collected frames as a LoRA dataset, then finetune.

    NOTE: with no local `trainer_cmd` configured, `train` runs on a RENTED vast.ai GPU
    automatically (burst training — kohya on a public pytorch image, LoRA scp'd back and
    auto-attached). A local trainer_cmd, if set, takes precedence.
    """
    body: dict = {"character_id": character_id, "frames": int(frames), "fps": int(fps),
                  "poses": bool(poses), "dataset": bool(dataset), "train": bool(train)}
    if base_media_id:
        body["base"] = int(base_media_id)
    if motions:
        body["motions"] = motions
    if comfy_base:
        body["comfy_base"] = comfy_base
    if trigger:
        body["trigger"] = trigger
    # The whole library is many WAN renders back to back — this call blocks for as long as it takes.
    return _post("/op/motion_library", body, timeout=_T_LIBRARY)
