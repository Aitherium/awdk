"""
Build standalone executables for AitherADK.

Produces a single-file executable (`aither` / `aither.exe`) that requires
no Python installation on the target machine.

Usage:
    python packaging/build_executable.py          # Build for current platform
    python packaging/build_executable.py --onedir  # Build as directory (faster startup)

Output:
    dist/aither        (Linux/macOS)
    dist/aither.exe    (Windows)

Requires: pip install pyinstaller
"""

import argparse
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


#: Packages the DAEMONS never touch. Excluded only on the narrow build, where
#: they are provably unreachable -- measured through the import system, not
#: guessed: importing daemon.create_app and server.main together loads 35 adk
#: modules and ZERO of these.
NARROW_EXCLUDES = (
    # Tier 1 -- the ML stack. Excluding these took the build 694 -> 426 MiB.
    "torch", "torchvision", "torchaudio", "transformers", "scipy", "sklearn",
    "pandas", "matplotlib", "cv2", "PIL", "boto3", "botocore", "grpc",
    "tensorboard", "datasets", "accelerate",
    # Tier 2 -- found only because 426 MiB was still absurd for two daemons.
    # BUILT AND SERVED: 199,768,543 bytes (190.5 MiB), 77 hooks, 72.5% off
    # the wide build. `harness serve` answers /health 200 and `up` starts
    # with zero import failures, so nothing excluded here was load-bearing.
    # 🚨 The tier-1 list was verified with a probe that asked "is torch in
    # sys.modules?" for ELEVEN names I had chosen, and it answered "0 ML
    # modules" -- truthfully, and uselessly, because the build was still
    # dragging in PyQt6, playwright, onnxruntime, numba/llvmlite, pyarrow,
    # langchain, nltk, imageio and a PDF/Office document stack. A probe scoped
    # to the list you already suspect cannot find what you did not suspect.
    # Re-measured against THIRTY names: the daemons import ZERO of them at
    # runtime. PyInstaller collects them from static traces of code paths these
    # two servers never execute.
    "numpy", "numba", "llvmlite", "onnxruntime", "pyarrow", "fastparquet",
    "playwright", "PyQt6", "IPython", "jedi", "parso", "black", "blib2to3",
    "langchain", "nltk", "imageio", "imageio_ffmpeg", "av", "pypdfium2",
    "pdfminer", "openpyxl", "pptx", "docx", "lxml", "google.cloud",
    "googleapiclient", "google.api_core", "ddgs", "fake_useragent",
)


def build(onedir: bool = False, narrow: bool = False, entry: str = "",
          name: str = "", add_data: list[str] | None = None,
          paths: list[str] | None = None):
    """Build the executable.

    ``narrow`` roots the freeze at packaging/daemon_entry.py -- the two daemons
    the AitherOS launcher starts -- instead of the whole CLI. Measured
    2026-09-05, the full CLI build is 727,327,049 bytes (694 MiB) in ~17
    minutes across 164+ hooks, because cli.py imports adk.images and drags in
    the entire ML stack. The daemons import none of it.

    NOT the default: the wide build is what ships to humans as `aither`, and
    quietly narrowing it would remove every command they use.
    """
    if narrow:
        entry_point = str(ROOT / "packaging" / "daemon_entry.py")
        # `awdaemons`, not `aither-daemons`. THREE names for one artifact is a
        # measured defect, not a nit: build_executable.py emitted
        # `aither-daemons`, daemon_entry.py's prog said `awdaemons`, and
        # surfaces.ts looked for `bin/aither` -- so the launcher could not find
        # the narrow build without a human renaming it, and `--help` announced a
        # different program than the one you ran. The registered brick is
        # `awdaemons` (AitherOS/config/addon_manifests/awdaemons.yaml,
        # ecosystem.yaml), so that is the name everything else moves to.
        default_name = "awdaemons"
    else:
        entry_point = str(ROOT / "adk" / "cli.py")
        default_name = "aither"

    # An EXTERNAL entry point may root the freeze instead. A product-side
    # entry point (the first one adds a `saga serve` verb) extends
    # daemon_entry's parser; it cannot live in this tree, which is
    # public-mirrored and may not name private product paths
    # (tools/check_public_paths.py awdk). Passing the entry in keeps ONE freeze
    # recipe rather than a second copy of NARROW_EXCLUDES that drifts.
    if entry:
        entry_point = str(Path(entry).resolve())
        if not Path(entry_point).is_file():
            print(f"Entry point not found: {entry_point}")
            sys.exit(2)
    name = name or default_name

    args = [
        sys.executable, "-m", "PyInstaller",
        entry_point,
        "--name", name,
        "--noconfirm",
        "--clean",
    ]

    if onedir:
        args.append("--onedir")
    else:
        args.append("--onefile")

    # An external entry imports its siblings by bare name after a runtime
    # sys.path insert, which PyInstaller's static analysis never executes -- so
    # the freeze builds, and the binary dies on its first import. Search roots
    # the analysis needs are named here, the same way data is.
    for extra in paths or []:
        args.extend(["--paths", str(Path(extra).resolve())])

    if narrow:
        for mod in NARROW_EXCLUDES:
            args.extend(["--exclude-module", mod])

    # Include data files
    data_sep = ";" if platform.system() == "Windows" else ":"

    # Include the docker-compose template
    compose = ROOT / "docker-compose.adk-vllm.yml"
    if compose.exists():
        args.extend(["--add-data", f"{compose}{data_sep}adk"])

    # Caller-supplied data, written `src:dst` and translated to the platform
    # separator here so one recipe works on both. 🚨 The narrow build shipped
    # NO identities: adk/identity.py resolves `identities/` package-relative, so
    # `create_app(identity="saga")` inside a onefile freeze finds no saga.yaml
    # and the daemon starts as a different agent -- a binary that runs and is
    # the wrong program. Whatever a verb reads from disk has to be named here.
    for item in add_data or []:
        src, _, dst = item.partition(":")
        if not dst:
            print(f"--add-data needs src:dst, got {item!r}")
            sys.exit(2)
        resolved = Path(src).resolve()
        if not resolved.exists():
            print(f"--add-data source not found: {resolved}")
            sys.exit(2)
        args.extend(["--add-data", f"{resolved}{data_sep}{dst}"])

    # Hidden imports that PyInstaller misses
    hidden = [
        # The GUI wizard imports tkinter lazily (see adk/shell/gui_wizard.py) —
        # bundle it explicitly so the standalone exe can open the setup window.
        "tkinter",
        "tkinter.ttk",
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
    ]
    for h in hidden:
        args.extend(["--hidden-import", h])

    # Platform-specific
    if platform.system() == "Windows":
        args.extend(["--icon", "NONE"])  # TODO: add icon
    # NOTE: Do NOT pass --target-arch universal2 on Darwin. PyPI wheels for
    # pydantic_core, etc. are arch-specific (not fat binaries), so universal2
    # builds fail with IncompatibleBinaryArchError. Build for native arch only.

    # Console app (not windowed)
    args.append("--console")

    print(f"Building {name} executable...")
    print(f"  Platform: {platform.system()} {platform.machine()}")
    print(f"  Mode: {'onedir' if onedir else 'onefile'}")
    print(f"  Entry: {entry_point}")
    print()

    result = subprocess.run(args, cwd=str(ROOT))
    if result.returncode != 0:
        print("Build failed!")
        sys.exit(1)

    dist_dir = ROOT / "dist"
    exe_name = f"{name}.exe" if platform.system() == "Windows" else name

    if onedir:
        exe_path = dist_dir / name / exe_name
    else:
        exe_path = dist_dir / exe_name

    if exe_path.exists():
        size_mb = exe_path.stat().st_size / (1024 * 1024)
        print()
        print(f"Build complete: {exe_path}")
        print(f"Size: {size_mb:.1f} MB")
        print()
        print("Distribution:")
        print(f"  1. Upload to https://aitherium.com/download/{exe_name}")
        print(f"  2. Update install.sh/install.ps1 download URLs")
        print(f"  3. Update winget/brew/npm manifests with SHA256")
    else:
        print(f"Warning: expected output at {exe_path} not found")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build AitherADK executable")
    parser.add_argument("--onedir", action="store_true",
                        help="Build as directory instead of single file")
    parser.add_argument("--narrow", action="store_true",
                        help="freeze ONLY the daemons the AitherOS launcher "
                             "starts (packaging/daemon_entry.py) and exclude "
                             "the ML stack -- the wide build is 694 MiB")
    parser.add_argument("--entry", default="",
                        help="root the freeze at THIS entry point instead "
                             "(it must extend packaging/daemon_entry.py's "
                             "parser, not fork it)")
    parser.add_argument("--name", default="",
                        help="output name (default awdaemons for --narrow, "
                             "aither otherwise)")
    parser.add_argument("--add-data", action="append", default=[],
                        metavar="SRC:DST",
                        help="extra data to bundle, repeatable; the platform "
                             "path separator is applied here")
    parser.add_argument("--paths", action="append", default=[],
                        metavar="DIR",
                        help="extra import search root for the analysis, "
                             "repeatable (an --entry's bare-name siblings)")
    args = parser.parse_args()
    build(onedir=args.onedir, narrow=args.narrow, entry=args.entry,
          name=args.name, add_data=args.add_data, paths=args.paths)
