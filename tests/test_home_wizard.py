"""Tests for the Aither-for-Home first-run path.

Covers the two new modules without any network or hardware calls:
  * image_setup._engine_options_from_detect — pure option matrix logic
  * gui_wizard.run(mode="auto") — the headless wizard path

The full GUI (Tkinter) path is exercised separately on a machine with a
display; these tests keep CI hermetic and fast.
"""

import pytest


# ── image_setup: engine options matrix ──────────────────────────────────────


def _detect(gpu_vendor="nvidia", vram_gb=16, cpu_only=False, sdxl=False):
    return {
        "system_info": {
            "gpu_vendor": gpu_vendor,
            "gpu_name": "Test GPU",
            "gpu_vram_mb": int(vram_gb * 1024),
        },
        "capability": {
            # Apple unified memory reports sdxl_1024 (metal-comfyui), matching
            # image_bootstrap's real detect output for that vendor.
            "sdxl_1024": sdxl or vram_gb >= 12 or gpu_vendor == "apple",
            "sdxl_lowvram": (not sdxl) and vram_gb >= 6 and vram_gb < 12,
            "cpu_only": cpu_only,
        },
        "recommended_recipe": "cuda-comfyui-12gb",
    }


@pytest.mark.parametrize(
    "detect,expected_ids",
    [
        (_detect(gpu_vendor="nvidia", vram_gb=16),
         {"comfyui", "sana", "bonsai"}),
        (_detect(gpu_vendor="nvidia", vram_gb=6, sdxl=False),
         {"comfyui"}),  # below Sana/Bonsai 8GB floor
        (_detect(gpu_vendor="none", vram_gb=0, cpu_only=True),
         {"comfyui"}),  # CPU ComfyUI
        (_detect(gpu_vendor="apple", vram_gb=0),
         {"comfyui"}),  # Metal ComfyUI
        (_detect(gpu_vendor="none", vram_gb=0, cpu_only=False),
         set()),  # nothing runnable here: no option at all, never a phantom one
    ],
)
def test_engine_options_from_detect(monkeypatch, detect, expected_ids):
    from adk.shell import image_setup

    opts = image_setup._engine_options_from_detect(detect)
    ids = {o["id"] for o in opts}
    assert ids == expected_ids
    # Every offered option installs a real engine on this computer.
    assert all(o["recipe_id"] for o in opts if o["id"] != "comfyui")


def test_bonsai_browser_is_not_offered_and_says_why():
    """No in-browser image engine exists, so the wizard must not offer one; an old saved
    choice gets a sentence saying it is not available rather than a silent no-op."""
    from adk.shell import image_setup

    for detect in (_detect(), _detect(gpu_vendor="none", vram_gb=0, cpu_only=True)):
        ids = {o["id"] for o in image_setup._engine_options_from_detect(detect)}
        assert "bonsai-browser" not in ids
    msg = image_setup.UNAVAILABLE_ENGINES["bonsai-browser"]
    assert "not available" in msg
    assert "credits" in msg


def test_image_studio_status_shape(monkeypatch):
    """image_studio_status returns the capability contract even when degraded."""
    from adk.shell import image_setup

    def fake_detect():
        return {"error": "boom", "fix": "none"}

    monkeypatch.setattr(image_setup._engine(), "imagegen_detect_hardware", fake_detect)
    status = image_setup.image_studio_status()
    assert status["capable"] is False
    assert status["engine_options"] == []
    assert status["errors"]


# ── gui_wizard: headless run path ───────────────────────────────────────────


def test_gui_wizard_headless_run(monkeypatch):
    """run(mode='auto') executes the steps and returns a usable dict."""
    from adk.shell import gui_wizard as gw

    def fake_detect(state):
        state.hardware = {
            "capable": True, "gpu_name": "RTX 5090",
            "recommended_recipe": "cuda-comfyui-24gb",
            "engine_options": [
                {"id": "comfyui", "name": "Full studio", "recipe_id": "cuda-comfyui-24gb",
                 "plain": "...", "requires_download_gb": 14.0},
            ],
            "plain_english": "ready", "errors": [],
        }
        state.gpu_detected = "RTX 5090"
        state.steps.append("hardware_detected")

    def fake_image(state, apply, recipe_id="", prefer_engine="auto"):
        state.image = {"status": "planned", "recipe_id": recipe_id or "auto",
                       "plain_english": "Ready to set up.", "notes": [], "errors": []}
        state.steps.append("image_studio_ready")

    monkeypatch.setattr(gw, "_detect_hardware", fake_detect)
    monkeypatch.setattr(gw, "_run_image_studio", fake_image)

    result = gw.run(mode="auto", auto_image=True)
    assert result["status"] == "success"
    assert "hardware_detected" in result["steps_completed"]
    assert "image_studio_ready" in result["steps_completed"]
    assert result["gpu_detected"] == "RTX 5090"
