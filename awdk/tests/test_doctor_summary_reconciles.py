"""`adk doctor` must not have invisible failures.

Measured 2026-08-07 on a clean run: the tool printed 10 status lines (9 `[OK]`,
1 `[!!]`) and then said **8/11 checks passed**. `cmd_doctor` counts 11 checks,
but `check_dgx` and `check_cloud_keys` returned falsy while printing NOTHING —
so two of the three failures were invisible and the arithmetic could not be
reconciled with what was on screen.

That matters more than it looks. `adk doctor` is what the README offers as
"something wrong? this names it", and it is the first thing a stranger runs when
the tool misbehaves. A diagnostic whose summary disagrees with its own output
teaches people to stop trusting it — and the two silent checks were exactly the
ones a new user is most likely to be missing (no remote endpoint, no cloud key).

The invariant asserted here is deliberately the WEAK one: every counted check
must SAY something. "Exactly one line per check" was tried first and is wrong —
several checks legitimately report multiple findings (vLLM lists ports, packs
lists packs), so that version failed on correct code, which is how a test gets
deleted rather than fixed.
"""

from __future__ import annotations

import re

import pytest
from adk import doctor

_STATUS = re.compile(r"\[(?:OK|!!|--)\]")


@pytest.mark.parametrize(
    "label,fn",
    doctor.DOCTOR_CHECKS,
    ids=[label for label, _ in doctor.DOCTOR_CHECKS],
)
def test_every_counted_check_says_something(label, fn, capsys):
    """A check counted in "<passed>/<total>" must emit a visible verdict.

    Runs against the REAL environment on purpose. Whether a given check passes
    here depends on the host, and that is fine — the assertion is only that it
    is not SILENT, which holds either way.
    """
    fn()
    out = capsys.readouterr().out
    assert _STATUS.search(out), (
        f"check {label!r} printed no [OK]/[!!]/[--] line. It is counted in the "
        f"doctor's summary, so on the host where it fails the user sees a "
        f"smaller number with nothing to explain it."
    )


def test_summary_total_matches_the_number_of_checks(capsys):
    """The denominator must be the real check count, not a hand-written number."""
    doctor.cmd_doctor()
    out = capsys.readouterr().out
    m = re.search(r"(\d+)/(\d+) checks passed", out)
    assert m, f"no summary line in doctor output:\n{out[-500:]}"
    passed, total = int(m.group(1)), int(m.group(2))
    assert total == len(doctor.DOCTOR_CHECKS)
    assert 0 <= passed <= total


def test_a_silent_check_would_be_caught():
    """Mutation guard: prove the assertion above can actually fail.

    Without this, the parametrized test could pass trivially on a host where
    every check happens to print, and nobody would know it can fail.
    """
    silent_output = ""  # what check_dgx used to produce on absence
    with pytest.raises(AssertionError):
        assert _STATUS.search(silent_output), "a silent check must not pass"
