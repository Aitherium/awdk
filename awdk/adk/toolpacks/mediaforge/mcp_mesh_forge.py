"""
MCP Mesh Forge — image/text → textured 3D geometry over media-forge (:8200).

WHY THIS IS THIN, AND WHY THAT IS THE POINT. The engines have been running for
months with nothing agent-callable in front of them, and the obvious-looking fix —
a new service that orchestrates ComfyUI-3D — is one this platform already built and
already deleted. From `.DEPLOYMENT/compose/docker-compose.aitheros.yml`:

    # ── MeshGen (REMOVED) ───────────────────────────────────────────
    # MeshGen was a pointless proxy that called ComfyUI-3D.
    # All 3D generation now goes directly to comfyui-3d:8188.

So this adds no hop. media-forge ALREADY exposes `/api/studio/make3d` and
`/api/studio/txt2img3d`, already runs them through its own job tracker, and already
owns the Hunyuan3D pipeline (shape → texture → GLB). What was missing was only the
agent surface — the same shape as the other 24 tools in this pack, which is why it
lives here rather than anywhere new.

THE MANIFEST CLAIM. `.toolpack.yaml` advertised an "image, video and 3D" pipeline in
four places while shipping no 3D tool — the phantom-capability shape PTB001 exists to
catch — and carries a note saying the claim may be restored only in the SAME commit
that adds `mediaforge_generate_3d`. This is that commit.

IT RETURNS A JOB, NEVER A MESH. A textured GLB is multi-MB and these renders take
minutes; media-forge's own default timeout on both routes is 900s. Handing an agent a
blob down a tool channel would be wrong twice over, so the contract is
dispatch → `job_id` → `mediaforge_3d_status(job_id)`.

🚨 "NO BACKEND HERE" IS A CORRECT ANSWER, AND ON THIS FLEET IT IS THE COMMON ONE.
`aitheros-hunyuan3d` (:8290) and `comfyui-3d` (:8289) both sit behind the
`creative-full` / `dgx-hybrid` compose profiles and neither runs by default. Measured
2026-08-24, BOTH candidate hosts were full: the 5090 had 4,792 MiB free of 32,607
against a 6-16 GB need, and the DGX Spark had 1 GB available of 121 GB with its own
3D container already at Exit (137) from a prior OOM. A tool that answers a 3D request
by starting a 7 GB model on either would reproduce the incident that crash-looped the
orchestrator 17+ times. So an absent backend is reported plainly, with the lanes that
would place one and what each one measured — never retried into existence, and never
disguised as a slow render.
"""

import os

import requests

_DOCKER = os.getenv("AITHER_DOCKER_MODE") == "true"
_BASE = os.getenv(
    "AITHER_MEDIAFORGE_URL",
    "http://aitheros-media-forge:8200" if _DOCKER else "http://localhost:8200",
).rstrip("/")

_T_DISPATCH = 60     # the route returns a JOB; it must not block on the render
_T_POLL = 30

#: Quality presets media-forge accepts on both 3D routes. Sent verbatim; an unknown
#: value is refused HERE rather than 30s into a render on the far side.
_QUALITY = ("fast", "balanced", "high")

#: What an operator can actually do when nothing is placed. Kept as data so the
#: refusal names real lanes instead of saying "not available" — the failure mode the
#: rest of this pack's docstrings are written against.
#:
#: 🚨 EVERY LANE CARRIES ITS MEASURED HEADROOM AND THE DATE IT WAS MEASURED. An
#: undated capacity claim is the thing that rots fastest in this repo, and here it rots
#: into an instruction: an earlier draft of this list said the DGX Spark was "preferred
#: when the local card is committed", which was written from its spec (large unified
#: memory) and not from a probe. Measured 2026-08-24 the DGX had **1 GB available of
#: 121 GB** -- fully committed to the serving stack -- and its own
#: `aither-comfyui-3d-dgx` container sits at **Exit (137)**, i.e. it has ALREADY been
#: OOM-killed there once. Sending someone to that box would have reproduced the exact
#: incident this tool refuses to cause on the 5090.
#:
#: Re-measure before trusting any number here; the point is that a stale number is
#: visible as stale, not that these particular ones stay true.
_PLACEMENT_LANES = [
    "comfyui-3d (:8289) — the canonical engine; compose profile `creative-full`. "
    "Needs ~6 GB VRAM for shape, ~16 GB for shape+texture. "
    "MEASURED 2026-08-24: the 5090 had 4,792 MiB free of 32,607 — below even the "
    "shape-only floor, so this lane does not fit today.",
    "aitheros-hunyuan3d (:8290) — standalone Tencent image→3D, profiles "
    "`creative-full` / `dgx-hybrid`. Same card, same arithmetic as above.",
    "DGX Spark (`dgx-hybrid`, aither-comfyui-3d-dgx) — the image is BUILT and the "
    "container already exists, so placing it is a `docker start`, not a deploy. "
    "MEASURED 2026-08-24: 1 GB available of 121 GB unified, and that container is at "
    "Exit (137) from a previous OOM — starting it now would evict live inference.",
    "burst — `mediaforge_burst_up` rents capacity for a heavy one-off and "
    "`mediaforge_burst_down` returns it. With both local lanes measured full, this is "
    "the only one that adds capacity rather than taking it from something else. "
    "It SPENDS MONEY, so it is an operator decision, never an automatic fallback.",
]


def _err(msg: str, **extra) -> dict:
    out = {"error": msg}
    out.update(extra)
    return out


def _post(path: str, body: dict) -> dict:
    try:
        r = requests.post(f"{_BASE}{path}", json=body, timeout=_T_DISPATCH)
        r.raise_for_status()
        data = r.json()
    except requests.Timeout:
        return _err(f"timed out after {_T_DISPATCH}s dispatching {path}",
                    hint="this route returns a JOB, so a timeout here means media-forge "
                         "itself is unresponsive, not that the render is slow")
    except requests.RequestException as e:
        return _err(f"{type(e).__name__}: {e}", engine=_BASE,
                    hint="media-forge is a HOST process; from a container it is reached "
                         "through AITHER_MEDIAFORGE_URL, not by container name")
    if not isinstance(data, dict):
        return _err(f"{path} returned {type(data).__name__}, expected an object")
    return data


def _annotate_backend(res: dict) -> dict:
    """Attach placement lanes to a backend-absence error, and only to that.

    Deliberately narrow: blanketing every error with 'here is how to deploy a GPU
    service' would bury the real message on a bad media_id or a blocked prompt.
    """
    err = str(res.get("error", "")).lower()
    if not err:
        return res
    absent = any(t in err for t in (
        "connection", "refused", "not found", "unavailable", "no such host",
        "name or service", "timed out", "comfyui", "vn module"))
    if absent:
        res["backend_absent"] = True
        res["place_a_backend"] = list(_PLACEMENT_LANES)
        res["note"] = (
            "No 3D backend answered. This is expected on the reference box: both 3D "
            "services sit behind non-default compose profiles, and the local card does "
            "not have the headroom to start one on demand."
        )
    return res


def _validate(prompt: str, media_id: int, quality: str):
    """Argument refusals, separated from dispatch so the self-test can prove them
    WITHOUT posting a job.

    That separation is the point: a self-test that reaches the engine would dispatch a
    real render every time it ran, and would also be silently untestable on any machine
    where media-forge is not up -- so it would end up deleted or, worse, kept as an arm
    that passes because the request failed for an unrelated reason.
    """
    if bool(media_id) == bool(prompt.strip()):
        return _err(
            "give exactly one of `media_id` (an image already in the gallery) or "
            "`prompt` (text → image → 3D)",
            got={"media_id": media_id, "prompt": prompt[:80]},
        )
    if quality not in _QUALITY:
        return _err(f"quality must be one of {list(_QUALITY)}, got {quality!r}")
    return None


def _annotate_jobs(res: dict) -> dict:
    """Annotate the failure where it actually lands: inside the JOB.

    🚨 Found by a live probe, and it is the whole point of this tool. Dispatch
    SUCCEEDS -- media-forge accepts the request and returns a job envelope -- and the
    backend refusal only appears later, nested in the job record:

        {"jobs": [{"id": "make3d-1", "status": "error",
                   "error": "URLError: ... target machine actively refused it",
                   "result": {"note": "needs the comfyui-3d container at :8289 ..."}}]}

    So a rule that only reads a TOP-LEVEL `error` annotates the rare path (media-forge
    itself unreachable) and misses the common one (media-forge fine, no 3D engine
    placed) -- which on this box is what always happens. The unit self-test passed
    throughout, because it handed `_annotate_backend` a synthetic top-level dict: a
    positive assertion against the REAL shape is the only thing that could catch it
    (security-review-patterns #5).
    """
    jobs = res.get("jobs")
    for job in (jobs if isinstance(jobs, list) else [res]):
        if not isinstance(job, dict):
            continue
        _annotate_backend(job)
        inner = job.get("result")
        if isinstance(inner, dict):
            # media-forge puts the actionable hint in result.note ("needs the
            # comfyui-3d container at ..."), which names the engine when the error
            # string alone does not.
            probe = dict(inner)
            probe["error"] = f"{inner.get('error', '')} {inner.get('note', '')}".strip()
            if _annotate_backend(probe).get("backend_absent"):
                job["backend_absent"] = True
                job.setdefault("place_a_backend", list(_PLACEMENT_LANES))
    return res


def mediaforge_generate_3d(
    prompt: str = "",
    media_id: int = 0,
    textured: bool = True,
    quality: str = "balanced",
    style: str = "photoreal",
    seed: int = 0,
    timeout: int = 900,
) -> dict:
    """Generate a textured 3D model (.glb) from a gallery image or from text.

    Exactly one source, and which one you give selects the pipeline:
      * `media_id` — an image already in the media-forge gallery → `/api/studio/make3d`
        (Hunyuan3D shape → texture → GLB). This is the accurate path: the geometry is
        derived from a picture you have already looked at.
      * `prompt` — text → image → 3D in one shot → `/api/studio/txt2img3d`. Convenient,
        and it silently depends on a txt2img backend as well as the 3D one, so it has
        two ways to be unplaced instead of one.

    Returns a JOB, not a mesh: `{job_id, ...}`. Poll `mediaforge_3d_status(job_id)`.
    A render is minutes; media-forge's own default deadline on both routes is 900s.

    On an absent backend the result carries `backend_absent: true` and
    `place_a_backend` — a list of the lanes that would actually place one. That is a
    real answer, not a failure to try.
    """
    bad = _validate(prompt, media_id, quality)
    if bad is not None:
        return bad

    if media_id:
        body = {"media_id": int(media_id), "textured": bool(textured),
                "quality": quality, "timeout": int(timeout)}
        if seed:
            body["seed"] = int(seed)
        res = _post("/api/studio/make3d", body)
    else:
        body = {"prompt": prompt, "style": style, "textured": bool(textured),
                "quality": quality, "timeout": int(timeout)}
        if seed:
            body["seed"] = int(seed)
        res = _post("/api/studio/txt2img3d", body)

    res = _annotate_backend(res)
    if "error" not in res:
        res.setdefault("poll_with", "mediaforge_3d_status")
        res.setdefault("model_format", "glb")
    return res


def mediaforge_3d_status(job_id: str = "") -> dict:
    """Progress of one 3D job, or every tracked job when `job_id` is omitted.

    media-forge tracks these on its own job plane (`/api/jobs`), the same one the
    desktop app reads — so a mesh started from the UI and one started by an agent are
    visible to both. That is why this polls media-forge rather than ComfyUI's
    `/history/{prompt_id}`: the ComfyUI id is an implementation detail of one stage of
    a multi-stage pipeline (shape, then texture, then GLB finalize), and watching it
    would report "done" at the end of the first stage.
    """
    path = f"/api/jobs/{job_id}" if job_id else "/api/jobs"
    try:
        r = requests.get(f"{_BASE}{path}", timeout=_T_POLL)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        return _annotate_backend(_err(f"{type(e).__name__}: {e}", engine=_BASE))
    if not isinstance(data, dict):
        return _err(f"{path} returned {type(data).__name__}, expected an object")
    # A failed JOB is the common shape here, not a failed request. See _annotate_jobs.
    return _annotate_jobs(data)


def mediaforge_3d_backends() -> dict:
    """Is there anywhere to run a 3D render right now — and if not, what would fix it.

    Asks media-forge, because media-forge is what the render actually goes through; a
    probe of :8289 from wherever the agent happens to run answers a different question
    (whether THIS host can see the port) and has been wrong in both directions.
    """
    try:
        r = requests.get(f"{_BASE}/api/studio/models3d", timeout=_T_POLL)
        if r.status_code == 404:
            # media-forge does not publish a 3D capability probe. Say so, rather than
            # inferring readiness from a 404 — "the probe is missing" and "no backend"
            # are different facts and only one of them is about the backend.
            return {
                "known": False,
                "reason": "media-forge exposes no 3D capability probe (/api/studio/"
                          "models3d is 404); readiness is only observable by dispatching",
                "place_a_backend": list(_PLACEMENT_LANES),
            }
        r.raise_for_status()
        return {"known": True, "backends": r.json()}
    except requests.RequestException as e:
        return _annotate_backend(_err(f"{type(e).__name__}: {e}", engine=_BASE))


def self_test() -> int:
    """Prove the refusals fire, and that they do not fire on valid input.

    No engine, no network: every arm below is decided before a request is made, which
    is exactly the property that makes them worth pinning — an argument error that
    only surfaces 30s into a render on the far side is the expensive kind.
    """
    fails = []

    if _validate("a goblin", 7, "balanced") is None:
        fails.append("accepted BOTH media_id and prompt")

    if _validate("", 0, "balanced") is None:
        fails.append("accepted NEITHER media_id nor prompt")

    if _validate("", 0, "balanced") is None:
        fails.append("accepted an empty request")

    bad_q = _validate("", 7, "ultra")
    if bad_q is None or "quality" not in bad_q["error"]:
        fails.append("accepted an unknown quality preset")

    # ...and must NOT refuse valid shapes. Without this half, a tool that refuses
    # everything passes every refusal test while being completely inert -- the
    # silent-no-op class, which is exactly what this pack shipped before it bound
    # any tools at all.
    for label, args in (("media_id", ("", 7, "balanced")),
                        ("prompt", ("a goblin", 0, "high"))):
        if _validate(*args) is not None:
            fails.append(f"CRIED WOLF on a valid {label} call")

    marked = _annotate_backend({"error": "ConnectionError: connection refused"})
    if not marked.get("backend_absent") or not marked.get("place_a_backend"):
        fails.append("a connection failure was not reported as an absent backend")

    plain = _annotate_backend({"error": "no gallery image for media_id=99"})
    if plain.get("backend_absent"):
        fails.append("a bad media_id was mislabelled as an absent backend")

    # The shape a LIVE poll actually returns, captured from media-forge on 2026-08-24.
    # Dispatch succeeded; the backend refusal is nested in the job. A version that only
    # read a top-level `error` passed every other arm here and annotated nothing on the
    # one path this box actually takes.
    live = _annotate_jobs({"jobs": [{
        "id": "make3d-1", "kind": "make3d", "status": "error",
        "error": "URLError: <urlopen error [WinError 10061] No connection could be "
                 "made because the target machine actively refused it>",
        "result": {"error": "URLError: <urlopen error [WinError 10061] ...>",
                   "note": "needs the comfyui-3d container at http://localhost:8289 "
                           "(Hunyuan3D-2.1). freed_image_vram=False",
                   "ids": [], "images": []},
    }]})
    if not live["jobs"][0].get("backend_absent"):
        fails.append("a FAILED JOB carrying a backend refusal was not annotated "
                     "-- the common live path")
    if not live["jobs"][0].get("place_a_backend"):
        fails.append("an annotated job carried no placement lanes")

    # ...and a job that failed for an ordinary reason must stay unannotated, or the
    # annotation means nothing.
    ok_job = _annotate_jobs({"jobs": [{
        "id": "make3d-2", "status": "error",
        "error": "no gallery image for media_id=99",
        "result": {"error": "no gallery image for media_id=99", "note": ""},
    }]})
    if ok_job["jobs"][0].get("backend_absent"):
        fails.append("a bad media_id JOB was mislabelled as an absent backend")

    # A running job must not be annotated at all.
    running = _annotate_jobs({"jobs": [{"id": "make3d-3", "status": "running",
                                        "result": {}}]})
    if running["jobs"][0].get("backend_absent"):
        fails.append("a RUNNING job was labelled as an absent backend")

    # ── and the WIRING, which is a separate question from the helper ──────────
    # Every arm above drives _annotate_jobs directly. That proves the helper works and
    # says NOTHING about whether mediaforge_3d_status calls it -- measured: deleting
    # the call from the poll left this self-test passing. A test that exercises a
    # helper instead of the code path is the shape that let the ratchet's propose/
    # apply/keep/revert closures go unexecuted by 33 tests (AVO001-AVO005).
    # So drive the real function, with the transport stubbed to the captured payload.
    class _StubResp:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"jobs": [{
                "id": "make3d-1", "status": "error",
                "error": "URLError: <urlopen error [WinError 10061] No connection "
                         "could be made because the target machine actively refused it>",
                "result": {"error": "URLError: <urlopen error [WinError 10061] ...>",
                           "note": "needs the comfyui-3d container at "
                                   "http://localhost:8289 (Hunyuan3D-2.1).",
                           "ids": [], "images": []},
            }]}

    _real_get = requests.get
    try:
        requests.get = lambda *a, **k: _StubResp()      # noqa: E731
        wired = mediaforge_3d_status()
    finally:
        requests.get = _real_get
    if not wired.get("jobs", [{}])[0].get("backend_absent"):
        fails.append("mediaforge_3d_status did not annotate a failed job -- the poll "
                     "is not wired to _annotate_jobs, however well the helper works")

    for f in fails:
        print(f"SELF-TEST FAIL: {f}")
    if fails:
        return 1
    print("self-test OK — argument refusals fire, valid shapes pass, and only a real "
          "backend absence is annotated with placement lanes")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(self_test())
