"""adk-specific runtime checks for the generated `adk doctor`.

The generated _doctor.py reports the aw* stack; this module is the
package-supplied hook for checks only adk can own. The one that matters on
this box is the CLAUDE LANE: settings.json, the profile matcher, the
MicroScheduler endpoint, the credential ladder, presence freshness and git
index parity — the exact failure signature of the 2026-08-25 lane-flip
incident (profile flipped to 'anthropic', CLI lost the [1m] window, recovery
needed a hand-bridged key). The engine is aither_doctor.py in this repo; adk
shells out to it rather than duplicating the checks, so the lane diagnosis
and the unattended host probe (HOST_ONLY_GATES) can never drift apart.

DISPLAY-ONLY by contract: _doctor.report() prints these lines but its exit
code is the stack verdict, not the lane's. The lane's exit code pages via
run_fleet_gates_from_host.HOST_ONLY_GATES, which runs aither_doctor directly.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# .../awdk/adk/doctor_local.py -> parents[2] is the repo root the lane tool
# lives in, whichever checkout this package was installed from.
_LANE_DOCTOR = (Path(__file__).resolve().parents[2]
                / "AitherOS" / "dev" / "tools" / "aither_doctor.py")
_MAX_LINES = 12


def _doctor_local() -> "list[str]":
    if not _LANE_DOCTOR.is_file():
        return [f"lane doctor missing at {_LANE_DOCTOR} — git pull AitherOS/dev/tools"]
    try:
        proc = subprocess.run(
            [sys.executable, str(_LANE_DOCTOR)],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180)
    except Exception as exc:  # noqa: BLE001 - a doctor that cannot run reports that
        return [f"lane doctor could not run: {type(exc).__name__}: {exc}"]
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    verdict = f"lane rc={proc.returncode}"
    if len(lines) > _MAX_LINES:
        lines = lines[:_MAX_LINES] + ["…  (full report: aither_doctor.py)"]
    return lines + [verdict]
