# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['D:\\AitherOS-Fresh\\awdk\\adk\\cli.py'],
    pathex=[],
    binaries=[],
    datas=[('D:\\AitherOS-Fresh\\awdk\\docker-compose.adk-vllm.yml', 'adk')],
    hiddenimports=['tkinter', 'tkinter.ttk', 'httpx', 'httpx._transports', 'httpx._transports.default', 'yaml', 'uvicorn', 'uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto', 'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto', 'uvicorn.lifespan', 'uvicorn.lifespan.on', 'fastapi', 'starlette', 'anyio', 'anyio._backends', 'anyio._backends._asyncio'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='aither',
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
    icon='NONE',
)
