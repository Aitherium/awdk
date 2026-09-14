"""The model download must ask HuggingFace what exists, not guess and 401.

The fifth bug in the `--setup-model` chain, found the moment bugs 1-4 were
fixed and the unattended run reached the model step:

    Trying: https://huggingface.co/bartowski/nvidia_Nemotron-Orchestrator-8B-GGUF/...
    ERROR: download failed: HTTP Error 401: Unauthorized

That repo DOES NOT EXIST. HF answers 401 -- not 404 -- for a nonexistent repo
to anonymous callers (so private-repo existence is not leakable), which makes
the failure read as an auth problem and sends the user hunting for a token they
do not need. The filenames were guessed too, and real uploaders disagree:
MaziyarPanahi ships `Name.Q6_K.gguf` (dot, no Q8_0 at all), Mungert ships
`Name-f16_q8_0.gguf` (dash, lowercase). No guess list survives both.

The file lists in these tests are VERBATIM copies of the two real repos, taken
2026-08-22, so CI asserts against reality with no network.
"""

from __future__ import annotations

from adk.llamacpp_setup import (
    DEFAULT_MODEL_REPO,
    DEFAULT_MODEL_REPO_FALLBACKS,
    QUANT_FALLBACK_LADDER,
    _resolve_gguf,
)

MAZIYAR = [
    "Nemotron-Orchestrator-8B.Q2_K.gguf",
    "Nemotron-Orchestrator-8B.Q3_K_L.gguf",
    "Nemotron-Orchestrator-8B.Q3_K_M.gguf",
    "Nemotron-Orchestrator-8B.Q4_K_M.gguf",
    "Nemotron-Orchestrator-8B.Q5_K_M.gguf",
    "Nemotron-Orchestrator-8B.Q6_K.gguf",
    "Nemotron-Orchestrator-8B.fp16.gguf",
]
MUNGERT = [
    "Nemotron-Orchestrator-8B-bf16.gguf",
    "Nemotron-Orchestrator-8B-f16_q8_0.gguf",
    "Nemotron-Orchestrator-8B-imatrix.gguf",
    "Nemotron-Orchestrator-8B-q4_k_m.gguf",
]


def test_exact_quant_matches_whatever_the_uploader_spelled():
    # Dot-and-upper (MaziyarPanahi) and dash-and-lower (Mungert) both resolve.
    assert _resolve_gguf(MAZIYAR, "Q4_K_M") == (
        "Nemotron-Orchestrator-8B.Q4_K_M.gguf", "Q4_K_M")
    assert _resolve_gguf(MUNGERT, "Q4_K_M") == (
        "Nemotron-Orchestrator-8B-q4_k_m.gguf", "Q4_K_M")


def test_a_missing_quant_walks_the_ladder_and_says_which_it_took():
    # The real case from the failed run: an 86 GB box asks for Q8_0, and the
    # default repo ships none. Q6_K is the first ladder hit -- and the CALLER
    # announces the substitution, which is why the actual quant is returned
    # rather than the requested one.
    name, actual = _resolve_gguf(MAZIYAR, "Q8_0")
    assert name == "Nemotron-Orchestrator-8B.Q6_K.gguf"
    assert actual == "Q6_K"


def test_a_mixed_quant_file_still_counts_as_its_quant():
    # Mungert's f16_q8_0 IS a q8_0-class artifact; refusing it would send an
    # 86 GB box down to q4 for no reason.
    name, actual = _resolve_gguf(MUNGERT, "Q8_0")
    assert name == "Nemotron-Orchestrator-8B-f16_q8_0.gguf"
    assert actual == "Q8_0"


def test_imatrix_calibration_files_are_never_served_as_models():
    # An imatrix file matches almost any quant substring search and is not a
    # runnable model. Serving one produces a llama-server that loads garbage.
    assert _resolve_gguf(["X-imatrix.gguf"], "Q8_0") == (None, None)


def test_an_empty_repo_yields_none_not_a_crash():
    assert _resolve_gguf([], "Q8_0") == (None, None)


def test_the_default_repo_is_not_the_phantom():
    # bartowski/nvidia_Nemotron-Orchestrator-8B-GGUF answers 401 because it was
    # never published. Pinning the constant is the cheapest way to stop the
    # exact regression: someone "fixing" the default back to the famous name.
    assert "bartowski" not in DEFAULT_MODEL_REPO
    assert DEFAULT_MODEL_REPO == "MaziyarPanahi/Nemotron-Orchestrator-8B-GGUF"


def test_fallback_repos_exist_and_exclude_the_primary():
    # A fallback list that is empty, or that contains the primary, is the
    # vacuous-decoration defect: a constant that changes nothing.
    assert DEFAULT_MODEL_REPO_FALLBACKS
    assert DEFAULT_MODEL_REPO not in DEFAULT_MODEL_REPO_FALLBACKS


def test_the_ladder_descends_in_size():
    # The ladder's whole meaning is "the best quant this repo still has". If
    # someone sorts it ascending, every substitution silently picks the worst.
    order = {q: i for i, q in enumerate(QUANT_FALLBACK_LADDER)}
    assert order["Q8_0"] < order["Q6_K"] < order["Q4_K_M"]
