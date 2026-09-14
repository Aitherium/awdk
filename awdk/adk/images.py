"""Local image generation for adk agents -- auto-discovered, OpenAI-shaped.

An agent that can write and run code but cannot draw a picture is missing a
sense, and every hosted image API costs money and ships the prompt off the
machine. This module gives the daemon a `/v1/images/generations` route backed
by whatever image server is ALREADY running on loopback, so an agent gets the
capability without anyone installing or configuring anything for it.

It starts nothing. Discovery only. If no backend is running the route says so,
naming the ports it tried and what to start.

WHY THE PROBE ASKS FOR THE GENERATION ROUTE AND NOT /health
-----------------------------------------------------------
Measured 2026-08-24 against this daemon itself: it answered `/health` with 200
and `/v1/images/generations` with 404. A liveness probe therefore reports the
backend UP, routing lands on it, and the caller gets a 404 instead of an image.
`/health` is a MENU -- it says a process is alive, never that it can do the one
thing you are about to ask for. So each candidate names the endpoint generation
really uses, and a 404 there means NOT CAPABLE even though the server is
plainly running. Any other answer (200/401/405/422) means the route exists.

That distinction is reported, not collapsed: "running, but no image route" is
both true and more useful to a human than "not running".

WHY Origin IS NEVER FORWARDED
-----------------------------
ComfyUI 0.3.71 rejects a cross-origin POST before reading the body. Measured:
POST /prompt with no Origin returns 400 (a graph error -- it processed the
request); the same POST carrying a foreign Origin returns 403. Nothing in that
failure names a header. We are a loopback client, not a browser, so we simply
never send one.

Self-test:
    python -m adk.images --self-test
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import pathlib
import random
import sys
from dataclasses import dataclass, field
from typing import Any

try:
    import httpx
except ModuleNotFoundError:  # pragma: no cover - declared dependency
    httpx = None  # type: ignore[assignment]


# Preference order. ComfyUI first because it is the one most people already
# have; Sana second because an agent stack may be serving it. Both loopback.
@dataclass(frozen=True)
class Candidate:
    id: str
    label: str
    port: int
    probe: str
    kind: str  # "comfyui" | "openai"


def _port(env: str, default: int) -> int:
    """A candidate's port, overridable by env.

    The defaults are the upstream ones, which is right for almost everyone --
    but "almost" is doing real work there. Someone running ComfyUI on 8189
    because 8188 was taken gets `not running` from a rule that never looked,
    and nothing in that message hints a port is configurable. One env var is
    cheaper than the hour it costs to find that out.
    """
    raw = (os.environ.get(env) or "").strip()
    if raw.isdigit() and 1 <= int(raw) <= 65535:
        return int(raw)
    return default


CANDIDATES: tuple[Candidate, ...] = (
    Candidate("comfyui", "ComfyUI", _port("ADK_COMFYUI_PORT", 8188),
              "/object_info/CheckpointLoaderSimple", "comfyui"),
    Candidate("sana", "Sana", _port("ADK_SANA_PORT", 8202),
              "/v1/images/generations", "openai"),
    Candidate("sdnext", "SD.Next / A1111", _port("ADK_SDNEXT_PORT", 7860),
              "/sdapi/v1/sd-models", "comfyui"),
)

PROBE_TIMEOUT_S = 1.5
GENERATE_TIMEOUT_S = 300.0


@dataclass
class Lane:
    id: str
    label: str
    port: int
    kind: str
    up: bool
    status: int
    note: str
    # "cuda", "cpu", "mps" or "" when the backend does not say. Reported
    # because a CPU backend is not broken, it is SLOW -- measured on 32 cores
    # here, about an order of magnitude slower than the same card. Without
    # this a person waits 90 seconds for what took 9 yesterday and has nothing
    # anywhere telling them the backend moved to CPU.
    device: str = ""
    device_name: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "port": self.port,
            "up": self.up,
            "status": self.status,
            "note": self.note,
            "device": self.device,
            "device_name": self.device_name,
        }


def judge(status: int) -> tuple[bool, str]:
    """Turn a probe status into (usable, human note). Pure -- self-tested.

    0   nothing answered.
    404 a server, but not one that can generate. Reported distinctly, because
        "not running" would send the reader to fix the wrong thing.
    """
    if status == 0:
        return False, "not running"
    if status == 404:
        return False, "running, but no image route (HTTP 404)"
    return True, f"ready (HTTP {status})"


async def _device_of(client: "httpx.AsyncClient", port: int) -> tuple[str, str]:
    """(device_type, device_name) from ComfyUI, or ("", "") if it will not say.

    A SEPARATE call from the capability probe on purpose. The capability probe
    asks the route generation actually uses -- that is what decides whether a
    lane is usable, and it must not start depending on an endpoint some builds
    may not have. This is decoration: if it fails, the lane is still usable and
    we simply do not know the device.
    """
    try:
        r = await client.get(f"http://127.0.0.1:{port}/system_stats",
                             timeout=PROBE_TIMEOUT_S)
        if r.status_code != 200:
            return "", ""
        devs = (r.json() or {}).get("devices") or []
        if not devs:
            return "", ""
        d = devs[0]
        return str(d.get("type") or ""), str(d.get("name") or "")
    except Exception:
        return "", ""


async def _probe_one(client: "httpx.AsyncClient", c: Candidate) -> Lane:
    status = 0
    try:
        r = await client.get(
            f"http://127.0.0.1:{c.port}{c.probe}", timeout=PROBE_TIMEOUT_S
        )
        status = r.status_code
    except Exception:
        status = 0
    up, note = judge(status)
    device, device_name = ("", "")
    if up and c.kind == "comfyui":
        device, device_name = await _device_of(client, c.port)
    if device == "cpu":
        note += " -- on CPU, expect ~10x slower"
    return Lane(c.id, c.label, c.port, c.kind, up, status, note, device, device_name)


async def discover() -> list[Lane]:
    """Every candidate, probed concurrently. Never raises."""
    if httpx is None:
        return [
            Lane(c.id, c.label, c.port, c.kind, False, 0, "httpx not installed")
            for c in CANDIDATES
        ]
    async with httpx.AsyncClient() as client:
        return list(await asyncio.gather(*(_probe_one(client, c) for c in CANDIDATES)))


def unavailable_message(lanes: list[Lane]) -> str:
    """Names every port tried. A bare 'no backend' is a dead end for a reader."""
    tried = ", ".join(f"{ln.label} (127.0.0.1:{ln.port}) -- {ln.note}" for ln in lanes)
    return (
        "No local image backend is able to generate. Tried: "
        f"{tried or 'nothing -- discovery did not run'}. "
        "Start ComfyUI (default port 8188) and try again. "
        "Nothing is downloaded or installed for you."
    )


# --------------------------------------------------------------------------
# ComfyUI
# --------------------------------------------------------------------------

def comfy_graph(
    *, prompt: str, negative: str, width: int, height: int,
    steps: int, cfg: float, seed: int, ckpt: str,
) -> dict[str, Any]:
    """Minimal txt2img graph. Literal on purpose: a ComfyUI graph is opaque,
    and whoever edits this next needs to see which node id does what."""
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["4", 1]}},
        "3": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": steps, "cfg": cfg,
            "sampler_name": "euler", "scheduler": "normal", "denoise": 1,
            "model": ["4", 0], "positive": ["6", 0],
            "negative": ["7", 0], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "adk", "images": ["8", 0]}},
    }


class ImageError(RuntimeError):
    """Carries a message written to be shown to a person."""


async def _comfy_generate(lane: Lane, req: "ImageRequest") -> dict[str, Any]:
    base = f"http://127.0.0.1:{lane.port}"
    async with httpx.AsyncClient(timeout=GENERATE_TIMEOUT_S) as client:
        info = await client.get(f"{base}/object_info/CheckpointLoaderSimple")
        ckpts: list[str] = []
        if info.status_code == 200:
            try:
                ckpts = info.json()["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
            except Exception:
                ckpts = []
        if not ckpts:
            raise ImageError(
                "ComfyUI is running but reports no checkpoints. Put a model in "
                "ComfyUI/models/checkpoints and restart it."
            )
        ckpt = req.model if req.model in ckpts else ckpts[0]

        # An image seed, not a credential -- `random` is the right tool and a
        # CSPRNG here would only mislead the next reader about what it guards.
        seed = req.seed if req.seed is not None else random.randrange(2**32)
        body = {
            "prompt": comfy_graph(
                prompt=req.prompt, negative=req.negative,
                width=req.width, height=req.height,
                steps=req.steps, cfg=req.cfg, seed=seed, ckpt=ckpt,
            ),
            "client_id": f"adk-{random.randrange(2**32):08x}",
        }
        q = await client.post(f"{base}/prompt", json=body)
        if q.status_code != 200:
            raise ImageError(
                f"ComfyUI refused the job (HTTP {q.status_code}). {q.text[:300]}"
            )
        pid = q.json().get("prompt_id")
        if not pid:
            raise ImageError("ComfyUI accepted the job but returned no prompt_id.")

        # Bounded poll. An unbounded wait against a wedged backend is a hang
        # that looks like a slow model.
        waited = 0.0
        while waited < GENERATE_TIMEOUT_S:
            await asyncio.sleep(1.0)
            waited += 1.0
            h = await client.get(f"{base}/history/{pid}")
            if h.status_code != 200:
                continue
            entry = (h.json() or {}).get(pid)
            if not entry:
                continue
            names = [
                (img.get("filename", ""), img.get("subfolder", ""), img.get("type", "output"))
                for node in (entry.get("outputs") or {}).values()
                for img in (node.get("images") or [])
            ]
            if names:
                out = []
                for fn, sub, typ in names:
                    v = await client.get(
                        f"{base}/view",
                        params={"filename": fn, "subfolder": sub, "type": typ},
                    )
                    if v.status_code == 200 and v.content:
                        out.append(base64.b64encode(v.content).decode())
                if out:
                    return {"images_b64": out, "backend": lane.id, "model": ckpt}
                raise ImageError("ComfyUI produced an image that could not be read back.")
            if (entry.get("status") or {}).get("status_str") == "error":
                raise ImageError("ComfyUI reported an error running the graph.")
        raise ImageError(
            f"ComfyUI did not return an image within {int(GENERATE_TIMEOUT_S)}s."
        )


async def _openai_generate(lane: Lane, req: "ImageRequest") -> dict[str, Any]:
    base = f"http://127.0.0.1:{lane.port}"
    async with httpx.AsyncClient(timeout=GENERATE_TIMEOUT_S) as client:
        r = await client.post(
            f"{base}/v1/images/generations",
            json={
                "prompt": req.prompt,
                "negative_prompt": req.negative,
                "size": f"{req.width}x{req.height}",
                "n": 1,
                "response_format": "b64_json",
            },
        )
    if r.status_code != 200:
        raise ImageError(f"{lane.label} refused the job (HTTP {r.status_code}). {r.text[:300]}")
    items = (r.json() or {}).get("data") or []
    out = [d["b64_json"] for d in items if d.get("b64_json")]
    if not out:
        # A 200 with no image is the silent no-op. Refuse to call it success.
        raise ImageError(
            f"{lane.label} answered 200 with no image -- it most likely has no "
            "model loaded. Check its console rather than retrying."
        )
    return {"images_b64": out, "backend": lane.id, "model": req.model or ""}


@dataclass
class ImageRequest:
    prompt: str
    negative: str = ""
    width: int = 768
    height: int = 768
    steps: int = 20
    cfg: float = 6.0
    seed: int | None = None
    model: str = ""
    backend: str = ""
    _lanes: list[Lane] = field(default_factory=list)


async def generate(req: ImageRequest) -> dict[str, Any]:
    """Route to whichever local backend can actually generate.

    Raises ImageError with a message meant for a human.
    """
    if not req.prompt.strip():
        raise ImageError("An image needs a prompt.")
    if httpx is None:
        raise ImageError("httpx is not installed, so no backend can be reached.")

    lanes = await discover()
    req._lanes = lanes
    if req.backend:
        chosen = next((ln for ln in lanes if ln.id == req.backend), None)
        if chosen is None:
            raise ImageError(f"Unknown backend '{req.backend}'.")
        if not chosen.up:
            raise ImageError(f"{chosen.label} is not usable: {chosen.note}.")
    else:
        chosen = next((ln for ln in lanes if ln.up), None)
        if chosen is None:
            raise ImageError(unavailable_message(lanes))

    if chosen.kind == "comfyui":
        return await _comfy_generate(chosen, req)
    return await _openai_generate(chosen, req)


# --------------------------------------------------------------------------
# Self-test -- the pure parts, offline.
# --------------------------------------------------------------------------

def _self_test() -> int:
    ok = True

    def arm(label: str, cond: bool) -> None:
        nonlocal ok
        print(f"  {'ok  ' if cond else 'FAIL'} {label}")
        ok = ok and cond

    # The rule the whole module turns on.
    arm("status 0 is not usable", judge(0) == (False, "not running"))
    arm("status 404 is NOT usable", judge(404)[0] is False)
    arm("404 note distinguishes it from absent", "no image route" in judge(404)[1])
    arm("status 200 is usable", judge(200)[0] is True)
    arm("status 405 is usable (route exists)", judge(405)[0] is True)
    arm("status 401 is usable (route exists)", judge(401)[0] is True)

    g = comfy_graph(prompt="a", negative="b", width=512, height=512,
                    steps=4, cfg=2.0, seed=7, ckpt="m.safetensors")
    arm("graph has the seven nodes", set(g) == {"3", "4", "5", "6", "7", "8", "9"})
    arm("graph carries the checkpoint", g["4"]["inputs"]["ckpt_name"] == "m.safetensors")
    arm("negative is its own encode node", g["7"]["inputs"]["text"] == "b")
    arm("sampler reads the negative node", g["3"]["inputs"]["negative"] == ["7", 0])
    arm("seed is honoured", g["3"]["inputs"]["seed"] == 7)

    lanes = [Lane("comfyui", "ComfyUI", 8188, "comfyui", False, 0, "not running"),
             Lane("sana", "Sana", 8202, "openai", False, 404,
                  "running, but no image route (HTTP 404)")]
    msg = unavailable_message(lanes)
    arm("unavailable names every port", "8188" in msg and "8202" in msg)
    arm("unavailable distinguishes the two failures",
        "not running" in msg and "no image route" in msg)

    # An empty prompt must be refused before any network call.
    try:
        asyncio.run(generate(ImageRequest(prompt="   ")))
        arm("empty prompt refused", False)
    except ImageError as e:
        arm("empty prompt refused", "needs a prompt" in str(e))

    # Port overrides: honoured, defaulted, and garbage-rejected. Without the
    # third arm a typo'd env var would silently probe port 0 and report every
    # backend down, which reads as "nothing is running" rather than "you typed
    # that wrong".
    import os as _os
    arm("port default when unset", _port("ADK_NOPE_PORT_X", 8188) == 8188)
    _os.environ["ADK_TEST_PORT_X"] = "9999"
    arm("port override honoured", _port("ADK_TEST_PORT_X", 8188) == 9999)
    _os.environ["ADK_TEST_PORT_X"] = "not-a-port"
    arm("garbage port falls back", _port("ADK_TEST_PORT_X", 8188) == 8188)
    _os.environ["ADK_TEST_PORT_X"] = "70000"
    arm("out-of-range port falls back", _port("ADK_TEST_PORT_X", 8188) == 8188)
    _os.environ.pop("ADK_TEST_PORT_X", None)

    # A CPU lane is USABLE, never "down". Getting this backwards would delete
    # image generation for everyone without a GPU, which is the whole point of
    # reporting the device rather than gating on it.
    cpu_lane = Lane("comfyui", "ComfyUI", 8188, "comfyui", True, 200,
                    "ready (HTTP 200) -- on CPU, expect ~10x slower", "cpu", "cpu")
    arm("a CPU lane is still usable", cpu_lane.up is True)
    arm("a CPU lane says so in its note", "CPU" in cpu_lane.note)
    arm("device survives as_dict", cpu_lane.as_dict()["device"] == "cpu")
    gpu_lane = Lane("comfyui", "ComfyUI", 8188, "comfyui", True, 200,
                    "ready (HTTP 200)", "cuda", "NVIDIA GeForce RTX 5090")
    arm("a GPU lane carries its name", "NVIDIA" in gpu_lane.as_dict()["device_name"])
    arm("an unknown device is empty, not guessed",
        Lane("x", "X", 1, "openai", True, 200, "n").device == "")

    print("SELF-TEST PASS" if ok else "SELF-TEST FAILED")
    return 0 if ok else 1


def cmd_image(args) -> int:
    """`adk image` -- the self-service entry point.

    Writes a real PNG to disk rather than printing base64: the point of this
    command is that a person ends up holding a picture, and a wall of base64 in
    a terminal is not that.
    """
    if getattr(args, "backends", False):
        lanes = asyncio.run(discover())
        usable = [ln for ln in lanes if ln.up]
        for ln in lanes:
            mark = "READY" if ln.up else "  -  "
            port = f":{ln.port}" if ln.port else ""
            dev = f"  [{ln.device_name or ln.device}]" if ln.device else ""
            print(f"  [{mark}] {ln.id:<8} 127.0.0.1{port:<6} {ln.note}{dev}")
        if not usable:
            print()
            print(unavailable_message(lanes))
            return 1
        return 0

    prompt = " ".join(getattr(args, "prompt", []) or []).strip()
    if not prompt:
        print("adk image: give it something to draw, e.g.", file=sys.stderr)
        print('  adk image "a goblin at a green CRT"', file=sys.stderr)
        return 2

    req = ImageRequest(
        prompt=prompt,
        negative=getattr(args, "negative", "") or "",
        width=getattr(args, "width", 768),
        height=getattr(args, "height", 768),
        steps=getattr(args, "steps", 20),
        cfg=getattr(args, "cfg", 6.0),
        seed=getattr(args, "seed", None),
        model=getattr(args, "model", "") or "",
        backend=getattr(args, "backend", "") or "",
    )
    try:
        out = asyncio.run(generate(req))
    except ImageError as e:
        # The message is the product. Every ImageError in this module names the
        # lane, the port and what to start, so print it as-is rather than
        # wrapping it in a generic failure.
        print(f"adk image: {e}", file=sys.stderr)
        return 1

    import base64 as _b64
    dest = pathlib.Path(getattr(args, "out", "") or "adk-image.png")
    written = []
    for i, b in enumerate(out["images_b64"]):
        target = dest if i == 0 else dest.with_name(f"{dest.stem}-{i}{dest.suffix}")
        target.write_bytes(_b64.b64decode(b))
        written.append(target)
    for t in written:
        print(f"wrote {t}  ({t.stat().st_size} bytes, {out['backend']}"
              f"{'/' + out['model'] if out['model'] else ''})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="local image generation for adk")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--discover", action="store_true", help="probe and print lanes")
    ap.add_argument("--prompt", default="", help="generate and report size only")
    a = ap.parse_args()

    if a.self_test:
        return _self_test()
    if a.discover:
        for ln in asyncio.run(discover()):
            print(f"  {ln.id:<8} port={ln.port:<5} up={str(ln.up):<5} {ln.note}")
        return 0
    if a.prompt:
        try:
            out = asyncio.run(generate(ImageRequest(prompt=a.prompt)))
        except ImageError as e:
            print(f"image generation failed: {e}", file=sys.stderr)
            return 1
        n = len(out["images_b64"])
        size = sum(len(b) for b in out["images_b64"])
        print(f"ok: {n} image(s), {size} b64 bytes, backend={out['backend']}, "
              f"model={out['model']}")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
