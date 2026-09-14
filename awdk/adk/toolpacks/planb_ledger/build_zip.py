"""Build PlanB-Ledger.zip — the self-bootstrapping consumer kit.

The zip contains the SAME core modules as this toolpack (relative imports make
them location-agnostic) as package `planb/`, plus the install wizards. Output:
awdk/dist/PlanB-Ledger.zip.

Run: python adk/toolpacks/planb_ledger/build_zip.py   (from awdk/)
"""
from __future__ import annotations

import zipfile
from pathlib import Path

PACK = Path(__file__).resolve().parent
DIST = PACK.parents[2] / "dist"

CORE = ["__init__.py", "engine.py", "ledger.py", "sheet_render.py", "sheet.py",
        "brain.py", "bot.py", "cli.py", "bootstrap.py"]
KIT = ["install.ps1", "install.sh", "README.md", "requirements.txt"]


def build() -> Path:
    DIST.mkdir(parents=True, exist_ok=True)
    out = DIST / "PlanB-Ledger.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in KIT:
            z.write(PACK / "dist_kit" / name, f"PlanB-Ledger/{name}")
        for name in CORE:
            z.write(PACK / name, f"PlanB-Ledger/planb/{name}")
    return out


if __name__ == "__main__":
    path = build()
    size_kb = path.stat().st_size / 1024
    print(f"built {path} ({size_kb:.0f} KB)")
