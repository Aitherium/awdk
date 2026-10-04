"""``adk home`` -- download, host and run your own agent.

    adk home init [--name NAME]                 create ~/.aither/agent-home
    adk home persona [show|path|set FILE TEXT]  view / edit the persona files
    adk home model --local bonsai|bonsai2|llamacpp|ollama|awnode | --byo deepseek|openai|anthropic
    adk home model --local bonsai2 [--quant ..] [--backend ..] [--data-dir D] [--stop]
                                                Bonsai 2 27B on our shipped PrismML build
    adk home voice --local aither               install the Aither voice (verified, resumable)
    adk home voice --say "text" [-o out.wav]    speak it on this machine, no service
    adk home harness aither|claude|openclaw|hermes
    adk home status                             everything at a glance
    adk home signin                             Sign in with Aitherium: purchases unlock
    adk home license <file-or-text>             offline activation (pasted license)
    adk home teach setup [--url U]              teacher agent: local Bonsai + classroom tools
    adk home teach start                        one click: setup, run at logon, open the page
    adk home teach stop                         stop this home's running agent (the remover)
    adk home chat "hello"                       one message to your agent
    adk home calendar [today|"this week"|..]    your built-in + subscribed calendars
    adk home calendar add "tomorrow 9:00" "Dentist"   (also: move, delete, refresh)
    adk home todo [add "Buy milk"|done ID]      your built-in to-do list
    adk home connect calendar --ics <link>      Google / Outlook / iCloud, read-only
    adk home connect mail --user you@x.com      a mailbox by app password (no OAuth)
    adk home serve [--channels relay,telegram,..] [--pair] [--no-local]
                                                answer YOUR messages on every channel
    adk home serve --install | --uninstall      start serve at logon (task / systemd --user)
    adk home say "text"                         one message to the RUNNING serve (local)
    adk home events [-n N]                      stream what the running serve sends you
    adk home connect-browser                    one-time code: chat with THIS serve from
                                                hearth.aitherium.com / aitherium.com
    adk home channels [--json]                  which channels are ready, who owns them
    adk home receipts [--verify] [-n N]         what the agent did; --verify exits 0/1/2
    adk home receipts --anchor                  sign the chain head -> <home>/receipts.anchor
    adk home trust status|init                  egress guard, approvals, receipts at a glance
    adk home report [--pdf FILE] [--days N]     device-signed data-boundary report
    adk home report --verify FILE.json          check a report's signature: exit 0/1/2
    adk home join <game-url> [--steps N] [--learn] [--say TEXT] [--act TEXT]
    adk home enroll <game-url>                  world-model enrollment (pack)

Also runnable without the adk CLI hook as ``python -m adk.home``.

Exit codes: 0 ok; 1 the game or model failed; 2 bad setup or arguments;
3 the feature needs the ``agent-home`` license pack.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from . import config as hc
from . import entitlement, harness, models

logger = logging.getLogger("adk.home.cli")

EXIT_OK, EXIT_FAIL, EXIT_SETUP, EXIT_LICENSE = 0, 1, 2, 3


def register_parser(sub: Any) -> argparse.ArgumentParser:
    """Add ``home`` to an argparse subparsers object (``adk`` top level)."""
    p = sub.add_parser("home", help="Agent Home: host your own agent, pick its "
                                    "model and harness, and let it join games")
    _build(p)
    return p


def _build(p: argparse.ArgumentParser) -> None:
    hs = p.add_subparsers(dest="home_command")

    i = hs.add_parser("init", help="Create your agent's home folder")
    i.add_argument("--name", default="my-agent")
    i.add_argument("--force", action="store_true",
                   help="Overwrite config AND persona files")
    i.add_argument("--calendar-ics", default="", metavar="LINK",
                   help="Also subscribe to a calendar you already have (Google secret "
                   "iCal address, Outlook published ICS, iCloud public calendar)")

    pe = hs.add_parser("persona", help="Show or edit the persona files")
    pe.add_argument("action", nargs="?", default="show", choices=["show", "path", "set"])
    pe.add_argument("file", nargs="?", default="",
                    help="system_prompt.md | persona.md | rules.md (for set)")
    pe.add_argument("text", nargs="?", default="", help="New content (for set); "
                    "'-' reads stdin")

    m = hs.add_parser("model", help="Choose the model: local or bring-your-own-key")
    g = m.add_mutually_exclusive_group()
    g.add_argument("--local", choices=list(models.LOCAL))
    g.add_argument("--byo", choices=list(models.BYO))
    m.add_argument("--model", default="", help="Model name (default per provider)")
    m.add_argument("--base-url", default="", help="Override the endpoint")
    m.add_argument("--key-env", default="",
                   help="NAME of the env var holding your key (never the key)")
    m.add_argument("--check", action="store_true", help="Probe the model now")
    b2 = m.add_argument_group("bonsai2", "only with --local bonsai2")
    b2.add_argument("--quant", choices=["auto", "PQ2_0", "PTQ1_0"], default="auto",
                    help="PQ2_0 (7.2 GB, CUDA/Metal-fast) or PTQ1_0 (5.9 GB, Vulkan/CPU)")
    b2.add_argument("--backend", choices=["auto", "cuda", "vulkan", "cpu", "metal"],
                    default="auto", help="Which PrismML build to run")
    b2.add_argument("--port", type=int, default=0, help="llama-server port (default 8088)")
    b2.add_argument("--ctx", type=int, default=0, help="Context size (default: sized to "
                    "free VRAM/RAM, 4096-65536)")
    b2.add_argument("--data-dir", default="", help="Where the build and the GGUF live "
                    "(default: the user data dir; or $AITHER_BONSAI2_HOME)")
    b2.add_argument("--dry-run", action="store_true", help="Print the plan; download nothing")
    b2.add_argument("--stop", action="store_true",
                    help="Stop the llama-server this started (nothing else)")

    vo = hs.add_parser("voice", help="The Aither voice on this machine: install it "
                                     "(--local aither) or speak with it (--say TEXT)")
    vo.add_argument("--local", choices=["aither"], default="",
                    help="Install this voice (download + sha256 verify; idempotent)")
    vo.add_argument("--say", default="", metavar="TEXT",
                    help="Synthesize TEXT into a wav (installs the voice if needed)")
    vo.add_argument("-o", "--output", default="aither-say.wav",
                    help="Where --say writes the wav (default: aither-say.wav)")
    vo.add_argument("--speed", type=float, default=1.0, help="0.5-2.0 (default 1.0)")
    vo.add_argument("--dir", default="", help="Voice directory (default: the shared "
                    "local model directory; or $AITHER_VOICE_DIR)")

    h = hs.add_parser("harness", help="Choose who runs the agent loop")
    h.add_argument("kind", nargs="?", choices=list(harness.HARNESSES))
    h.add_argument("--mcp-url", default="", help="MCP endpoint for openclaw/hermes")

    hs.add_parser("status", help="Setup, model, harness and license at a glance")

    si = hs.add_parser("signin", help="Sign in with Aitherium -- what you bought "
                                      "unlocks here (device code, no license to paste)")
    si.add_argument("--portal-url", default="", help="Portal/Identity URL")

    lic = hs.add_parser("license", help="Offline activation: install a license (file or text)")
    lic.add_argument("license", nargs="?", default="",
                     help="Path to the license file, or the pasted text ('-' = stdin)")
    lic.add_argument("--replace", action="store_true",
                     help="(no effect: offline licenses are kept side by side)")

    te = hs.add_parser("teach", help="Aither Classroom: a teacher's own agent on this "
                                     "computer (local Bonsai, no cost)")
    tes = te.add_subparsers(dest="teach_command")
    ts = tes.add_parser("setup", help="Probe the classroom, init, local Bonsai, sign in, "
                                      "turn the teacher tools on")
    ts.add_argument("--url", default="", help="Classroom API root (default "
                    "$AITHER_CLASSROOM_URL, else the tutor URL)")
    ts.add_argument("--no-signin", action="store_true", help="Do not sign in now")
    ts.add_argument("--keep-model", action="store_true",
                    help="Keep the model already chosen instead of local Bonsai")
    tg = tes.add_parser("start", help="One click: setup, start at logon, start now, and "
                                      "open the Classroom page already connected")
    tg.add_argument("--url", default="", help="Classroom API root (as `teach setup`)")
    tg.add_argument("--page", default="", help="The page to open (default the Aither "
                    "Classroom agent page; its origin must be allowed to pair)")
    tg.add_argument("--no-signin", action="store_true", help="Do not sign in now")
    tg.add_argument("--no-open", action="store_true", help="Do not open a browser")
    tg.add_argument("--no-autostart", action="store_true",
                    help="Do not start the agent at logon")
    tes.add_parser("stop", help="Stop this home's running agent (what the remover runs "
                                "before it deletes the toolkit)")
    tg.add_argument("--json", action="store_true", help="Print the result as JSON")

    c = hs.add_parser("chat", help="Send one message to your agent")
    c.add_argument("message")
    c.add_argument("--native", action="store_true",
                   help="The general agent (file and shell tools) instead of your Hearth "
                   "agent (calendar, to-do, mail, reminders, web)")

    from .planner_cli import build_parsers as _planner_parsers

    _planner_parsers(hs)

    sv = hs.add_parser("serve", help="Answer YOUR relay DMs with your agent (owner only; "
                                     "token from $AITHER_RELAY_TOKEN or `adk relay provision`)")
    sv.add_argument("--nick", default="", help="The agent's relay nick (default: its name)")
    sv.add_argument("--relay-url", default="", help="Relay API root (default "
                    "$AITHER_RELAY_URL or https://relay.aitherium.com/api/relay/v1)")
    sv.add_argument("--pair", action="store_true",
                    help="Print a 6-digit code; the first DM that is exactly that code, "
                    "from a registered relay account, becomes the owner")
    sv.add_argument("--poll", type=float, default=4.0, help="Seconds between polls")
    sv.add_argument("--channels", default="",
                    help="Comma-separated: " + ", ".join(CHANNELS) + ". Default: relay "
                    "when a relay token exists, plus every channel whose credentials are "
                    "in the environment (`adk home channels` shows which). Pair one by "
                    "sending `pair <channel>` from a bound channel")
    sv.add_argument("--no-local", action="store_true",
                    help="Do not open the local channel (127.0.0.1:$HEARTH_LOCAL_PORT, "
                    "default 8363) that `adk home say` and awsh /hearth talk to")
    sv.add_argument("--browser", action="store_true",
                    help="Print a one-time code a web page (hearth.aitherium.com, "
                    "aitherium.com, academy.aitherium.com; $HEARTH_BROWSER_ORIGINS) "
                    "exchanges to chat with this "
                    "serve over the local channel. Later codes: `adk home connect-browser`")
    svi = sv.add_mutually_exclusive_group()
    svi.add_argument("--install", action="store_true",
                     help="Start this serve (with these --channels/--nick/--relay-url/"
                     "--poll/--no-local) at every logon: a no-window scheduled task on "
                     "Windows, a systemd --user unit on Linux, a launchd agent on macOS")
    svi.add_argument("--uninstall", action="store_true",
                     help="Remove the logon autostart that --install created")
    sv.add_argument("--dry-run", action="store_true",
                    help="With --install: print what would be registered, change nothing")

    sy = hs.add_parser("say", help="Send one message to the RUNNING `adk home serve` over "
                                   "the local channel (no second agent)")
    sy.add_argument("text", help="What to say ('-' reads stdin); `yes <nonce>` answers a card")
    sy.add_argument("--port", type=int, default=None,
                    help="Local channel port (default $HEARTH_LOCAL_PORT or 8363)")
    sy.add_argument("--json", action="store_true", help="Machine-readable output")

    ev = hs.add_parser("events", help="Stream what the running serve sends you locally "
                                      "(replies, follow-ups)")
    ev.add_argument("-n", type=int, default=0, help="Stop after N events (0 = forever)")
    ev.add_argument("--port", type=int, default=None,
                    help="Local channel port (default $HEARTH_LOCAL_PORT or 8363)")
    ev.add_argument("--json", action="store_true", help="One JSON object per line")

    cb = hs.add_parser("connect-browser", help="One-time code for a web page to chat with "
                                               "the RUNNING serve (your own model)")
    cb.add_argument("--port", type=int, default=None,
                    help="Local channel port (default $HEARTH_LOCAL_PORT or 8363)")
    cb.add_argument("--json", action="store_true", help="Machine-readable output")

    chs = hs.add_parser("channels", help="Each channel: available, configured, bound "
                                         "owner (masked), preferred")
    chs.add_argument("--json", action="store_true", help="Machine-readable output")

    rc = hs.add_parser("receipts", help="The signed log of what the agent did")
    rc.add_argument("--verify", action="store_true",
                    help="Check chain + signatures (+ the anchor, when one exists): "
                    "exit 0 intact, 1 tampered or truncated, 2 cannot judge")
    rc.add_argument("--anchor", action="store_true",
                    help="Sign the current chain head (seq, sha256) into "
                    "<home>/receipts.anchor so a later --verify catches tail truncation")
    rc.add_argument("-n", type=int, default=10, help="How many recent receipts to show")
    rc.add_argument("--json", action="store_true", help="Machine-readable output")

    tr = hs.add_parser("trust", help="Trust profile: egress guard, approvals, receipts")
    trs = tr.add_subparsers(dest="trust_command")
    trs.add_parser("status", help="What is enforced right now")
    ti = trs.add_parser("init", help="Write air_gap.yaml with enforcement: audit")
    ti.add_argument("--force", action="store_true", help="Overwrite an existing air_gap.yaml")

    rp = hs.add_parser("report", help="Device-signed data-boundary report: model boundary, "
                                      "egress policy and events, receipts verdict")
    rp.add_argument("--pdf", default="", help="Write the PDF here (needs `pip install "
                    "'awdk[pdf]'`); the signed JSON is written beside it as FILE.json")
    rp.add_argument("--out", default="", help="Write the signed JSON here")
    rp.add_argument("--days", type=int, default=30, help="Window: the last N days")
    rp.add_argument("--json", action="store_true", help="Print the signed report as JSON")
    rp.add_argument("--verify", default="", metavar="FILE",
                    help="Verify a report JSON: exit 0 valid, 1 tampered, 2 cannot judge")
    rp.add_argument("--pubkey", default="",
                    help="Hex Ed25519 public key to trust when verifying another "
                    "device's report")

    j = hs.add_parser("join", help="Join a game: observe, act, chat, learn")
    _game_args(j)
    j.add_argument("--say", default="", help="Say this in the room")
    j.add_argument("--act", default="", help="Take this one action")
    j.add_argument("--steps", type=int, default=0, help="Play this many steps")
    j.add_argument("--sessions", type=int, default=1)
    j.add_argument("--learn", action="store_true",
                   help="Keep what it learns across sessions (agent-home pack)")
    j.add_argument("--agents", type=int, default=1,
                   help="Agents to send in (more than 1 needs the agent-home pack)")
    j.add_argument("--policy", choices=["learned", "model"], default="learned",
                   help="learned = values + curiosity; model = ask your model")
    j.add_argument("--epsilon", type=float, default=0.1)
    j.add_argument("--seed", type=int, default=None)

    e = hs.add_parser("enroll", help="World-model enrollment of a game room (pack)")
    _game_args(e)
    e.add_argument("--episodes", type=int, default=5)
    e.add_argument("--budget", type=int, default=20)


def _game_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("url", help="Game URL, e.g. saga+http://127.0.0.1:8793?world=elysium")
    p.add_argument("--token", default="", help="Room token (or AITHER_GAME_TOKEN)")
    p.add_argument("--kind", default=None, help="Force the client: saga | aither-game")
    p.add_argument("--json", action="store_true", help="Machine-readable output")


# ── helpers ──────────────────────────────────────────────────────────────────

def _emit(data: Any, as_json: bool, text: Optional[str] = None) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(text if text is not None else json.dumps(data, indent=2, default=str))


def _cfg() -> hc.HomeConfig:
    return hc.load_config()


def _read_arg(value: str) -> str:
    return sys.stdin.read() if value == "-" else value


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_init(args: argparse.Namespace) -> int:
    existed = hc.is_initialized()
    cfg = hc.init_home(name=args.name, force=args.force)
    where = hc.home_dir()
    if existed and not args.force:
        print(f"Agent Home already set up at {where} (use --force to reset).")
    else:
        print(f"Agent Home ready at {where}\n"
              f"  agent:   {cfg.name}\n"
              f"  persona: {where / 'persona'} (edit these files freely)\n"
              "Next: pick a model. Private, on this machine (CPU or GPU):\n"
              f"  1. {models.bonsai_install_hint()}\n"
              "  2. adk home model --local bonsai --check\n"
              "Or bring your own key: `adk home model --byo anthropic` (with "
              "ANTHROPIC_API_KEY set). Then `adk home chat \"hello\"`.\n"
              "Chat from a web page: `adk home serve --browser` prints a one-time code; "
              "type it into \"Your computer\" on https://hearth.aitherium.com.")
    if not existed or args.force or getattr(args, "calendar_ics", ""):
        from .planner_cli import offer_calendar

        return offer_calendar(getattr(args, "calendar_ics", ""))
    return EXIT_OK


def cmd_persona(args: argparse.Namespace) -> int:
    _cfg()
    if args.action == "path":
        print(hc.persona_dir())
        return EXIT_OK
    if args.action == "set":
        if not args.file:
            print("usage: adk home persona set <file> <text|->", file=sys.stderr)
            return EXIT_SETUP
        p = hc.write_persona_file(args.file, _read_arg(args.text))
        print(f"wrote {p}")
        return EXIT_OK
    for fname, text in hc.read_persona().items():
        print(f"── {fname} ──\n{text.rstrip()}\n")
    return EXIT_OK


def cmd_model(args: argparse.Namespace) -> int:
    cfg = _cfg()
    provider = args.local or args.byo
    if provider == "bonsai2":
        rc = _bonsai2(args)
        if rc is not None:
            return rc
    if provider:
        cfg.model = models.choose_model(provider, model=args.model,
                                        base_url=args.base_url,
                                        api_key_env=args.key_env)
        hc.save_config(cfg)
    info = models.describe(cfg.model)
    if args.check:
        info["probe"] = models.probe(cfg.model)
    _emit(info, False)
    if provider and cfg.model.mode == "byo" and not info["api_key_present"]:
        print(f"note: set {cfg.model.api_key_env} in your environment before use")
    return EXIT_OK if not args.check or info["probe"]["ok"] else EXIT_FAIL


def _bonsai2(args: argparse.Namespace) -> Optional[int]:
    """Install + start Bonsai 2; None means 'now save the preset'."""
    from . import bonsai2

    root = bonsai2.data_dir(getattr(args, "data_dir", ""))
    if getattr(args, "stop", False):
        print(bonsai2.stop(root))
        return EXIT_OK
    port = getattr(args, "port", 0) or bonsai2.DEFAULT_PORT
    try:
        out = bonsai2.install_and_start(
            quant=getattr(args, "quant", "auto"), backend=getattr(args, "backend", "auto"),
            port=port, ctx=getattr(args, "ctx", 0), root_override=getattr(args, "data_dir", ""),
            dry_run=getattr(args, "dry_run", False))
    except hc.HomeError as exc:
        print(f"bonsai2: {exc}", file=sys.stderr)
        return EXIT_FAIL
    if getattr(args, "dry_run", False):
        _emit(out, False)
        return EXIT_OK
    args.base_url = args.base_url or out["base_url"]
    # serve restarts the server after a reboot; it needs to know where it lives.
    (hc.home_dir() / "bonsai2.json").write_text(
        json.dumps({"data_dir": out["data_dir"]}), encoding="utf-8")
    return None


def _bonsai2_ensure(cfg: hc.HomeConfig) -> None:
    """serve after a reboot: restart our recorded Bonsai 2 server (no downloads)."""
    if cfg.model.provider != "bonsai2":
        return
    from . import bonsai2

    try:
        pointer = json.loads((hc.home_dir() / "bonsai2.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pointer = {}
    try:
        bonsai2.ensure_running(bonsai2.data_dir(pointer.get("data_dir", "")), print)
    except hc.HomeError as exc:
        print(f"bonsai2: {exc}", file=sys.stderr)


def cmd_voice(args: argparse.Namespace) -> int:
    """``adk home voice``: install the Aither voice and/or speak with it locally."""
    from . import aither_voice_runtime as rt
    from . import voice

    directory = getattr(args, "dir", "") or ""
    text = getattr(args, "say", "") or ""
    try:
        if getattr(args, "local", ""):
            out = voice.install(args.local, directory)
            print(f"Ready: {out['model']} (size and sha256 verified)")
            if not text:
                print('  next: adk home voice --say "Welcome to Aitherium."')
        if text:
            secs = voice.say_to_file(text, Path(args.output), directory,
                                     speed=float(getattr(args, "speed", 1.0) or 1.0))
            print(f"Wrote {args.output} ({secs:.2f} s, Aither voice, on this machine)")
    except rt.RuntimeMissingError as exc:
        print(f"voice: {exc}; install it with: {voice.EXTRA_HINT}", file=sys.stderr)
        return EXIT_SETUP
    except ValueError as exc:
        print(f"voice: {exc}", file=sys.stderr)
        return EXIT_SETUP
    except (voice.VoiceInstallError, rt.VoiceError) as exc:
        print(f"voice: {exc}", file=sys.stderr)
        return EXIT_FAIL
    if not getattr(args, "local", "") and not text:
        _emit(voice.status(directory), False)
    return EXIT_OK


def cmd_harness(args: argparse.Namespace) -> int:
    cfg = _cfg()
    if args.kind:
        cfg.harness = harness.choose_harness(args.kind, mcp_url=args.mcp_url)
        hc.save_config(cfg)
        out = harness.connect_harness(cfg)
        _emit(out, False)
        return EXIT_OK
    _emit({"current": cfg.harness.kind,
           "available": {k: harness.harness_status(k) for k in harness.HARNESSES}},
          False)
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    out: Dict[str, Any] = {"home": str(hc.home_dir()),
                           "initialized": hc.is_initialized(),
                           "license": entitlement.status()}
    if out["initialized"]:
        cfg = _cfg()
        out["name"] = cfg.name
        out["model"] = models.describe(cfg.model)
        out["harness"] = harness.harness_status(cfg.harness.kind)
        games = sorted(p.stem for p in (hc.home_dir() / "games").glob("*.json"))
        out["games_learned"] = games
    _emit(out, False)
    return EXIT_OK


def cmd_signin(args: argparse.Namespace) -> int:
    """Device-flow sign-in; the account's license (and so its packs) follows."""
    from adk import account_license
    from adk import cli as adk_cli

    base = (getattr(args, "portal_url", "") or "").rstrip("/") or adk_cli._DEFAULT_IDENTITY_URL
    identity_url = adk_cli._resolve_identity_url(base)
    try:
        result = adk_cli._device_flow_login(identity_url, client_name="adk-home")
        adk_cli.complete_device_login(identity_url, result, sync=False)
    except RuntimeError as exc:
        print(f"sign-in failed: {exc}", file=sys.stderr)
        return EXIT_FAIL
    tier = account_license.save_account_license(str(result.get("license_key") or ""),
                                                str(result.get("tier") or ""))
    if not tier:
        account_license.sync_account_license(identity_url,
                                             str(result.get("access_token") or ""))
    _emit(entitlement.status(), False)
    return EXIT_OK


def cmd_teach(args: argparse.Namespace) -> int:
    """``adk home teach setup|start`` (see :mod:`adk.home.teach_setup`)."""
    from . import teach_setup

    if getattr(args, "teach_command", None) == "start":
        try:
            out = teach_setup.start(
                getattr(args, "url", ""),
                page=getattr(args, "page", "") or teach_setup.DEFAULT_PAGE,
                signin=not getattr(args, "no_signin", False),
                open_page=not getattr(args, "no_open", False),
                autostart=not getattr(args, "no_autostart", False),
                do_signin=lambda: cmd_signin(argparse.Namespace(portal_url="")))
        except hc.HomeError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_SETUP
        if getattr(args, "json", False):
            print(json.dumps(out, default=str))
        return EXIT_OK if out.get("ready") else EXIT_FAIL
    if getattr(args, "teach_command", None) == "stop":
        stopped = teach_setup.stop_serve()
        print("agent: stopped." if stopped else "agent: none was running.")
        return EXIT_OK if stopped else EXIT_FAIL
    if getattr(args, "teach_command", None) != "setup":
        print("usage: adk home teach setup [--url URL] [--no-signin] [--keep-model]\n"
              "       adk home teach start [--url URL] [--page URL] [--no-open]\n"
              "       adk home teach stop",
              file=sys.stderr)
        return EXIT_SETUP

    try:
        out = teach_setup.setup(
            getattr(args, "url", ""), signin=not getattr(args, "no_signin", False),
            keep_model=getattr(args, "keep_model", False),
            do_signin=lambda: cmd_signin(argparse.Namespace(portal_url="")))
    except hc.HomeError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_SETUP
    _emit(out, False)
    return EXIT_OK if out.get("signin") != "failed" else EXIT_FAIL


def cmd_license(args: argparse.Namespace) -> int:
    raw = _read_arg(args.license) if args.license else ""
    if not raw:
        _emit(entitlement.status(), False)
        return EXIT_OK
    try:
        res = entitlement.install_license(raw, replace=bool(getattr(args, "replace", False)))
    except ValueError as exc:  # includes LicenseWouldDropPacks: nothing was saved
        print(f"license not installed: {exc}", file=sys.stderr)
        return EXIT_SETUP
    _emit(res, False)
    return EXIT_OK


def build_chat_agent(cfg: hc.HomeConfig) -> Any:
    """The agent `adk home chat` talks to: the Hearth agent `serve` runs (calendar,
    to-do, mail, reminders, web), with the same ask-first policy.

    It was the general agent until 2026-10-01: that one holds no calendar tool, so
    a fresh home answered "I cannot access your calendar" to its first question.
    """
    from adk import approval

    from . import serve
    from .connector_tools import home_signed_in
    from .hearth import EGRESS_TOOLS
    from .planner import Planner

    serve.apply_approval_policy()
    agent = serve.build_serve_agent(cfg, serve.FollowupStore(),
                                    receipts_file=serve.receipts_path())
    # serve closes web egress once a turn has read third-party text (HearthCore's
    # taint guard). A one-shot chat has no core, so when this home can read any --
    # a subscribed calendar, a mailbox, a connected account -- web_fetch and
    # web_search ask first: a crafted event title must not be able to have the
    # model carry the calendar out in a URL.
    plan = Planner()
    if plan.subscriptions() or plan.mail_account() or home_signed_in():
        approval.set_runtime_gates(agent.name, EGRESS_TOOLS)
    return agent


def cmd_chat(args: argparse.Namespace) -> int:
    cfg = _cfg()
    native = bool(getattr(args, "native", False))
    agent = harness.build_native_agent(cfg) if native else build_chat_agent(cfg)
    from adk.games.learning import _run_coro

    import httpx

    from .planner_cli import chat_with_approvals

    try:
        resp = chat_with_approvals(agent, args.message, _run_coro)
    except httpx.HTTPError as exc:
        # A buyer's first chat with a mistyped key used to end in a 40-line
        # traceback (clean-machine run, 2026-09-30). Say what failed and what to do.
        print(_model_error(cfg.model, exc), file=sys.stderr)
        return EXIT_FAIL
    print(getattr(resp, "content", resp))
    return EXIT_OK


def _model_error(mc: hc.ModelConfig, exc: Exception) -> str:
    """One actionable line for a failed model call; never echoes a key value."""
    import httpx

    where = mc.base_url or f"the {mc.provider} API"
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403) and mc.mode == "byo":
            return (f"{where} rejected the key in ${mc.api_key_env or 'its key variable'} "
                    f"(HTTP {code}). Fix the key, then run `adk home chat` again.")
        return (f"{where} answered HTTP {code} for model {mc.model or '(default)'}. "
                "Check `adk home model --check` and the model name.")
    hint = models.PRESETS[mc.provider].hint if mc.provider in models.PRESETS else ""
    return (f"could not reach {where} ({type(exc).__name__}). "
            f"{hint or 'Check `adk home model --check`.'}").strip()


def _prose_fn(cfg: Optional[hc.HomeConfig]) -> Optional[Callable[[str, str], str]]:
    """Let the agent's own model write Saga prose when the server has none."""
    if cfg is None:
        return None
    holder: Dict[str, Any] = {}

    def _fn(context: str, message: str) -> str:
        from adk.games.learning import _run_coro
        from adk.llm.base import Message

        if "llm" not in holder:
            holder["llm"] = models.build_llm(cfg.model)
        resp = _run_coro(holder["llm"].chat([
            Message(role="system", content="You are the narrator of an "
                    "interactive story. Continue it in 2-4 vivid sentences."),
            Message(role="user", content=f"Story so far: {context}\n\n"
                    f"The player: {message}")]))
        return getattr(resp, "content", "") or ""

    return _fn


def _open(args: argparse.Namespace, name: str, cfg: Optional[hc.HomeConfig],
          client_kwargs: Optional[Dict[str, Any]] = None) -> Any:
    from adk.games import open_game, split_token
    from adk.games import saga as saga_mod

    kw = dict(client_kwargs or {})
    kind = args.kind
    is_saga = kind == "saga" or (kind is None
                                 and saga_mod._matches(split_token(args.url)[0]))
    if is_saga and "prose_fn" not in kw:  # prose_fn is a Saga-only kwarg
        kw["prose_fn"] = _prose_fn(cfg)
    return open_game(args.url, token=args.token or None, name=name, kind=kind, **kw)


def cmd_join(args: argparse.Namespace, client_kwargs: Optional[Dict[str, Any]] = None,
             state_dir: Any = None) -> int:
    from adk.games import GameError
    from adk.licensing import LicenseError

    cfg = _cfg() if hc.is_initialized() else None
    name = cfg.name if cfg else "agent-home"
    try:
        if args.agents > 1:
            entitlement.require("multi_agent")
        if args.learn:
            entitlement.require("game_learning")
    except LicenseError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_LICENSE

    reports: List[Dict[str, Any]] = []
    try:
        for n in range(max(1, args.agents)):
            agent_name = name if args.agents <= 1 else f"{name}-{n + 1}"
            with _open(args, agent_name, cfg, client_kwargs) as game:
                obs = game.join()
                result: Dict[str, Any] = {"agent": agent_name, "domain": game.domain,
                                          "joined": obs.to_dict()}
                if args.say:
                    result["chat"] = game.chat(args.say)
                if args.act:
                    step = game.act(args.act)
                    result["act"] = {"reward": step.reward, "done": step.done,
                                     "observation": step.observation.to_dict(),
                                     "info": step.info}
                if args.steps > 0:
                    result["sessions"] = _play(args, game, cfg, agent_name, state_dir)
                reports.append(result)
    except GameError as exc:
        print(f"game error: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except hc.HomeError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_SETUP

    if args.json:
        _emit(reports if len(reports) > 1 else reports[0], True)
    else:
        for r in reports:
            print(f"[{r['agent']}] joined {r['domain']}")
            print(f"  sees: {r['joined']['text'][:300]}")
            print(f"  can:  {', '.join(r['joined']['actions'][:8])}")
            if "chat" in r:
                print(f"  said: {args.say!r}")
            if "act" in r:
                print(f"  did {args.act!r}: reward {r['act']['reward']:.2f} -> "
                      f"{r['act']['observation']['text'][:200]}")
            for s in r.get("sessions", []):
                print(f"  session {s['session']}: {s['steps']} steps, reward "
                      f"{s['total_reward']}, surprise {s['mean_surprise']}, "
                      f"finished={s['done']}, remembered={s['persisted']}")
            last = (r.get("sessions") or [{}])[-1]
            if last.get("progress"):
                pr = last["progress"]
                print(f"  across {pr['sessions']} sessions: rewards "
                      f"{pr['reward_by_session']}, improving={pr['improving']}")
    return EXIT_OK


def _play(args: argparse.Namespace, game: Any, cfg: Optional[hc.HomeConfig],
          agent_name: str, state_dir: Any) -> List[Dict[str, Any]]:
    from adk.games.learning import GameLearner, game_memory, llm_policy

    policy = None
    if args.policy == "model":
        if cfg is None:
            raise hc.HomeError("--policy model needs `adk home init` + `adk home model`")
        policy = llm_policy(models.build_llm(cfg.model), hc.compose_system_prompt())
    memory = game_memory(agent_name) if args.learn else None
    learner = GameLearner(game, policy=policy, memory=memory, persist=args.learn,
                          state_dir=state_dir, epsilon=args.epsilon, seed=args.seed)
    out = [r.to_dict() for r in learner.play(sessions=args.sessions,
                                             budget=args.steps)]
    if args.learn and learner.model is not None and out:
        out[-1]["progress"] = learner.model.progress()
    return out


def cmd_enroll(args: argparse.Namespace) -> int:
    from adk.games.learning import enroll_game
    from adk.licensing import LicenseError

    try:
        res = enroll_game(args.url, token=args.token or None,
                          episodes=args.episodes, budget=args.budget,
                          **({"kind": args.kind} if args.kind else {}))
    except LicenseError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_LICENSE
    _emit(res, args.json)
    return EXIT_OK if res.get("ok") else EXIT_FAIL


# ── serve / receipts / trust ─────────────────────────────────────────────────

class ChannelSpec(NamedTuple):
    """Where a channel's transport lives and the env vars its credentials come from.

    ``env`` is a tuple of groups; each group is satisfied by ANY one of its names
    (the first is the one error messages name). ``factory`` is the module-level
    builder (``by_channel`` = it takes the channel name); without one the class's
    ``from_env()`` is used. ``keychain`` = (env var, module function) when that
    secret may live in the OS keychain instead. A transport class may override the
    groups with ``REQUIRED_ENV`` or answer for itself with ``is_configured()``.
    """

    module: str
    cls: str
    env: Tuple[Tuple[str, ...], ...]
    factory: str = ""
    by_channel: bool = False
    keychain: Tuple[str, str] = ("", "")


_WA = ("HEARTH_WA_TOKEN", "HEARTH_WA_APP_SECRET", "HEARTH_WA_VERIFY_TOKEN",
       "HEARTH_WA_PHONE_NUMBER_ID")
_TWILIO = ("HEARTH_TWILIO_ACCOUNT_SID", "HEARTH_TWILIO_AUTH_TOKEN", "HEARTH_TWILIO_FROM",
           "HEARTH_TWILIO_PUBLIC_URL")

#: Every channel ``adk home serve`` can speak, in display order.
CHANNEL_SPECS: Dict[str, ChannelSpec] = {
    "relay": ChannelSpec("relay", "RelayTransport", (("AITHER_RELAY_TOKEN",),)),
    "telegram": ChannelSpec("chat", "TelegramTransport",
                            (("HEARTH_TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN"),),
                            "build_chat_transport", True),
    "discord": ChannelSpec("chat", "DiscordTransport",
                           (("HEARTH_DISCORD_TOKEN", "DISCORD_BOT_TOKEN"),),
                           "build_chat_transport", True),
    "slack": ChannelSpec("chat", "SlackTransport",
                         (("HEARTH_SLACK_BOT_TOKEN", "SLACK_BOT_TOKEN"),
                          ("HEARTH_SLACK_APP_TOKEN", "SLACK_APP_TOKEN")),
                         "build_chat_transport", True),
    "email": ChannelSpec("mail", "EmailTransport",
                         (("HEARTH_IMAP_HOST",), ("HEARTH_IMAP_USER",),
                          ("HEARTH_MAIL_PASSWORD",), ("HEARTH_MAIL_AUTHSERV_ID",)),
                         keychain=("HEARTH_MAIL_PASSWORD", "_keychain_password")),
    "whatsapp": ChannelSpec("whatsapp", "WhatsAppTransport", tuple((n,) for n in _WA),
                            "build_whatsapp_transport"),
    "sms": ChannelSpec("twilio", "TwilioTransport", tuple((n,) for n in _TWILIO),
                       "build_twilio_transport"),
    # 127.0.0.1 only; its credential is <home>/local.token, not the environment.
    # On by default in serve (``--no-local`` turns it off), so it is not part of
    # the env-driven default scan below.
    "local": ChannelSpec("local", "LocalTransport", (), "build_local_transport"),
}
#: Channels ``serve`` adds by flag rather than by what the environment holds.
FLAG_CHANNELS = ("local",)
CHANNELS: Tuple[str, ...] = tuple(CHANNEL_SPECS)
TRANSPORTS_PKG = "adk.home.transports"
_BOT_URL_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def _relay_token() -> str:
    """``$AITHER_RELAY_TOKEN``, else the credential ``adk relay provision`` saved.

    Never an argv flag: a token on the command line lands in shell history and
    in every process listing on the box.
    """
    from adk.config import load_saved_config

    return (os.environ.get("AITHER_RELAY_TOKEN", "").strip()
            or str(load_saved_config().get("relay_token") or "").strip())


def _load_transport_class(channel: str) -> Tuple[Any, str]:
    """(class, "") or (None, why it is unavailable). Imported lazily, by module path."""
    spec = CHANNEL_SPECS[channel]
    mod_name = f"{TRANSPORTS_PKG}.{spec.module}"
    try:
        mod = importlib.import_module(mod_name)
    except ImportError as exc:
        missing = getattr(exc, "name", "") or ""
        if missing and missing != mod_name:
            return None, f"{mod_name} needs {missing!r} (not installed)"
        return None, f"{mod_name} is not installed"
    cls = getattr(mod, spec.cls, None)
    if cls is None:
        return None, f"{mod_name} has no {spec.cls}"
    return cls, ""


def _env_groups(channel: str, cls: Any = None) -> Tuple[Tuple[str, ...], ...]:
    groups = getattr(cls, "REQUIRED_ENV", None) if cls is not None else None
    if groups:
        return tuple((g,) if isinstance(g, str) else tuple(g) for g in groups)
    return CHANNEL_SPECS[channel].env


def _missing_env(channel: str, cls: Any = None) -> List[str]:
    """The env var to set for every unmet credential group ([] = configured)."""
    if channel == "relay":
        return [] if _relay_token() else ["AITHER_RELAY_TOKEN"]
    probe = getattr(cls, "is_configured", None) if cls is not None else None
    if callable(probe):
        try:
            if probe():
                return []
        except Exception as exc:  # noqa: BLE001 - a broken probe falls back to the env check
            logger.debug("hearth: %s credential probe failed: %s", channel, exc)
    missing = [g[0] for g in _env_groups(channel, cls)
               if not any((os.environ.get(n) or "").strip() for n in g)]
    kc_env, kc_fn = CHANNEL_SPECS[channel].keychain
    if kc_env in missing and cls is not None and _keychain_has(cls, kc_fn):
        missing.remove(kc_env)
    return missing


def _keychain_has(cls: Any, fn_name: str) -> bool:
    """Does the transport module's keychain reader find the secret? (Value discarded.)"""
    fn = getattr(sys.modules.get(getattr(cls, "__module__", "")), fn_name, None)
    try:
        return callable(fn) and bool(fn())
    except Exception:  # noqa: BLE001 - no keyring = not configured
        return False


def _channel_status(channel: str) -> Dict[str, Any]:
    cls, why = _load_transport_class(channel)
    missing = _missing_env(channel, cls)
    return {"channel": channel, "available": cls is not None, "unavailable": why,
            "configured": not missing, "missing_env": missing, "cls": cls}


def _secret_values() -> List[str]:
    names = {n for spec in CHANNEL_SPECS.values() for g in spec.env for n in g}
    vals = [(os.environ.get(n) or "").strip() for n in names]
    return sorted((v for v in vals if len(v) >= 6), key=len, reverse=True)


def _scrub(text: Any, limit: int = 240) -> str:
    """An error message with every channel credential (and bot-URL token) masked."""
    out = str(text)
    for v in _secret_values():
        out = out.replace(v, "***")
    out = _BOT_URL_RE.sub("bot***", out)
    return out if len(out) <= limit else out[:limit] + "..."


def mask_id(value: Any) -> str:
    """Enough of an owner id to recognise it, never enough to reuse it."""
    v = str(value or "")
    if not v:
        return ""
    return "***" if len(v) < 6 else f"{v[:2]}***{v[-2:]}"


def _auto_configured(channel: str, cls: Any) -> bool:
    """Configured through its OWN ``HEARTH_*`` names (plus the relay token).

    The generic aliases (``TELEGRAM_BOT_TOKEN``, ``DISCORD_BOT_TOKEN``,
    ``SLACK_BOT_TOKEN``, ...) often belong to some other bot on the box; they
    satisfy an explicit ``--channels``, but never pull a channel into the default
    selection (a second poller would steal that bot's updates).
    """
    if channel == "relay":
        return bool(_relay_token())
    kc_env, kc_fn = CHANNEL_SPECS[channel].keychain
    for group in _env_groups(channel, cls):
        own = [n for n in group if n.startswith("HEARTH_")] or list(group)
        if any((os.environ.get(n) or "").strip() for n in own):
            continue
        if kc_env in own and cls is not None and _keychain_has(cls, kc_fn):
            continue
        return False
    return True


def _select_channels(requested: str) -> Tuple[List[str], bool]:
    """(channels, explicit). Default = relay if a token exists + every channel set up
    through its ``HEARTH_*`` env vars (never through a generic ``*_BOT_TOKEN``)."""
    names = [c for c in dict.fromkeys(x.strip().lower() for x in (requested or "").split(","))
             if c]
    if names:
        unknown = [c for c in names if c not in CHANNEL_SPECS]
        if unknown:
            raise hc.HomeError(f"unknown channel {unknown[0]!r} (known: {', '.join(CHANNELS)})")
        return names, True
    return [c for c in CHANNELS if c not in FLAG_CHANNELS
            and _auto_configured(c, _load_transport_class(c)[0])], False


def _build_transport(channel: str, cls: Any) -> Any:
    """The module's factory when the spec names one, else ``cls.from_env()``/``cls()``."""
    spec = CHANNEL_SPECS[channel]
    factory = (getattr(sys.modules.get(cls.__module__), spec.factory, None)
               if spec.factory else None)
    if callable(factory):
        return factory(channel) if spec.by_channel else factory()
    from_env = getattr(cls, "from_env", None)
    return from_env() if callable(from_env) else cls()


async def _run_hearth(core: Any, tick_interval: float) -> int:
    """Start every transport CONCURRENTLY, report each, then fire follow-ups forever.

    A channel that fails to start is reported (credentials scrubbed) and dropped;
    the rest keep serving. Nothing started = EXIT_FAIL.
    """
    import asyncio

    from .hearth import _maybe_await

    names = list(core.transports)
    results = await asyncio.gather(*(core.transports[n].start(core) for n in names),
                                   return_exceptions=True)
    up: List[str] = []
    for name, res in zip(names, results):
        if isinstance(res, BaseException):
            if not isinstance(res, Exception):
                raise res
            print(f"  {name:<9} FAILED: {type(res).__name__}: {_scrub(res)}", file=sys.stderr)
            dead = core.transports.pop(name)
            try:
                await _maybe_await(dead.stop())
            except Exception as exc:  # noqa: BLE001 - it never started; nothing to leak
                logger.debug("hearth: stopping failed %s: %s", name, exc)
        else:
            up.append(name)
            print(f"  {name:<9} up")
    if not up:
        print("no channel started", file=sys.stderr)
        return EXIT_FAIL
    core._running = True
    try:
        while core._running:
            try:
                await core.fire_due()
            except Exception as exc:  # noqa: BLE001 - one bad tick is not fatal
                print(f"follow-up tick failed (continuing): {_scrub(exc)}", file=sys.stderr)
            await asyncio.sleep(tick_interval)
    finally:
        await core.stop()
    return EXIT_OK


def _prepare_channels(args: argparse.Namespace, local: bool = False
                      ) -> Tuple[List[str], Dict[str, Any], int]:
    """Pick the channels and build every non-relay transport BEFORE the agent exists.

    ``local`` adds the loopback ``local`` channel to the DEFAULT selection (an
    explicit ``--channels`` list is taken as given). Returns (names, transports,
    exit code); a non-zero code has been reported.
    """
    try:
        names, explicit = _select_channels(args.channels)
    except hc.HomeError as exc:
        print(str(exc), file=sys.stderr)
        return [], {}, EXIT_SETUP
    if local and not explicit and "local" not in names:
        names.append("local")
    chosen: List[str] = []
    built: Dict[str, Any] = {}
    for name in names:
        st = _channel_status(name)
        if not st["available"]:
            if explicit:
                print(f"{name}: unavailable -- {st['unavailable']}", file=sys.stderr)
                return [], {}, EXIT_SETUP
            print(f"  {name:<9} skipped: {st['unavailable']}", file=sys.stderr)
            continue
        if st["missing_env"]:  # only reachable for an explicit request
            if name == "relay":
                print("relay: no relay credential -- run `adk relay provision <nick>` or "
                      "set AITHER_RELAY_TOKEN.", file=sys.stderr)
            else:
                print(f"{name}: missing credentials -- set "
                      f"{', '.join(st['missing_env'])} in the environment", file=sys.stderr)
            return [], {}, EXIT_SETUP
        if name != "relay":
            try:
                built[name] = _build_transport(name, st["cls"])
            except Exception as exc:  # noqa: BLE001 - reported without the credential
                if not explicit:
                    # Picked up from the env, not asked for: never block the rest.
                    print(f"  {name:<9} skipped: {_scrub(exc)}", file=sys.stderr)
                    continue
                code = EXIT_LICENSE if type(exc).__name__ == "LicenseError" else EXIT_SETUP
                print(f"{name}: cannot attach -- {_scrub(exc)}", file=sys.stderr)
                return [], {}, code
        chosen.append(name)
    if not chosen:
        print("No channel is configured: run `adk relay provision <nick>` or set "
              "AITHER_RELAY_TOKEN, or give a channel its credentials "
              "(`adk home channels` lists them).", file=sys.stderr)
        return [], {}, EXIT_SETUP
    return chosen, built, EXIT_OK


#: The logon autostart's name: Windows task, systemd --user unit, launchd label suffix.
SERVE_AUTOSTART = "aither-hearth"
#: Non-secret settings an installed serve inherits from the installing shell. Tokens
#: are NOT copied: they would sit in plain text in the task/unit.
_SERVE_ENV_PASSTHROUGH = (hc.HOME_ENV, "AITHER_RECEIPTS_PATH", "HEARTH_LOCAL_PORT",
                          "AITHER_RELAY_URL")


def serve_autostart_argv(args: argparse.Namespace) -> List[str]:
    """The command the logon entry runs: this serve's options, minus the one-shot ones.

    ``--pair`` is dropped (a pairing code printed into a log nobody reads is an open
    door); pair once interactively, then install.
    """
    argv = [sys.executable, "-m", "adk.home", "serve"]
    for flag, value in (("--channels", getattr(args, "channels", "")),
                        ("--nick", getattr(args, "nick", "")),
                        ("--relay-url", getattr(args, "relay_url", ""))):
        if value:
            argv += [flag, str(value)]
    poll = getattr(args, "poll", 4.0)
    if poll != 4.0:
        argv += ["--poll", str(poll)]
    if getattr(args, "no_local", False):
        argv.append("--no-local")
    return argv


def _serve_autostart(args: argparse.Namespace) -> int:
    from adk import agent_daemon

    if args.uninstall:
        ok = agent_daemon.remove_user_autostart(SERVE_AUTOSTART)
        print(f"{SERVE_AUTOSTART} autostart " + ("removed." if ok else "NOT fully removed."),
              file=sys.stdout if ok else sys.stderr)
        return EXIT_OK if ok else EXIT_FAIL
    if getattr(args, "pair", False):
        print("note: --pair is not installed (the code would only reach a log); pair "
              "once with `adk home serve --pair`, then --install.", file=sys.stderr)
    env = {k: os.environ[k] for k in _SERVE_ENV_PASSTHROUGH if os.environ.get(k)}
    argv = serve_autostart_argv(args)
    where = agent_daemon.install_user_autostart(
        SERVE_AUTOSTART, argv, description="Aither Hearth: adk home serve", env=env,
        dry_run=bool(getattr(args, "dry_run", False)))
    if not where:
        print(f"{SERVE_AUTOSTART} autostart NOT installed (see the message above).",
              file=sys.stderr)
        return EXIT_FAIL
    print(f"{SERVE_AUTOSTART} autostart: {where}\n  runs:  {' '.join(argv)}\n"
          f"  log:   {agent_daemon.LOG_DIR / (SERVE_AUTOSTART + '.log')}\n"
          "  Channel tokens held only in this shell's environment are not copied; keep "
          "them in the keychain/config the serve reads. Undo: adk home serve --uninstall")
    return EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    import asyncio

    from . import serve
    from .hearth import HearthCore

    if getattr(args, "install", False) or getattr(args, "uninstall", False):
        return _serve_autostart(args)
    cfg = _cfg()
    _bonsai2_ensure(cfg)
    serve.apply_a2a_trust_default()
    egress_path, egress_written = ensure_egress_audit()
    names, built, rc = _prepare_channels(args, local=not getattr(args, "no_local", False))
    if rc:
        return rc
    if getattr(args, "browser", False) and "local" not in names:
        print("--browser needs the local channel (drop --no-local, or add `local` to "
              "--channels)", file=sys.stderr)
        return EXIT_SETUP
    owner = serve.load_owner()
    # The local channel binds the OS user on its first valid-token request.
    if (not owner and not args.pair and "local" not in names
            and not serve.OwnerRegistry(serve.owner_path()).owners):
        print("No owner is bound yet: start with `adk home serve --pair` and send the "
              "code it prints from your own account.", file=sys.stderr)
        return EXIT_SETUP
    always_ask = serve.apply_approval_policy()
    store = serve.FollowupStore()
    agent = serve.build_serve_agent(cfg, store, receipts_file=serve.receipts_path())
    nick = args.nick or cfg.name
    url = (args.relay_url or os.environ.get("AITHER_RELAY_URL", "")
           or serve.DEFAULT_RELAY_URL)
    code = serve.new_pair_code() if args.pair else ""
    client: Any = None
    if "relay" in names:
        client = serve.OwnerRelayClient(url, _relay_token(), nick, agent, owner_nick=owner,
                                        pair_code=code, store=store,
                                        poll_interval=args.poll)
        core = client.core
    else:
        core = HearthCore(agent, store, serve.receipts_path(), pair_code=code)
    for transport in built.values():
        core.add_transport(transport)
    relay_only = list(core.transports) == ["relay"]
    head = f"{nick} serving on {url}" if client is not None else f"{nick} serving"
    owners = (core.registry.owners if relay_only else
              {c: mask_id(u) for c, u in core.registry.owners.items()})
    print(f"{head}\n"
          f"  owners:   {owners or '(not paired)'}\n"
          f"  channels: {', '.join(core.transports)}\n"
          f"  tools:    {', '.join(serve.tool_names(agent))}\n"
          f"  asks:     {', '.join(always_ask)}\n"
          f"  egress:   {egress_state(egress_path, egress_written)}\n"
          f"  a2a:      AITHER_A2A_REQUIRE_TRUST={os.environ.get(serve.A2A_TRUST_ENV)}\n"
          f"  receipts: {core.receipts_file}")
    local_t = core.transports.get("local")
    if local_t is not None:
        print(f"  local:    http://127.0.0.1:{getattr(local_t, 'port', '?')} "
              f"(`adk home say`, awsh /hearth; token in {hc.home_dir() / 'local.token'})")
    if local_t is not None and getattr(args, "browser", False):
        print(_browser_code_text(*local_t.new_browser_code(),
                                 list(local_t.browser.origins), getattr(local_t, "port", 0)))
    if code:
        where = ("on the relay: a REGISTERED account" if "relay" in names
                 else "any attached channel")
        print(f"  PAIRING CODE: {code}  -- send just these 6 digits to {nick} from your "
              f"own account on ONE attached channel ({where}); add more channels later "
              "with `pair <channel>`")
    try:
        if relay_only:
            asyncio.run(client.run())
            return EXIT_OK
        return asyncio.run(_run_hearth(core, args.poll))
    except KeyboardInterrupt:
        return EXIT_OK
    except RuntimeError as exc:
        print(_scrub(exc), file=sys.stderr)
        return EXIT_FAIL


def _local_client(args: argparse.Namespace) -> Any:
    from .local_client import LocalClient

    return LocalClient(port=getattr(args, "port", None))


def _browser_code_text(code: str, ttl: float, origins: List[str], port: int) -> str:
    pages = " or ".join(origins) or "(none: HEARTH_BROWSER_ORIGINS=off)"
    return (f"  BROWSER CODE: {code}  -- type it into \"Your computer\" on {pages} "
            f"within {int(ttl // 60)} minutes (one use). The page then chats with this "
            f"serve on 127.0.0.1:{port or '?'}; your model answers, approvals still need "
            "your click, and a restart signs the page out.")


def cmd_connect_browser(args: argparse.Namespace) -> int:
    """Ask the running serve for a one-time browser pairing code and print it."""
    from .local_client import LocalClientError

    try:
        client = _local_client(args)
        data = client.browser_code()
    except LocalClientError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAIL
    if args.json:
        print(json.dumps(data))
        return EXIT_OK
    port = int(str(client.url).rsplit(":", 1)[-1]) if ":" in str(client.url) else 0
    print(_browser_code_text(str(data.get("code") or ""), float(data.get("expires_in") or 0),
                             list(data.get("origins") or []), port).strip())
    return EXIT_OK


def cmd_say(args: argparse.Namespace) -> int:
    """One message to the running serve over the local channel; print its replies."""
    from .local_client import LocalClientError, render_replies

    text = _read_arg(args.text).strip()
    if not text:
        print("nothing to say", file=sys.stderr)
        return EXIT_SETUP
    try:
        data = _local_client(args).say(text)
    except LocalClientError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAIL
    _emit(data, args.json, render_replies(data))
    return EXIT_OK


def cmd_events(args: argparse.Namespace) -> int:
    """Stream outbound local messages (replies and follow-ups) until -n or Ctrl-C."""
    from .local_client import LocalClientError, render_message

    try:
        for event in _local_client(args).events(limit=max(0, args.n)):
            if args.json:
                print(json.dumps(event, default=str), flush=True)
            else:
                print(f"[{event.get('kind', '?')}] {render_message(event)}", flush=True)
    except LocalClientError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAIL
    except KeyboardInterrupt:
        return EXIT_OK
    return EXIT_OK


def cmd_channels(args: argparse.Namespace) -> int:
    """Each channel: available, configured, bound owner (masked), preferred."""
    from .hearth import OwnerRegistry, owner_path

    reg = OwnerRegistry(owner_path())
    rows: List[Dict[str, Any]] = []
    for name in CHANNELS:
        st = _channel_status(name)
        st.pop("cls")
        st["owner"] = mask_id(reg.owner(name))
        st["preferred"] = bool(reg.owner(name)) and name == reg.preferred
        rows.append(st)
    if args.json:
        _emit(rows, True)
        return EXIT_OK
    print(f"{'channel':<9}  {'ready':<5}  {'configured':<10}  {'owner':<9}  preferred")
    for r in rows:
        ready = "yes" if r["available"] else "no"
        conf = "yes" if r["configured"] else "no"
        print(f"{r['channel']:<9}  {ready:<5}  {conf:<10}  {r['owner'] or '-':<9}  "
              f"{'*' if r['preferred'] else ''}")
        if not r["available"]:
            print(f"           unavailable: {r['unavailable']}")
        elif not r["configured"]:
            print(f"           set: {', '.join(r['missing_env'])}")
    return EXIT_OK


def cmd_receipts(args: argparse.Namespace) -> int:
    from adk import receipts

    from . import serve

    path = serve.receipts_path()
    if getattr(args, "anchor", False):
        try:
            anc = receipts.anchor(path)
        except receipts.AnchorError as exc:
            _emit({"code": exc.code, "reason": str(exc), "path": str(path)}, args.json,
                  str(exc))
            return exc.code
        where = receipts.anchor_path_for(path)
        _emit({"code": 0, "anchor": anc, "anchor_path": str(where)}, args.json,
              f"anchored seq {anc['seq']} sha256 {anc['sha256'][:16]}... "
              f"{'signed' if anc['signed'] else 'UNSIGNED'} -> {where}")
        return EXIT_OK
    if args.verify:
        code, reason = receipts.check(path)
        verdict = {0: "intact", 1: "TAMPERED"}.get(code, "cannot judge")
        _emit({"code": code, "reason": reason, "path": str(path)}, args.json,
              f"{verdict}: {reason}")
        return code
    rows = receipts.tail(max(1, args.n), path=path)
    if args.json:
        _emit(rows, True)
        return EXIT_OK
    if not rows:
        print(f"no receipts yet at {path}")
    for r in rows:
        print(f"#{r.get('seq')} {r.get('ts')} {r.get('kind')} {r.get('name')} "
              f"[{r.get('approval')}] {'signed' if r.get('signed') else 'UNSIGNED'}  "
              f"{r.get('result_preview', '')}")
    return EXIT_OK


AIR_GAP_AUDIT = """\
# Written by `adk home trust init`.
# audit  = every destination outside loopback is logged to audit.jsonl, and allowed.
# strict = anything outside allowed_subnets is refused before a byte is sent
#          (that breaks keyless web search and CDN-backed hosts -- list them first).
enabled: true
enforcement: audit
resolve_hostnames: true
"""


def air_gap_path() -> Path:
    """Where the egress guard reads its user config (``adk.compliance.air_gap``)."""
    explicit = os.environ.get("AITHER_AIR_GAP_CONFIG", "").strip()
    if explicit:
        return Path(explicit)
    base = os.environ.get("AITHER_DATA_DIR", "").strip()
    return (Path(base) if base else Path.home() / ".aither") / "air_gap.yaml"


def write_air_gap_audit(path: Optional[Path] = None) -> Path:
    """Write :data:`AIR_GAP_AUDIT` (enforcement: audit) -- `trust init` and serve's
    first start share this one writer."""
    path = path or air_gap_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(AIR_GAP_AUDIT, encoding="utf-8")
    return path


def ensure_egress_audit() -> Tuple[Path, bool]:
    """serve's first start: write the audit config when none exists and no env
    override decides. Returns (config path, written now)."""
    path = air_gap_path()
    if path.exists() or (os.environ.get("AITHER_AIR_GAP") or "").strip():
        return path, False
    return write_air_gap_audit(path), True


def egress_state(path: Path, wrote: bool) -> str:
    """One banner line: what the egress guard does for THIS process, honestly."""
    from adk.compliance import air_gap
    from adk.compliance._egress_guard import egress_guard_status

    status = egress_guard_status()
    if status["enforced"]:
        return f"{status['mode']} (guard installed; {status['config_path'] or path})"
    env = (os.environ.get("AITHER_AIR_GAP") or "").strip()
    if env:
        return f"AITHER_AIR_GAP={env}, guard NOT installed in this process"
    cfg, _digest, err = air_gap.AirGapEnforcer._read_layer(path)
    mode = str(cfg.get("enforcement") or "strict") if cfg and cfg.get("enabled") \
        else "disabled"
    if err:
        return f"config error in {path} ({err}); guard NOT installed"
    if wrote:
        return (f"{mode} -- wrote {path} now; the guard applies from the next start "
                "(this run is not audited)")
    return f"{mode} per {path}, guard NOT installed in this process"


def cmd_trust(args: argparse.Namespace) -> int:
    from . import serve

    sub = getattr(args, "trust_command", None) or "status"
    path = air_gap_path()
    if sub == "init":
        if path.exists() and not args.force:
            print(f"{path} already exists (use --force to overwrite it).")
            return EXIT_OK
        write_air_gap_audit(path)
        print(f"wrote {path} (enforcement: audit); new adk processes pick it up.")
        return EXIT_OK

    from adk import receipts
    from adk.compliance import air_gap
    from adk.compliance._egress_guard import egress_guard_status

    # Read the file, never construct an enforcer: constructing an enabled one
    # ACTIVATES it (flips AITHER_LLM_OFFLINE_MODE and friends, writes an audit
    # row) -- a status command must not change what it reports on.
    cfg, _digest, err = air_gap.AirGapEnforcer._read_layer(path)
    mode = "disabled"
    if cfg and cfg.get("enabled"):
        mode = str(cfg.get("enforcement") or "strict")
    code, reason = receipts.check(serve.receipts_path())
    _emit({
        "air_gap": {"config": str(path), "present": path.exists(), "mode": mode,
                    "env_override": os.environ.get("AITHER_AIR_GAP") or None,
                    "config_error": err},
        "egress_guard": egress_guard_status(),
        "owner": serve.load_owner() or None,
        "serve_tools": list(serve.SERVE_CATEGORIES) + ["life"],
        "always_ask": list(serve.ALWAYS_ASK),
        "receipts": {"path": str(serve.receipts_path()), "verify": code, "reason": reason},
    }, False)
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    from . import report as hr

    if args.verify:
        try:
            data = json.loads(Path(args.verify).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"cannot judge: {args.verify} unreadable ({type(exc).__name__})",
                  file=sys.stderr)
            return 2
        code, reason = hr.verify_report(data, pubkey=args.pubkey or None)
        verdict = {0: "valid", 1: "TAMPERED"}.get(code, "cannot judge")
        _emit({"code": code, "verdict": verdict, "reason": reason}, args.json,
              f"{verdict}: {reason}")
        return code

    from . import serve

    cfg = _cfg()
    rep = hr.build_report(cfg, serve.receipts_path(), air_gap_path(), days=args.days)
    written: List[str] = []
    json_out = args.out or (str(Path(args.pdf).with_suffix(".json")) if args.pdf else "")
    if args.pdf:
        try:
            pdf = hr.render_pdf(rep)
        except ImportError:
            print("PDF output needs fpdf2: pip install 'awdk[pdf]'", file=sys.stderr)
            return EXIT_SETUP
        Path(args.pdf).parent.mkdir(parents=True, exist_ok=True)
        Path(args.pdf).write_bytes(pdf)
        written.append(args.pdf)
    if json_out:
        Path(json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(json_out).write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
        written.append(json_out)
    if args.json or not written:
        _emit(rep, True)
        return EXIT_OK
    integ = rep["integrity"]
    rv = rep["receipts"]["verify"]
    signed = (f"Ed25519 key_id {integ['key_id']}" if integ["signed"]
              else "NO (no device key)")
    print(f"report {rep['report_id']} ({rep['window_start'][:10]} .. "
          f"{rep['window_end'][:10]})\n"
          f"  model:    {rep['model']['boundary']} -- {rep['model']['boundary_reason']}\n"
          f"  egress:   {rep['egress_policy']['mode']}, "
          f"{rep['egress_events_total']} event(s) outside policy\n"
          f"  receipts: {rv['verdict']} -- {rv['reason']}\n"
          f"  signed:   {signed}\n"
          f"  wrote:    {', '.join(written)}")
    return EXIT_OK


COMMANDS: Dict[str, Callable[[argparse.Namespace], int]] = {
    "init": cmd_init, "persona": cmd_persona, "model": cmd_model, "voice": cmd_voice,
    "harness": cmd_harness, "status": cmd_status, "license": cmd_license,
    "signin": cmd_signin, "teach": cmd_teach,
    "chat": cmd_chat, "join": cmd_join, "enroll": cmd_enroll,
    "serve": cmd_serve, "channels": cmd_channels, "receipts": cmd_receipts, "trust": cmd_trust,
    "say": cmd_say, "events": cmd_events, "report": cmd_report,
    "connect-browser": cmd_connect_browser,
    "calendar": lambda a: _planner("cmd_calendar", a),
    "todo": lambda a: _planner("cmd_todo", a),
    "connect": lambda a: _planner("cmd_connect", a),
}


def _planner(name: str, args: argparse.Namespace) -> int:
    """`adk home calendar|todo|connect` (adk.home.planner_cli); needs `adk home init`."""
    from . import planner_cli

    _cfg()
    return getattr(planner_cli, name)(args)


def cmd_home(args: argparse.Namespace) -> int:
    """Dispatch ``adk home <sub>``; the adk CLI calls ``sys.exit(cmd_home(args))``."""
    sub = getattr(args, "home_command", None)
    if not sub:
        print(__doc__)
        return EXIT_OK
    try:
        return COMMANDS[sub](args)
    except hc.HomeError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_SETUP


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="adk home", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    _build(p)
    return cmd_home(p.parse_args(argv))
