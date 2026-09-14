"""`adk bonsai-local` must be REACHABLE by an adk agent.

THE GAP THIS CLOSES (found 2026-07-31 while chasing "make it dead simple to install/use
Bonsai-27B with awdk agents seamlessly"):

`adk bonsai-local` starts llama.cpp on **:8090**. Every backend preset pointed at **:8201**,
which is AitherVLLMSwap — a FLEET service that does not exist on anyone else's machine. So
the one-command install worked perfectly and then no `--backend` could talk to it:

    $ adk bonsai-local          # -> healthy server on :8090
    $ adk --backend bonsai      # -> dials :8201, finds nothing

Nothing failed loudly. The container was up, `/health` returned 200, and the backend simply
could not connect — which reads as "the local model is broken", not "the two halves were
never introduced". `cmd_bonsai_local`'s own docstring asserted :8090 was "the ladder's
`local` tier", so the code claimed a wiring that did not exist, and that claim is what made
it look already-done.

The port lived as FOUR separate literals (docstring, `--port` default, docker publish, help
text) and the preset's URL was a fifth, different number. These tests assert the pairing
rather than any one of the copies.
"""

import re
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parents[1] / "cli.py"


@pytest.fixture(scope="module")
def src() -> str:
    return CLI.read_text(encoding="utf-8")


def _presets(src: str) -> str:
    m = re.search(r"_BACKEND_PRESETS: dict\[str, dict\] = \{(.*?)\n\}", src, re.S)
    assert m, "could not locate _BACKEND_PRESETS — this test is stale, not passing"
    return m.group(1)


def test_a_backend_preset_targets_the_port_bonsai_local_serves(src):
    """The whole point: after `adk bonsai-local`, some `--backend` must reach it."""
    port = re.search(r"BONSAI_LOCAL_PORT = (\d+)", src)
    assert port, "BONSAI_LOCAL_PORT is gone — the port has been un-unified"
    body = _presets(src)
    assert "BONSAI_LOCAL_PORT" in body, (
        "no backend preset references BONSAI_LOCAL_PORT. `adk bonsai-local` would start a "
        "server no agent can dial — the exact gap this file exists to prevent."
    )


def test_the_preset_is_named_after_the_command_that_starts_it(src):
    """Discoverability is the feature. `adk bonsai-local` -> `--backend bonsai-local`."""
    assert '"bonsai-local"' in _presets(src)


def test_the_cli_default_port_is_not_a_separate_literal(src):
    """A second copy of the number is how the two halves drifted apart in the first place."""
    assert 'default=BONSAI_LOCAL_PORT' in src, (
        "the --port default is a bare literal again; it must read the shared constant"
    )
    assert 'os.environ.get("AITHER_BONSAI_PORT", str(BONSAI_LOCAL_PORT))' in src


def test_the_new_preset_is_additive_and_does_not_hijack_local_or_bonsai(src):
    """Do NOT fix the laptop path by hijacking the FLEET presets.

    This test used to assert `local`/`bonsai` both spell `localhost:8201/v1`, pinning the
    literal instead of the rule. That became wrong on 2026-08-22 (3a69e0c19a): :8201 is
    AitherVLLMSwap, whose bonsai slot had been OFFLINE since 2026-07-25, so the presets
    were deliberately repointed to MicroScheduler (:8150) — i.e. the very audience the
    old assertion claimed to protect was being served nothing. The pin then failed for
    doing its job backwards: the code moved for a measured reason and the test called it
    a regression.

    The invariant that actually survives is the one the docstring always described —
    `bonsai-local` is ADDITIVE. The laptop preset owns BONSAI_LOCAL_PORT; the two fleet
    presets must keep pointing somewhere else, whatever that somewhere currently is.
    """
    body = _presets(src)
    fleet = re.findall(r'"(local|bonsai)":\s*\{([^}]*)\}', body)
    assert len(fleet) == 2, "the `local` and `bonsai` presets are gone"
    for name, entry in fleet:
        assert "BONSAI_LOCAL_PORT" not in entry, (
            f"the `{name}` preset was repointed at BONSAI_LOCAL_PORT — that hijacks a fleet "
            "preset to serve laptops instead of adding a preset, which trades one broken "
            "audience for another. Use `--backend bonsai-local`."
        )


def test_the_docstring_no_longer_claims_8090_is_the_local_tier(src):
    """The false claim is what made this look wired. It must not come back."""
    m = re.search(r"def cmd_bonsai_local\(args\).*?\"\"\"(.*?)\"\"\"", src, re.S)
    assert m, "cmd_bonsai_local is gone — this test is stale, not passing"
    doc = m.group(1)
    assert "ladder's `local` tier" not in doc, (
        "the docstring again claims :8090 is the `local` tier; `local` is a fleet preset "
        "pointing elsewhere — the laptop server is reached by `--backend bonsai-local`"
    )
