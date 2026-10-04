"""`adk models list | recommend | pull <id> | use <id>`.

One command family from "what is there" to "it is serving":

    adk models list            every catalogue model: size, purpose, runtime, the memory
                               it needs, its licence, and whether it fits THIS machine
    adk models recommend       the largest permitted model this machine can run
    adk models pull <id>       download it, resumable and verified, into the Bonsai
                               installer's models directory
    adk models use <id>        serve it and point adk at it

`pull` and `use` go through the licence gate (``catalogue.licence_verdict``) and refuse,
in one line, any model whose licence record is missing or does not permit
redistribution. `list` shows those models too, marked refused, so the catalogue is never
silently shorter than it is.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from . import catalogue, probe


def _say(msg: str) -> None:
    print(msg, flush=True)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _gb(mb: int) -> str:
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb} MB"


def add_parser(sub: Any) -> None:
    """Register ``models`` on the top-level ``adk`` subparsers."""
    p = sub.add_parser(
        "models",
        help="Browse the model catalogue, pick one for this machine, pull it verified, "
             "serve it (list | recommend | pull | use)")
    ms = p.add_subparsers(dest="models_command")
    for name, text in (("list", "Every catalogue model, its licence and whether it fits"),
                       ("recommend", "The largest permitted model this machine can run")):
        sp = ms.add_parser(name, help=text)
        sp.add_argument("--budget-mb", type=int, default=0,
                        help="Size against this much memory instead of probing")
        sp.add_argument("--json", action="store_true", help="Machine-readable output")
    pull = ms.add_parser("pull", help="Download a model (resumable, size + sha256 checked)")
    pull.add_argument("id", help="Catalogue id (see: adk models list)")
    pull.add_argument("--dir", default="", help="Download directory (default: the Bonsai "
                                                "installer's models directory)")
    use = ms.add_parser("use", help="Serve a pulled model and point adk at it")
    use.add_argument("id", help="Catalogue id (see: adk models list)")
    use.add_argument("--port", type=int, default=0,
                     help="Port to serve on (default: 8080 chat, 8229 embedding)")
    use.add_argument("--dir", default="", help="Where the model was pulled to")
    use.add_argument("--no-start", action="store_true",
                     help="Only point adk at the endpoint; do not (re)start a server")


def _hardware(args: Any) -> probe.Hardware:
    budget = int(getattr(args, "budget_mb", 0) or 0)
    if budget > 0:
        return probe.Hardware("given", budget, "--budget-mb", 0)
    return probe.detect()


def cmd_list(args: Any, cat: Dict[str, Any]) -> int:
    hw = _hardware(args)
    rows = catalogue.rows(cat, hw.budget_mb)
    if getattr(args, "json", False):
        _say(json.dumps({"hardware": hw.to_dict(), "models": rows}, indent=2))
        return 0
    _say(f"This machine: {hw.describe()}")
    _say("")
    _say(f"{'ID':<19}{'SIZE':>9}  {'NEEDS':>8}  {'FITS':<5}{'ROLE':<10}{'RUNTIME':<17}LICENCE")
    for r in rows:
        status = "ok" if r["allowed"] else "REFUSED"
        _say(f"{r['id']:<19}{_gb(r['size_mb']):>9}  {_gb(r['floor_mb']):>8}  "
             f"{'yes' if r['fits'] else 'no':<5}{r['role']:<10}{r['runtime']:<17}"
             f"{r['licence']} [{status}]")
        _say(f"    {r['purpose'] or '-'}")
        if not r["allowed"]:
            _say(f"    refused: {r['reason']}")
    _say("")
    _say("NEEDS = memory (VRAM, or RAM on CPU) under the catalogue's headroom rule. "
         "REFUSED models cannot be pulled.")
    return 0


def cmd_recommend(args: Any, cat: Dict[str, Any]) -> int:
    hw = _hardware(args)
    pick = catalogue.recommend(cat, hw.budget_mb, cpu_only=hw.kind == "ram")
    skipped = catalogue.refused_rungs(cat, hw.budget_mb)
    if getattr(args, "json", False):
        _say(json.dumps({"hardware": hw.to_dict(), "recommended": pick,
                         "fits_but_refused": skipped}, indent=2))
        return 0 if pick else 1
    _say(f"This machine: {hw.describe()}")
    if pick:
        m = cat["models"][pick]
        _say(f"Recommended: {pick} ({_gb(int(m['size_mb']))}, needs "
             f"{_gb(catalogue.floor_mb(cat, m))}) - {m.get('purpose') or ''}".rstrip(" -"))
        _say(f"  licence: {catalogue.licence_verdict(m).reason}")
        _say(f"  next: adk models pull {pick} && adk models use {pick}")
        return 0
    if skipped:
        _say("No model this machine can run may be distributed yet. These fit, but the "
             "licence gate refuses them:")
        for mid in skipped:
            _say(f"  {mid}: {catalogue.licence_verdict(cat['models'][mid]).reason}")
    else:
        _say(f"No catalogue model fits {_gb(hw.budget_mb)} with headroom "
             f"(the floor is {_gb(int(cat.get('min_budget_mb') or 0))}).")
    return 1


def _gated(cat: Dict[str, Any], model_id: str, verb: str) -> Optional[Dict[str, Any]]:
    """The model when the licence gate permits it; else print the refusal -> None."""
    m = catalogue.get(cat, model_id)
    verdict = catalogue.licence_verdict(m)
    if not verdict.allowed:
        _err(f"adk models {verb}: refused '{model_id}': {verdict.reason}")
        return None
    return m


def cmd_pull(args: Any, cat: Dict[str, Any]) -> int:
    from . import download, serve

    m = _gated(cat, args.id, "pull")
    if m is None:
        return 1
    dest_dir = Path(args.dir) if args.dir else serve.models_dir()
    _say(f"Pulling {args.id} -> {dest_dir}")
    try:
        path, hashed = download.fetch_model(m, dest_dir, say=_say)
    except download.DownloadError as exc:
        _err(f"adk models pull: {exc}")
        return 1
    if hashed:
        _say(f"Ready: {path} (size and sha256 verified)")
    else:
        _say(f"Ready: {path} (size verified against the mirror; the catalogue records no "
             "sha256 for this model, so its content is NOT hash-verified)")
    _say(f"  next: adk models use {args.id}")
    return 0


def cmd_use(args: Any, cat: Dict[str, Any]) -> int:
    from . import serve

    m = _gated(cat, args.id, "use")
    if m is None:
        return 1
    role = str(m.get("role") or "chat")
    if role == "tts":
        # A voice is a file a local runtime reads, not an endpoint llama.cpp serves.
        _err(f"adk models use: '{args.id}' is a voice, not a served model; speak with: "
             'adk home voice --say "hello"')
        return 1
    port = int(args.port or (serve.EMBED_PORT if role == "embedding" else serve.CHAT_PORT))
    gguf = (Path(args.dir) if args.dir else serve.models_dir()) / m["file"]
    size = m.get("size_bytes")
    if not gguf.is_file() or (size and gguf.stat().st_size != size):
        _err(f"adk models use: {gguf} is missing or incomplete; run: adk models pull {args.id}")
        return 1
    if not args.no_start:
        try:
            serve.start(m, gguf, port, say=_say)
        except serve.ServeError as exc:
            _err(f"adk models use: {exc}")
            return 1
    from adk.config import save_saved_config

    cfg = serve.backend_config(m, port)
    save_saved_config(dict(cfg))
    if role == "embedding":
        _say(f"Embedding endpoint: {cfg['embeddings_url']} ({args.id}). The chat backend "
             "was not changed.")
        _say(f"  adk now embeds in the {args.id} space (saved embed_space; "
             "AITHER_EMBED_SPACE still overrides it)")
    else:
        _say(f"Chat backend: {cfg['inference_url']} serving {args.id} as "
             f"{cfg['default_model']}. adk status / adk start now use it.")
        if not args.no_start:
            _say("  The login autostart entry still names the model the installer chose; "
                 "re-run `adk models use` after a reboot, or re-run the installer.")
    return 0


def run(args: Any) -> int:
    """Dispatch ``adk models ...``. -> process exit code."""
    sub = getattr(args, "models_command", None)
    handlers = {"list": cmd_list, "recommend": cmd_recommend, "pull": cmd_pull,
                "use": cmd_use}
    if sub not in handlers:
        _say("usage: adk models {list | recommend | pull <id> | use <id>}")
        return 1
    try:
        return handlers[sub](args, catalogue.load())
    except catalogue.CatalogueError as exc:
        _err(f"adk models: {exc}")
        return 2


def main(argv: Optional[list] = None) -> int:
    """``python -m adk.models.cli`` -- the same family without the full ``adk`` CLI."""
    ap = argparse.ArgumentParser(prog="adk")
    add_parser(ap.add_subparsers(dest="command"))
    args = ap.parse_args(["models"] + list(sys.argv[1:] if argv is None else argv))
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
