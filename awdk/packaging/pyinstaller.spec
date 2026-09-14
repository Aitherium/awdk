# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the `aither` CLI binary.

Build:
    pyinstaller packaging/pyinstaller.spec

Output:
    dist/aither        (Linux/macOS)
    dist/aither.exe    (Windows)
"""

import sys
from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH).parent

# Data files to bundle
datas = []
data_dir = ROOT / "adk" / "data"
if data_dir.is_dir():
    datas.append((str(data_dir), "adk/data"))

identities = ROOT / "adk" / "identities"
if identities.is_dir():
    datas.append((str(identities), "adk/identities"))

profiles = ROOT / "profiles"
if profiles.is_dir():
    datas.append((str(profiles), "profiles"))

compose = ROOT / "docker-compose.adk-vllm.yml"
if compose.exists():
    datas.append((str(compose), "adk"))

a = Analysis(
    [str(ROOT / "adk" / "cli.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "httpx",
        "httpx._transports",
        "httpx._transports.default",
        "yaml",
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "fastapi",
        "starlette",
        "anyio",
        "anyio._backends",
        "anyio._backends._asyncio",
        "adk.compliance",
        "adk.compliance.air_gap",
        "adk.compliance.attestation",
        "adk.compliance.model_licenses",
        "adk.compliance.pdf_report",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "scipy",
        "PIL",
        "torch",
        "numpy",
        "sentence_transformers",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="aither",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
