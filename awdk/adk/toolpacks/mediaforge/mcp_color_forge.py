"""
MCP Color Forge — professional OCIO/ACES color management over media-forge (:8200).

Agent-callable surface for the ComfyUI-OCIO pack (wizzense/ComfyUI-OCIO, baked into
aitheros-comfyui and installed on burst nodes): colorspace conversion, ASC CDL grading,
LUT application and display rendering, plus the ComfyUI-Agent-Kit knowledge lookup.

The pipeline discipline (see .claude/skills/color-pipeline/SKILL.md):
  * work in ACEScg, deliver through a display transform — grade AFTER converting in,
    display_render LAST (never grade display-referred sRGB and call it mastered).
  * colorspace names come from the LIVE config — call mediaforge_color_info first; names
    fuzzy-match (exact → case-insensitive → unique substring) and a wrong name returns
    the near matches instead of guessing.
  * LUT files live under the render plane's ComfyUI *input* dir (host D:\\ComfyUI\\input),
    referenced relative, e.g. "luts/film.cube" — media-forge is a host process but the
    render plane is a container; only that mount is visible to it.
  * transforms are CPU-side OCIO processors — cheap, safe while the GPU is busy.

Pattern follows mcp_character_forge.py — synchronous requests, JSON returns,
GateBusy-aware POST (color ops are "cheap" class but share the op pipeline).
"""

import os
import time

import requests

_DOCKER = os.getenv("AITHER_DOCKER_MODE") == "true"
_BASE = os.getenv(
    "AITHER_MEDIAFORGE_URL",
    "http://aitheros-media-forge:8200" if _DOCKER else "http://localhost:8200",
).rstrip("/")

_T_FAST = 60      # probes / knowledge lookup
_T_OP = 300       # a color transform: upload + CPU OCIO + fetch — seconds, not minutes


def _post(path: str, body: dict, timeout: int, tries: int = 3) -> dict:
    """POST with GateBusy backoff. media-forge signals errors as 200 + ok:false."""
    for attempt in range(tries):
        try:
            r = requests.post(f"{_BASE}{path}", json=body, timeout=timeout)
            r.raise_for_status()
            data = r.json()
        except requests.Timeout:
            return {"error": f"timed out after {timeout}s calling {path}"}
        except requests.RequestException as e:
            return {"error": f"{type(e).__name__}: {e}"}
        if isinstance(data, dict) and data.get("ok") is False:
            err = str(data.get("error", ""))
            if "busy" in err.lower() and attempt < tries - 1:
                time.sleep(15)
                continue
            return {"error": err or "rejected"}
        return data
    return {"error": "busy — exhausted retries"}


def mediaforge_color_info() -> dict:
    """The render plane's LIVE color capability: OCIO node classes present, the 55 ACES
    colorspaces, displays, views, and LUT files actually available. Call this FIRST —
    every colorspace/display/view/lut argument below must come from these lists
    (fuzzy matching helps, but the source of truth is this probe)."""
    return _post("/op/color_info", {}, timeout=_T_FAST)


def mediaforge_color_convert(media_id: int, out_colorspace: str = "ACEScg",
                        in_colorspace: str = "sRGB Encoded Rec.709 (sRGB)",
                        mix: float = 1.0) -> dict:
    """Convert a gallery image between OCIO colorspaces (ACES 2.0 built-in config).
    Typical uses: sRGB texture -> ACEScg to enter a graded pipeline; camera-log footage
    stills (S-Log3 / LogC4 / V-Log names in mediaforge_color_info) -> ACEScg to normalize.
    Registers the result in the gallery (parent = source) and returns {ok, head_ids, images}."""
    return _post("/op/color_convert",
                 {"image": int(media_id), "in_colorspace": in_colorspace,
                  "out_colorspace": out_colorspace, "mix": float(mix)}, timeout=_T_OP)


def mediaforge_color_grade(media_id: int, slope: float = 1.0, offset: float = 0.0,
                      power: float = 1.0, saturation: float = 1.0,
                      slope_rgb: list = None, offset_rgb: list = None,
                      power_rgb: list = None, mix: float = 1.0) -> dict:
    """ASC CDL grade (the film-industry primary): slope=gain, offset=lift, power=gamma,
    plus saturation. Master values hit all channels; pass slope_rgb/offset_rgb/power_rgb
    as [r,g,b] for per-channel control (e.g. slope_rgb=[1.05,1.0,0.92] warms highlights).
    Grade scene-linear (ACEScg) material, then mediaforge_display_render for delivery."""
    body: dict = {"image": int(media_id), "slope": float(slope), "offset": float(offset),
                  "power": float(power), "saturation": float(saturation), "mix": float(mix)}
    for name, rgb in (("slope", slope_rgb), ("offset", offset_rgb), ("power", power_rgb)):
        if isinstance(rgb, (list, tuple)) and len(rgb) == 3:
            body[f"{name}_r"], body[f"{name}_g"], body[f"{name}_b"] = (float(v) for v in rgb)
    return _post("/op/color_grade", body, timeout=_T_OP)


def mediaforge_apply_lut(media_id: int, lut: str, interpolation: str = "tetrahedral",
                    direction: str = "forward", mix: float = 1.0) -> dict:
    """Apply a .cube/.3dl/.ccc/.cdl LUT file to a gallery image. `lut` is relative to the
    render plane's ComfyUI input dir (host D:\\ComfyUI\\input), e.g. "luts/film.cube" —
    mediaforge_color_info lists what is actually there. mix<1 blends with the source."""
    return _post("/op/apply_lut",
                 {"image": int(media_id), "lut": lut, "interpolation": interpolation,
                  "direction": direction, "mix": float(mix)}, timeout=_T_OP)


def mediaforge_display_render(media_id: int, in_colorspace: str = "ACEScg",
                         display: str = "sRGB - Display",
                         view: str = "ACES 2.0 - SDR 100 nits (Rec.709)",
                         invert: bool = False, mix: float = 1.0) -> dict:
    """The DELIVERY step: render scene-referred material through an OCIO display+view
    transform (ACES 2.0 SDR Rec.709 default; Display P3 / Rec.2100 HDR views in
    mediaforge_color_info). invert=True goes display -> scene instead."""
    return _post("/op/display_render",
                 {"image": int(media_id), "in_colorspace": in_colorspace, "display": display,
                  "view": view, "invert": bool(invert), "mix": float(mix)}, timeout=_T_OP)


def mediaforge_exr_master(media_id: int, out_colorspace: str = "ACEScg",
                     in_colorspace: str = "sRGB Encoded Rec.709 (sRGB)",
                     bit_depth: str = "16f", compression: str = "zip") -> dict:
    """Render a gallery image out as a FLOAT scene-linear EXR master (default ACEScg 16f)
    for VFX/DI handoff. The EXR lands on the render plane's output mount (returned as
    exr_path / meta.exr_path); the gallery gets a display-rendered PNG proxy with lineage.
    bit_depth 16f|32f; compression zip|zips|piz|pxr24|dwaa|dwab|rle|none."""
    return _post("/op/exr_master",
                 {"image": int(media_id), "in_colorspace": in_colorspace,
                  "out_colorspace": out_colorspace, "bit_depth": bit_depth,
                  "compression": compression}, timeout=_T_OP)


def mediaforge_ingest_exr(source: str, in_colorspace: str = "ACEScg",
                     display: str = "sRGB - Display",
                     view: str = "ACES 2.0 - SDR 100 nits (Rec.709)") -> dict:
    """Bring an EXR INTO the gallery: float read + ACES display transform -> registered PNG
    (meta.exr_source keeps the float original's path). `source` is a host path or a path
    relative to the render plane's ComfyUI input dir. in_colorspace = the space the FILE is
    in (EXR convention: ACEScg / ACES2065-1)."""
    return _post("/op/ingest_exr",
                 {"source": source, "in_colorspace": in_colorspace, "display": display,
                  "view": view}, timeout=_T_OP)


def mediaforge_video_master(media_id: int, codec: str = "prores_422hq",
                       in_colorspace: str = "", out_colorspace: str = "") -> dict:
    """Master a gallery VIDEO through OCIO: prores_4444|prores_422hq|prores_422|dnxhr_hq
    (.mov) or h264|hevc (.mp4). Default is a colorspace-passthrough transcode; give BOTH
    in_colorspace and out_colorspace for a managed conversion through ACEScg. The master
    registers in the gallery and stays on the output mount (master_path)."""
    return _post("/op/video_master",
                 {"video": int(media_id), "codec": codec, "in_colorspace": in_colorspace,
                  "out_colorspace": out_colorspace}, timeout=900)


def mediaforge_kit_lookup(query: str = "", doc: str = "auto", max_sections: int = 5) -> dict:
    """Search the ComfyUI-Agent-Kit knowledge base (per-model prompt recipes for ~71 model
    families — FLUX, SDXL, Wan, Qwen-Image, LTX... — plus node/task guides). READ a model's
    recipe BEFORE prompting it: every model is its own dialect (SDXL tags won't help FLUX).
    Empty query lists the available docs. doc: auto|models|model_index|nodes|tasks|skill|
    known_issues|example_workflows."""
    return _post("/op/kit_lookup",
                 {"query": query, "doc": doc, "max_sections": int(max_sections)},
                 timeout=_T_FAST)
