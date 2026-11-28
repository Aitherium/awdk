"""CLI entrypoints for ``adk bot <new|validate|dev|chat|verify|deploy>``."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Sequence

from adk.bots.deploy import deploy_bot, verify_bot_bundle
from adk.bots.manifest import (
    BOT_BACKENDS,
    BOT_STYLES,
    BOT_TARGET_SURFACES,
    scaffold_bot,
    slugify_bot_id,
)
from adk.bots.runtime import BotRuntime, create_bot_app
from adk.bots.validator import validate_bot
from adk.llm.base import Message


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adk bot",
        description="Scaffold, validate, run, chat with, and deploy declarative Aither bots.",
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    p_new = sub.add_parser("new", help="Scaffold a new Gemini-style or OpenAI-style bot")
    p_new.add_argument("name", help="Bot display name or id")
    p_new.add_argument("--style", choices=BOT_STYLES, default="gemini")
    p_new.add_argument("--dir", dest="target_dir", default=None, help="Output directory")
    p_new.add_argument("--backend", choices=BOT_BACKENDS, default="auto")
    p_new.add_argument("--model", default=None)
    p_new.add_argument("--instructions", default=None)
    p_new.add_argument("--description", default="")
    p_new.add_argument("--model-sha256", dest="model_sha256", default=None)
    p_new.add_argument("--shared-ro-mmap", dest="shared_ro_mmap", action="store_true")
    p_new.add_argument(
        "--target-surface",
        dest="target_surface",
        choices=BOT_TARGET_SURFACES,
        default="standard",
    )
    p_new.add_argument("--overwrite", action="store_true")

    p_val = sub.add_parser("validate", help="Static zero-execution BOT001-BOT008 check")
    p_val.add_argument("bot_dir", nargs="?", default=".")
    p_val.add_argument("--allow-local-action-urls", action="store_true")
    p_val.add_argument("--json", action="store_true", dest="as_json")

    p_dev = sub.add_parser("dev", help="Serve the bot locally with Playground UI")
    p_dev.add_argument("bot_dir", nargs="?", default=".")
    p_dev.add_argument("--host", default="127.0.0.1")
    p_dev.add_argument("--port", type=int, default=8095)
    p_dev.add_argument("--backend", default=None)
    p_dev.add_argument("--allow-local-actions", action="store_true")

    p_chat = sub.add_parser("chat", help="Send a single prompt to a bot directory")
    p_chat.add_argument("bot_dir", nargs="?", default=".")
    p_chat.add_argument("-m", "--message", required=True)
    p_chat.add_argument("--backend", default=None)
    p_chat.add_argument("--allow-local-actions", action="store_true")
    p_chat.add_argument("--json", action="store_true", dest="as_json")

    p_ver = sub.add_parser("verify", help="Verify an awseal-signed bot bundle (fail-closed)")
    p_ver.add_argument("bot_dir", nargs="?", default=".")
    p_ver.add_argument("--key", default=None, help="Expected 64-hex Ed25519 publisher key")
    p_ver.add_argument("--json", action="store_true", dest="as_json")

    p_dep = sub.add_parser("deploy", help="Package or deploy a bot (local|discord|fleet|awrun)")
    p_dep.add_argument("bot_dir", nargs="?", default=".")
    p_dep.add_argument(
        "--target", choices=("local", "discord", "fleet", "awrun"), default="local"
    )
    p_dep.add_argument("--out", dest="out_dir", default=None)
    p_dep.add_argument("--port", type=int, default=8095)
    p_dep.add_argument("--fleet-url", default="")
    p_dep.add_argument("--token", default="")
    p_dep.add_argument("--discord-token", default="")
    p_dep.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    p_dep.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    p_dep.add_argument("--allow-local-action-urls", action="store_true")
    p_dep.add_argument("--json", action="store_true", dest="as_json")

    return parser


def bot_main(argv: Sequence[str] | None = None) -> int:
    """CLI handler for ``adk bot`` subcommands."""
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.verb == "new":
        dest = Path(args.target_dir) if args.target_dir else Path(slugify_bot_id(args.name))
        try:
            out = scaffold_bot(
                args.name,
                args.style,
                dest,
                backend=args.backend,
                model=args.model,
                instructions=args.instructions,
                description=args.description,
                overwrite=args.overwrite,
                model_sha256=args.model_sha256,
                shared_ro_mmap=args.shared_ro_mmap,
                target_surface=args.target_surface,
            )
        except (ValueError, FileExistsError) as exc:
            print(f"error: {exc}")
            return 1
        print(f"Scaffolded {args.style} bot at {out}")
        return 0

    if args.verb == "validate":
        rep = validate_bot(
            args.bot_dir, allow_local_action_urls=args.allow_local_action_urls
        )
        if args.as_json:
            print(
                json.dumps(
                    {
                        "ok": rep.ok,
                        "exit_code": rep.exit_code,
                        "bot_dir": str(rep.bot_dir),
                        "findings": [
                            {"code": f.code, "message": f.message} for f in rep.findings
                        ],
                    },
                    indent=2,
                )
            )
        else:
            if rep.ok:
                print(f"OK: {rep.bot_dir}")
            else:
                for f in rep.findings:
                    print(str(f))
        return rep.exit_code

    if args.verb == "dev":
        rep = validate_bot(
            args.bot_dir, allow_local_action_urls=args.allow_local_actions
        )
        if not rep.ok:
            for f in rep.findings:
                print(str(f))
            return rep.exit_code
        import uvicorn

        app = create_bot_app(
            args.bot_dir,
            backend=args.backend,
            allow_local_actions=args.allow_local_actions,
        )
        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    if args.verb == "chat":
        try:
            rt = BotRuntime(
                args.bot_dir,
                backend=args.backend,
                allow_local_actions=args.allow_local_actions,
            )
            turn = asyncio.run(rt.generate([Message(role="user", content=args.message)]))
        except Exception as exc:
            print(f"error: {exc}")
            return 1
        if args.as_json:
            print(
                json.dumps(
                    {
                        "text": turn.text,
                        "model": turn.model,
                        "finish_reason": turn.finish_reason,
                        "executed_tool_calls": turn.executed_tool_calls,
                        "knowledge_hits": [
                            {"source": h.source, "score": h.score}
                            for h in turn.knowledge_hits
                        ],
                    },
                    indent=2,
                )
            )
        else:
            print(turn.text)
        return 0

    if args.verb == "verify":
        ver = verify_bot_bundle(args.bot_dir, expect_key=args.key)
        if args.as_json:
            print(json.dumps(ver, indent=2))
        else:
            if ver["ok"]:
                print(
                    f"VERIFIED: {ver['bot_dir']} (key={ver.get('public_key', '')[:16]}...)"
                )
            else:
                reason = ver.get("error") or (
                    f"signature_ok={ver.get('signature_ok')} "
                    f"content_ok={ver.get('content_ok')} "
                    f"key_trusted={ver.get('key_trusted')}"
                )
                print(f"VERIFY FAILED: {reason}")
        return int(ver["exit_code"])

    if args.verb == "deploy":
        res = deploy_bot(
            args.bot_dir,
            target=args.target,
            out_dir=args.out_dir,
            port=args.port,
            dry_run=args.dry_run,
            fleet_url=args.fleet_url,
            token=args.token,
            discord_token=args.discord_token,
            allow_local_action_urls=args.allow_local_action_urls,
        )
        if args.as_json:
            print(
                json.dumps(
                    {
                        "ok": res.ok,
                        "target": res.target,
                        "bot_id": res.bot_id,
                        "style": res.style,
                        "output_path": str(res.output_path) if res.output_path else None,
                        "bundle_zip": str(res.bundle_zip) if res.bundle_zip else None,
                        "payload": res.payload,
                        "warnings": res.warnings,
                        "error": res.error,
                    },
                    indent=2,
                )
            )
        else:
            if res.ok:
                print(
                    f"Deployed {res.bot_id} ({res.style}) -> {res.target}"
                    + (f" at {res.output_path}" if res.output_path else "")
                )
            else:
                print(f"Deploy failed: {res.error}")
        return 0 if res.ok else 1

    return 1
