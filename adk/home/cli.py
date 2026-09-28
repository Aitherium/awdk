"""``adk home`` -- download, host and run your own agent.

    adk home init [--name NAME]                 create ~/.aither/agent-home
    adk home persona [show|path|set FILE TEXT]  view / edit the persona files
    adk home model --local bonsai|llamacpp|ollama | --byo deepseek|openai|anthropic
    adk home harness aither|claude|openclaw|hermes
    adk home status                             everything at a glance
    adk home signin                             Sign in with Aitherium: purchases unlock
    adk home license <file-or-text>             offline activation (pasted license)
    adk home chat "hello"                       one message to your agent
    adk home join <game-url> [--steps N] [--learn] [--say TEXT] [--act TEXT]
    adk home enroll <game-url>                  world-model enrollment (pack)

Also runnable without the adk CLI hook as ``python -m adk.home``.

Exit codes: 0 ok; 1 the game or model failed; 2 bad setup or arguments;
3 the feature needs the ``agent-home`` license pack.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable, Dict, List, Optional

from . import config as hc
from . import entitlement, harness, models

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

    c = hs.add_parser("chat", help="Send one message to your agent")
    c.add_argument("message")

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
              "Next: `adk home model --local bonsai` (or --byo deepseek), then "
              "`adk home join <game-url>`.")
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


def cmd_chat(args: argparse.Namespace) -> int:
    cfg = _cfg()
    agent = harness.build_native_agent(cfg)
    from adk.games.learning import _run_coro

    resp = _run_coro(agent.chat(args.message))
    print(getattr(resp, "content", resp))
    return EXIT_OK


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


COMMANDS: Dict[str, Callable[[argparse.Namespace], int]] = {
    "init": cmd_init, "persona": cmd_persona, "model": cmd_model,
    "harness": cmd_harness, "status": cmd_status, "license": cmd_license,
    "signin": cmd_signin,
    "chat": cmd_chat, "join": cmd_join, "enroll": cmd_enroll,
}


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
