"""adk wm dream: exit codes, the copy-last verdict first, and the right URLs."""
from types import SimpleNamespace

import pytest

from adk.commands import wm_dream


def _args(**kw):
    base = dict(source="cns", modality="telemetry", steps=6, rows=16, action=1,
                open=False, json=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_verdict_line_comes_first_and_is_honest():
    lines = wm_dream.summarize("cns", {"model_mse": 0.5, "copy_last_mse": 0.1,
                                       "beats_copy_last": False, "modality": "telemetry",
                                       "frames": [[0.0, 1.0]]})
    assert lines[0].startswith("model error 0.5") and "does NOT beat" in lines[0]
    lines = wm_dream.summarize("prospector", {"model_err": 0.1, "copy_last_err": 0.3,
                                              "beats_copy_last": True, "rows": [],
                                              "history_bins": 3, "rollout_bins": 8,
                                              "bin_seconds": 300, "features_total": 9})
    assert "BEATS" in lines[0]


@pytest.mark.parametrize("status,body,code", [
    (0, None, 2), (404, {"detail": "x"}, 1), (503, None, 1), (500, None, 2),
    (200, {"model_mse": 1.0, "copy_last_mse": 0.0, "beats_copy_last": False,
           "modality": "telemetry", "frames": []}, 0),
])
def test_exit_codes(monkeypatch, capsys, status, body, code):
    monkeypatch.setattr(wm_dream, "fetch", lambda s, a: (status, body, "boom"))
    assert wm_dream.cmd_wm_dream(_args()) == code


def test_unknown_source_is_could_not_run():
    assert wm_dream.cmd_wm_dream(_args(source="nope")) == 2


def test_env_overrides_base(monkeypatch):
    monkeypatch.setenv("AITHER_CNS_URL", "https://cns.example:9/")
    assert wm_dream._base("prospector") == "https://cns.example:9"
    assert wm_dream.SOURCES["prospector"]["api"] == "/prospector/dream"
    assert wm_dream._params("cns", _args(modality="steps"))["modality"] == "steps"
