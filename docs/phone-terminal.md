# awdk on a phone terminal (Aither Learn)

For a child's Pixel: the **Linux terminal** (Android 15+, a Debian arm64 VM).
Everything here is optional; the Home-screen `/learn` app is all a child needs.

## Install and sign in

```sh
sudo apt update && sudo apt install -y python3-venv
python3 -m venv ~/aw
~/aw/bin/pip install awdk
~/aw/bin/adk login          # prints a short code
```

The grown-up opens the parent console (`/learn/parent`), opens **Terminal (optional)**,
picks the child and types the code. The approval goes to
`POST /api/v1/tutor/family/learners/{lid}/approve-device`, so the token the terminal
receives belongs to the **child**, never the parent.

## Learn

```sh
~/aw/bin/adk learn          # the /learn link, hello, and whether there is time today
~/aw/bin/adk learn play     # one quest in text; q stops and saves
```

`adk learn` talks only to `https://app.aitherium.com/api/tutor/me/*` with the child's
bearer (override with `--url` or `AITHER_LEARN_URL`). The server owns grading, hints,
breaks and the daily cap. Kid rules hold in text too: no red X, no timer, no score, no
streak; a miss shows the worked steps and the same item comes back.

## Optional: a small on-device model

```sh
~/aw/bin/adk bonsai setup --device phone            # Bonsai-1.7B-Q1_0 (~250 MB), CPU
~/aw/bin/adk bonsai setup --device phone --model 4b # ~570 MB, wants ~4 GB RAM
~/aw/bin/adk bonsai status
```

It fetches the llama.cpp release's plain `ubuntu-arm64` CPU build and a Bonsai 1 Q1_0
GGUF from weights.aitherium.com, serves on `127.0.0.1:8080` with a 2k context and at
most 4 threads, and writes `~/.aither/bin/bonsai-phone.sh` to start it again. It does
not change your default backend unless you pass `--use`. If the prebuilt binary will
not run (a glibc mismatch), the command prints the build-from-source lines.

**The tutor never needs it.** Hints are canned, answer-free lines when no model
answers (or when the server sets `AITHER_TUTOR_LLM=off`); a model only rephrases them.

## What is verified and what is not

| Claim | Status |
|---|---|
| Every base `awdk` dependency has a `manylinux` aarch64 wheel for CPython 3.11 and 3.13; none is torch/CUDA/numpy | verified (`pip download --platform manylinux2014_aarch64 --only-binary=:all:`) |
| `adk learn --help`, `adk login --help`, `adk bonsai setup --help` import no torch/CUDA/numpy | verified (`tests/test_learn_cli.py`) |
| `adk learn play` speaks the `/me/*` contract | verified against a scripted server (`tests/test_learn_cli.py`) |
| The llama.cpp asset picker chooses `ubuntu-arm64` on an arm64 CPU | verified (unit test on the live release names) |
| Tutor hints with no LLM, a missing gateway or a hung gateway | verified (`AitherOS/dev/tests/test_tutor_safety.py`) |
| Install, `adk login`, `adk learn play` on a real Pixel | **not run on hardware** |
| The `ubuntu-arm64` llama.cpp build runs on the Pixel VM's glibc; Bonsai Q1_0 speed on a phone CPU | **not run on hardware** |
| Termux | **not tested** (no prebuilt wheels for Android; use the Linux terminal) |
