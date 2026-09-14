"""``adk gobbonet`` — get GobboNet running with keyless search, in one command.

The setup was three commands: clone the UI, start the server, then set an env
var to a URL you had to know. Every one of those is a place to stop, and the
last is the worst — nothing tells you the value is wrong. You get a UI with a
search box that returns nothing, which reads as "search is broken" rather than
"SEARCH_URL is unset".

So this does all of it: finds or clones the UI, starts the server, wires the
search URL, and prints the one line the user actually needs. It is idempotent —
run it again and it reuses the checkout rather than failing on a directory that
already exists.

    adk gobbonet                      # clone if needed, serve, print the URL
    adk gobbonet --ui ./GobboNet      # use a checkout you already have
    adk gobbonet --no-open            # don't open a browser

Deliberately NOT silent about what it did: it prints the checkout path, the
port, and the SEARCH_URL to paste, because a wrapper that hides the underlying
commands leaves the user unable to debug it or run the pieces themselves.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

UPSTREAM = "https://github.com/ElodineOfficial/GobboNet"
DEFAULT_PORT = 11434


def _looks_like_gobbonet(d: Path) -> bool:
    return (d / "chat.html").is_file()


def _find_existing(explicit: str | None) -> Path | None:
    """Locate a GobboNet checkout without cloning."""
    if explicit:
        p = Path(explicit).expanduser().resolve()
        return p if _looks_like_gobbonet(p) else None
    for cand in (Path.cwd(), Path.cwd() / "GobboNet", Path.home() / "GobboNet"):
        if _looks_like_gobbonet(cand):
            return cand.resolve()
    return None


def _clone(dest: Path) -> Path:
    if not shutil.which("git"):
        raise SystemExit(
            "git is not installed, and no GobboNet checkout was found.\n"
            f"Either install git, or download {UPSTREAM} and pass --ui <folder>."
        )
    print(f"cloning {UPSTREAM} -> {dest}")
    r = subprocess.run(["git", "clone", "--depth", "1", UPSTREAM, str(dest)])
    if r.returncode != 0:
        raise SystemExit(
            f"clone failed (exit {r.returncode}). Download {UPSTREAM} manually "
            "and pass --ui <folder>."
        )
    if not _looks_like_gobbonet(dest):
        # A clone that "succeeded" into an unexpected layout must not be served
        # as though it were fine — the server would 404 every asset.
        raise SystemExit(f"cloned, but {dest} has no chat.html — layout changed upstream?")
    return dest


def setup_model() -> int:
    """Install a local model server sized to THIS machine.

    Self-service means the tool works out what fits rather than making the user
    guess a quant. adk already knows how: `detect_accel()` reads the actual
    accelerator and RAM, `pick_quant()` turns that into a quantisation, and
    `install()` fetches llama.cpp and the weights.

    Reused rather than reimplemented — a second copy of the hardware logic would
    drift from the one the rest of adk uses, and the failure would be a model
    that does not fit, which reads as "this machine is too small" rather than
    "two code paths disagree".
    """
    try:
        from adk import llamacpp_setup as lc
    except ImportError as e:
        print(f"could not load the llama.cpp installer: {e}")
        return 1

    accel = lc.detect_accel()
    print(f"hardware: {accel.kind}, {accel.vram_gb:.1f} GB VRAM, {accel.ram_gb:.1f} GB RAM")
    quant = lc.pick_quant(accel.vram_gb, accel.ram_gb, accel.kind)
    print(f"selected quantisation: {quant}")
    print("installing llama.cpp and a model that fits (this downloads several GB)…\n")

    rc = lc.install(accel=accel) if _accepts_accel(lc.install) else lc.install()
    # lc.install returns an InstallResult, not an exit code. `rc not in (0,
    # None, True)` treated EVERY result -- including success=True -- as a
    # failure, so a fully working install printed "setup did not complete" and
    # the launcher bailed before serving the UI. Measured live 2026-08-22:
    # InstallResult(success=True, ...) with llama-server already running was
    # reported as a failure. Read the field; keep exit-code semantics only for
    # an install() that really returns one.
    ok = rc.success if hasattr(rc, "success") else rc in (0, None, True)
    if not ok:
        detail = getattr(rc, "error", "") or rc
        print(f"\nsetup did not complete ({detail}).")
        return 1

    # VERIFY. An installer that copies files and prints "done" is exactly how a
    # broken setup reads as a working one.
    if lc.smoke_test(lc.DEFAULT_PORT):
        print(f"\nverified: a model is answering on port {lc.DEFAULT_PORT}")
        return 0
    print(f"\ninstalled, but nothing answered on port {lc.DEFAULT_PORT} yet.")
    print("It may still be loading — re-run `adk gobbonet` in a moment.")
    return 0


def _accepts_accel(fn) -> bool:
    import inspect

    try:
        return "accel" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def cmd_gobbonet(args) -> int:
    ui = _find_existing(getattr(args, "ui", None))

    if ui is None:
        if getattr(args, "ui", None):
            raise SystemExit(
                f"no chat.html under {args.ui} — point --ui at a GobboNet checkout"
            )
        ui = _clone(Path.cwd() / "GobboNet")
    else:
        print(f"using GobboNet checkout: {ui}")

    requested = int(getattr(args, "port", None) or DEFAULT_PORT)
    host = getattr(args, "host", None) or "127.0.0.1"

    if getattr(args, "setup_model", False):
        rc = setup_model()
        if rc != 0:
            return rc

    from adk.packs.gobbonet.server import AgenticEngine, LocalEngine, serve

    # BIND FIRST, then print. The requested port is not necessarily the bound
    # one — serve() falls back to an OS-assigned port when the requested one is
    # inside a Windows reserved block — so printing a SEARCH_URL beforehand
    # hands the user a URL nothing is listening on, which is a worse failure
    # than the bind error it was hiding.
    # exclude_port stops the server proxying chat to ITSELF: this pack serves on
    # GobboNet's default 11434, which is also ollama's, and forwarding to our own
    # port is an infinite loop that presents as a hang rather than an error.
    # Agentic by DEFAULT — that is the whole reason to install a pack rather
    # than point GobboNet at a model directly. `--plain` is the escape hatch for
    # anyone who wants the raw model with no tool loop.
    if getattr(args, "plain", False):
        engine = LocalEngine(backend=getattr(args, "backend", None), exclude_port=requested)
    else:
        engine = AgenticEngine(backend=getattr(args, "backend", None), exclude_port=requested)
    httpd = serve(ui, engine, host=host, port=requested)
    port = httpd.server_address[1]

    search_url = f"http://{host}:{port}/web_search"
    # Set it for any child process, and print it for the UI's own settings —
    # the server cannot reach into the browser's config, so telling the user
    # is not optional.
    os.environ["SEARCH_URL"] = search_url

    print()
    print(f"  UI          http://{host}:{port}/chat.html")
    print(f"  SEARCH_URL  {search_url}")
    print("              ^ paste this into GobboNet's search settings")
    print()
    print("  keyless DuckDuckGo search - no account, no API key, nothing hosted")
    print()

    if not getattr(args, "no_open", False):
        try:
            webbrowser.open(f"http://{host}:{port}/chat.html")
        except Exception as e:  # noqa: BLE001 - a headless box has no browser
            print(f"  (could not open a browser: {e})")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for `python -m adk.packs.gobbonet.launch`."""
    import argparse

    ap = argparse.ArgumentParser(
        prog="adk gobbonet",
        description="Run GobboNet with keyless web search, in one command.",
    )
    ap.add_argument("--ui", help="existing GobboNet checkout (default: find or clone)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-open", action="store_true", help="do not open a browser")
    ap.add_argument("--setup-model", action="store_true",
                    help="install llama.cpp + a model sized to this machine")
    ap.add_argument("--backend", help="pin an OpenAI-compatible server URL")
    ap.add_argument("--plain", action="store_true",
                    help="passthrough chat instead of the adk agent loop")
    return cmd_gobbonet(ap.parse_args(argv if argv is not None else sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
