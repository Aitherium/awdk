"""Mobile testing — drive Android devices/emulators and iOS simulators, keep the proof.

An agent that says "the app works on Android" should hand back what it saw. This
module finds the devices this machine can reach (``adb`` for Android, ``xcrun
simctl`` for iOS simulators on macOS), runs an end-to-end flow on one of them with
Maestro, and writes every run into a proof directory: the flow, the JUnit report,
screenshots taken before and after, the Maestro debug output, and a ``run.json``
that names the device, the exit code and each artifact.

    adk mobile devices
    adk mobile shot --device emulator-5554
    adk mobile test flows/login.yaml [--device ID] [--app com.acme.app]

Nothing here is a hard dependency: a missing ``adb``/``xcrun``/``maestro`` is
reported with the install hint and a non-zero exit, never an import error.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, List, Optional

PROOF_ROOT = Path(os.environ.get("AITHER_MOBILE_PROOF", Path.home() / ".aither" / "mobile"))
INSTALL_HINTS = {
    "adb": "Android platform-tools: https://developer.android.com/tools/releases/platform-tools",
    "xcrun": "Xcode command-line tools (macOS only): xcode-select --install",
    "maestro": "Maestro: https://maestro.mobile.dev (needs Java 17+)",
}

Runner = Callable[..., subprocess.CompletedProcess]


def _run(argv: List[str], timeout: float = 60, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, timeout=timeout, **kw)


@dataclass
class Device:
    platform: str  # "android" | "ios"
    id: str
    name: str
    state: str  # "device", "emulator", "Booted", "unauthorized", ...

    @property
    def usable(self) -> bool:
        return self.state in ("device", "Booted")


def list_devices(run: Runner = _run, which: Callable[[str], Optional[str]] = shutil.which,
                 system: str = "") -> "tuple[List[Device], List[str]]":
    """Every reachable device, plus notes on the tools that are missing."""
    devices: List[Device] = []
    notes: List[str] = []
    if which("adb"):
        out = run(["adb", "devices", "-l"], text=True, encoding="utf-8", errors="replace")
        for line in (out.stdout or "").splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2:
                continue
            model = next((p.split(":", 1)[1] for p in parts[2:] if p.startswith("model:")), "")
            devices.append(Device("android", parts[0], model or parts[0], parts[1]))
    else:
        notes.append(f"adb not found — {INSTALL_HINTS['adb']}")
    if (system or platform.system()) == "Darwin":
        if which("xcrun"):
            out = run(["xcrun", "simctl", "list", "devices", "--json"], text=True,
                      encoding="utf-8", errors="replace")
            try:
                data = json.loads(out.stdout or "{}")
            except json.JSONDecodeError:
                data = {}
            for runtime, sims in (data.get("devices") or {}).items():
                for s in sims:
                    if s.get("isAvailable", True):
                        name = f"{s.get('name', '')} ({runtime.rsplit('.', 1)[-1]})"
                        devices.append(Device("ios", s["udid"], name, s.get("state", "")))
        else:
            notes.append(f"xcrun not found — {INSTALL_HINTS['xcrun']}")
    else:
        notes.append("iOS simulators need macOS; this host can drive Android only")
    return devices, notes


def pick_device(devices: List[Device], wanted: str = "") -> Device:
    usable = [d for d in devices if d.usable]
    if wanted:
        for d in devices:
            if d.id == wanted:
                if not d.usable:
                    raise LookupError(f"device {wanted} is {d.state}, not ready")
                return d
        raise LookupError(f"device {wanted} not found")
    if not usable:
        raise LookupError("no ready device — start an emulator/simulator or plug one in")
    return usable[0]


def screenshot(dev: Device, dest: Path, run: Runner = _run) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dev.platform == "android":
        out = run(["adb", "-s", dev.id, "exec-out", "screencap", "-p"])
        if out.returncode != 0 or not out.stdout:
            raise RuntimeError(f"screencap failed: {(out.stderr or b'')[:200]!r}")
        dest.write_bytes(out.stdout)
    else:
        out = run(["xcrun", "simctl", "io", dev.id, "screenshot", str(dest)])
        if out.returncode != 0:
            raise RuntimeError(f"simctl screenshot failed: {(out.stderr or b'')[:200]!r}")
    return dest


def run_flow(flow: Path, dev: Device, *, app: str = "", run: Runner = _run,
             which: Callable[[str], Optional[str]] = shutil.which,
             proof_root: Path = PROOF_ROOT, timeout: float = 1800) -> dict:
    """Run one Maestro flow on ``dev`` and write the proof directory. Returns run.json."""
    # The RESOLVED path, not the bare name: on Windows maestro is maestro.bat, and
    # CreateProcess does not apply PATHEXT to a bare argv[0].
    maestro = which("maestro")
    if not maestro:
        raise FileNotFoundError(f"maestro not found — {INSTALL_HINTS['maestro']}")
    if not flow.is_file():
        raise FileNotFoundError(f"flow not found: {flow}")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = proof_root / f"{stamp}-{flow.stem}-{dev.platform}"
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(flow, out_dir / flow.name)
    artifacts: List[str] = [flow.name]

    def shot(name: str) -> None:
        try:
            artifacts.append(screenshot(dev, out_dir / name, run=run).name)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            artifacts.append(f"{name}: {e}")

    shot("before.png")
    argv = [maestro, "--device", dev.id, "test", str(flow), "--format", "junit",
            "--output", str(out_dir / "report.xml"), "--debug-output", str(out_dir / "debug")]
    if app:
        argv += ["-e", f"APP_ID={app}"]
    started = time.time()
    try:
        res = run(argv, timeout=timeout, text=True, encoding="utf-8", errors="replace")
        code, log = res.returncode, (res.stdout or "") + (res.stderr or "")
    except subprocess.TimeoutExpired:
        code, log = 124, f"timed out after {timeout}s"
    (out_dir / "maestro.log").write_text(log, encoding="utf-8")
    shot("after.png")
    artifacts += [p for p in ("report.xml", "debug") if (out_dir / p).exists()]
    artifacts.append("maestro.log")
    record = {
        "flow": flow.name, "device": asdict(dev), "app": app, "exit_code": code,
        "passed": code == 0, "seconds": round(time.time() - started, 1),
        "proof_dir": str(out_dir), "artifacts": artifacts,
    }
    (out_dir / "run.json").write_text(json.dumps(record, indent=1), encoding="utf-8")
    return record


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="adk mobile",
                                description="Test Android/iOS apps end to end and keep the proof.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices", help="List reachable devices, emulators and simulators")
    s = sub.add_parser("shot", help="Screenshot a device into the proof dir")
    s.add_argument("--device", default="")
    t = sub.add_parser("test", help="Run a Maestro flow and write a proof directory")
    t.add_argument("flow")
    t.add_argument("--device", default="")
    t.add_argument("--app", default="", help="App id, passed to the flow as APP_ID")
    t.add_argument("--timeout", type=float, default=1800)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    devices, notes = list_devices()
    if args.cmd == "devices":
        for d in devices:
            print(f"{d.platform:8} {d.id:28} {d.state:13} {d.name}")
        if not devices:
            print("no devices")
        for n in notes:
            print(f"note: {n}", file=sys.stderr)
        return 0
    try:
        dev = pick_device(devices, args.device)
    except LookupError as e:
        print(f"mobile: {e}", file=sys.stderr)
        for n in notes:
            print(f"note: {n}", file=sys.stderr)
        return 2
    if args.cmd == "shot":
        dest = PROOF_ROOT / "shots" / f"{time.strftime('%Y%m%d-%H%M%S')}-{dev.id}.png"
        try:
            print(screenshot(dev, dest))
        except RuntimeError as e:
            print(f"mobile: {e}", file=sys.stderr)
            return 1
        return 0
    try:
        rec = run_flow(Path(args.flow), dev, app=args.app, timeout=args.timeout)
    except FileNotFoundError as e:
        print(f"mobile: {e}", file=sys.stderr)
        return 2
    print(f"{'PASS' if rec['passed'] else 'FAIL'}  {rec['flow']} on {dev.name}  "
          f"({rec['seconds']}s)  proof: {rec['proof_dir']}")
    return 0 if rec["passed"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = ["Device", "list_devices", "pick_device", "screenshot", "run_flow", "main"]
