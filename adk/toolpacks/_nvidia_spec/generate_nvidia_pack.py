#!/usr/bin/env python3
"""Generate the NVIDIA tool packs from _nvidia_spec/nvidia-components.yaml.

The spec is the single source of truth. Every pack except the hand-written
``nvidia_cuda`` OAuth/MCP client is rendered here, so a pack cannot drift from
the declaration that the manifest, the marketplace and the parity gate read.

    python generate_nvidia_pack.py --check           # report drift (exit 1)
    python generate_nvidia_pack.py --all --write     # render every buildable pack
    python generate_nvidia_pack.py --component nim --write
    python generate_nvidia_pack.py --component nim --check

A ``hand_written: true`` component (currently only ``nvidia_cuda``) is never
rendered by the default paths and is refused on an explicit ``--write`` unless
``--force`` is passed: it is real bespoke code, not something the spec can
derive, so regenerating it would replace it with a stub.

Exit 0 clean, 1 drift/violation, 2 cannot run (spec missing or unparseable).
Output is deterministic: the same spec renders byte-identical files.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from string import Template
from typing import Any

import yaml

_HERE = Path(__file__).resolve().parent
TOOLPACKS_DIR = _HERE.parent
REPO_ROOT = _HERE.parents[3]
SPEC_FILE = _HERE / "nvidia-components.yaml"

PACK_VERSION = "1.0.0"

INIT_TEMPLATE = Template(
    '''"""$display tool pack — auto-generated from the component spec.

Optional and fail-soft: with no NVIDIA credential the pack still imports and
every tool reports not_authenticated. Regenerate, never hand-edit.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("$log_prefix")

PACK_ID = "$pack_id"

_TOOL_NAMES = [
$tool_names
]


def register(registry) -> int:
    """Register every ${prefix}_* tool. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools, not a crash
        logger.warning("$pack_id pack unavailable (%s) — 0 tools registered", exc)
        return 0

    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("$pack_id: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool must not sink the pack
            logger.debug("$pack_id: skip tool %s: %s", name, exc)

    logger.info("$display pack registered %d tools", n)
    return n
'''
)

CONFIG_TEMPLATE = Template(
    '''"""$display pack configuration and credential resolution.

Resolution order for the NGC API key:

    NVIDIA_NGC_API_KEY | NGC_API_KEY   (environment override)
      -> $token_file under ~/.aither/  (0600, written by the installer)
      -> adk pack settings

The key is never hardcoded, never logged and never returned to the agent.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

logger = logging.getLogger("$log_prefix")

API_BASE = "$api_base"
TOKEN_FILE = Path.home() / ".aither" / "$token_file"
FIX = (
    "Set workspace secret $secret_name (a free key from build.nvidia.com) "
    "or export NVIDIA_NGC_API_KEY"
)


def api_key() -> str:
    """Return the NGC API key, or an empty string when none is configured."""
    for env in ("NVIDIA_NGC_API_KEY", "NGC_API_KEY"):
        value = os.environ.get(env, "").strip()
        if value:
            return value
    try:
        value = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError as exc:
        logger.debug("$log_prefix: token file unavailable: %s", exc)
    try:
        from adk.config import load_saved_config  # type: ignore

        settings = (load_saved_config().get("packs_settings") or {}).get("$pack_id") or {}
        value = str(settings.get("$secret_name") or "").strip()
        if value:
            return value
    except Exception as exc:  # noqa: BLE001 — the config store is optional context
        logger.debug("$pack_id: pack settings unavailable: %s", exc)
    return ""


def credential_source() -> str:
    """Name where the key came from, never the key itself."""
    for env in ("NVIDIA_NGC_API_KEY", "NGC_API_KEY"):
        if os.environ.get(env, "").strip():
            return "env:" + env
    try:
        if TOKEN_FILE.read_text(encoding="utf-8").strip():
            return "file:" + str(TOKEN_FILE)
    except OSError as exc:
        logger.debug("$log_prefix: token file unavailable: %s", exc)
    return "none"


def request(method: str, path: str, payload: dict | None, key: str) -> dict:
    """Call the hosted endpoint. Never raises; every failure is a dict."""
    url = API_BASE.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urlrequest.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urlrequest.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
        return {"status": "ok", "data": json.loads(body) if body else {}}
    except urlerror.HTTPError as exc:
        if exc.code in (401, 403):
            return {
                "error": "not_authenticated",
                "status_code": exc.code,
                "fix": "The NGC key was rejected; mint a new one at build.nvidia.com.",
            }
        return {"error": "request_failed", "status_code": exc.code, "message": exc.reason}
    except Exception as exc:  # noqa: BLE001 — a network failure is a status, not a crash
        return {"error": "request_failed", "message": str(exc)}
'''
)

TOOLS_HEADER = Template(
    '''"""$display tool pack — tool implementations.

Auto-generated from the component spec. Do not edit by hand: run the generator
and let --check report drift.
"""
from __future__ import annotations

import logging

from . import config

logger = logging.getLogger("$log_prefix")


def _guard() -> dict | None:
    """Return the not-authenticated envelope, or None when a key is present."""
    if config.api_key():
        return None
    return {"error": "not_authenticated", "fix": config.FIX}
'''
)

TOOL_TEMPLATE = Template(
    '''

$def_line
    """$description"""
    denied = _guard()
    if denied:
        return denied
$body
'''
)

CHAT_BODY = """    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    return config.request("POST", "/chat/completions", payload, config.api_key())"""

GET_BODY = """    return config.request("GET", "/models", None, config.api_key())"""

CONFIG_BODY = """    return {
        "status": "ok",
        "api_base": config.API_BASE,
        "credential_source": config.credential_source(),
    }"""

TOOLPACK_TEMPLATE = Template(
    """# Auto-generated from _nvidia_spec/nvidia-components.yaml — do not hand-edit.
id: $pack_id
name: $display
version: $version
description: $description_yaml
category: tool_packs
author: Aitherium
icon: $icon
tags: [$tags]
license_class: $license_class
requires_auth: $requires_auth
available: true
requires_nvaie: false
$extra_lines
mcp_tools:
  - "${prefix}_*"
tool_modules: []
entitlements: []
skills: []
"""
)

_ICONS = {"nim": "zap", "nvidia_nemotron": "route"}


def _pack_id(component: str) -> str:
    return component if component.startswith("nvidia_") else f"nvidia_{component}"


def _prefix(component: str) -> str:
    pid = _pack_id(component)
    return pid[len("nvidia_"):]


def _def_line(tool: dict[str, Any]) -> str:
    """Render a def line, wrapping arguments when the single line would be long."""
    name = tool["name"]
    parts = []
    for arg in tool.get("args") or []:
        arg_name = arg["name"]
        if "default" in arg:
            parts.append(f"{arg_name}={json.dumps(arg['default'])}")
        else:
            parts.append(arg_name)
    signature = ", ".join(parts)
    one_line = f"def {name}({signature}) -> dict:"
    if len(one_line) <= 99:
        return one_line
    wrapped = "\n".join(f"    {part}," for part in parts)
    return f"def {name}(\n{wrapped}\n) -> dict:"


def _body(tool: dict[str, Any]) -> str:
    name = tool["name"]
    if name.endswith("_chat"):
        return CHAT_BODY
    if name.endswith("_config"):
        return CONFIG_BODY
    return GET_BODY


def _tags(component: str) -> str:
    prefix = _prefix(component)
    return ", ".join(["nvidia", prefix, "toolpack"])


def _render(component: str, meta: dict[str, Any]) -> dict[str, str]:
    pack_id = _pack_id(component)
    prefix = _prefix(component)
    display = meta["display_name"]
    log_prefix = f"{prefix}_pack"
    token_file = meta.get("token_file", "nim_api_key")
    secret_name = meta.get("secret_name", "nim_api_key")
    api_base = meta.get("source_url", "https://integrate.api.nvidia.com/v1")

    tool_names = "\n".join(f'    "{t["name"]}",' for t in meta.get("tools") or [])
    init_py = INIT_TEMPLATE.substitute(
        display=display,
        log_prefix=log_prefix,
        pack_id=pack_id,
        tool_names=tool_names,
        prefix=prefix,
    )

    config_py = CONFIG_TEMPLATE.substitute(
        display=display,
        log_prefix=log_prefix,
        api_base=api_base,
        token_file=token_file,
        secret_name=secret_name,
        pack_id=pack_id,
    )

    tools_py = TOOLS_HEADER.substitute(display=display, log_prefix=log_prefix)
    for tool in meta.get("tools") or []:
        tools_py += TOOL_TEMPLATE.substitute(
            name=tool["name"],
            def_line=_def_line(tool),
            description=tool.get("description", ""),
            body=_body(tool),
        )

    extra_lines = ""
    if meta.get("self_hosted_requires_nvaie"):
        extra_lines = "self_hosted_requires_nvaie: true\n"
    toolpack = TOOLPACK_TEMPLATE.substitute(
        pack_id=pack_id,
        display=display,
        version=PACK_VERSION,
        description_yaml=json.dumps(meta.get("description", "")),
        icon=_ICONS.get(component, "cpu"),
        tags=_tags(component),
        license_class=meta.get("license_class", "free"),
        requires_auth=str(bool(meta.get("requires_auth"))).lower(),
        extra_lines=extra_lines,
        prefix=prefix,
    )

    return {
        "__init__.py": init_py,
        "config.py": config_py,
        "tools.py": tools_py,
        ".toolpack.yaml": toolpack,
    }


def load_spec() -> dict[str, Any]:
    if not SPEC_FILE.exists():
        raise SystemExit(f"CANNOT RUN: spec not found: {SPEC_FILE}")
    try:
        spec = yaml.safe_load(SPEC_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — an unreadable spec is a cannot-run
        raise SystemExit(f"CANNOT RUN: spec unreadable: {exc}") from exc
    if not spec or "components" not in spec:
        raise SystemExit("CANNOT RUN: spec missing 'components' key")
    return spec


def buildable_components(spec: dict[str, Any]) -> list[str]:
    return [
        key
        for key, meta in spec["components"].items()
        if meta.get("buildable_today") and not meta.get("hand_written")
    ]


def check_component(component: str, spec: dict[str, Any]) -> list[str]:
    meta = spec["components"].get(component)
    if not meta:
        return [f"{component}: not in spec"]
    if meta.get("hand_written"):
        # Deriving a stub and diffing it against real code reports guaranteed
        # drift and invites a "fix" that overwrites the bespoke pack. Skip it
        # for every caller, not just the CLI loop.
        return []
    expected = _render(component, meta)
    pack_dir = TOOLPACKS_DIR / _pack_id(component)
    problems: list[str] = []
    for rel, text in expected.items():
        path = pack_dir / rel
        if not path.exists():
            problems.append(f"{path.relative_to(REPO_ROOT)}: missing")
            continue
        actual = path.read_text(encoding="utf-8")
        if actual != text:
            problems.append(f"{path.relative_to(REPO_ROOT)}: differs from spec")
    return problems


def write_component(component: str, spec: dict[str, Any]) -> list[Path]:
    expected = _render(component, spec["components"][component])
    pack_dir = TOOLPACKS_DIR / _pack_id(component)
    pack_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for rel, text in expected.items():
        path = pack_dir / rel
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", help="one spec key (e.g. nim)")
    parser.add_argument("--all", action="store_true", help="every buildable component")
    parser.add_argument("--write", action="store_true", help="render files to disk")
    parser.add_argument("--check", action="store_true", help="report drift (default)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="render a hand-written component anyway (destroys its bespoke code)",
    )
    args = parser.parse_args(argv)

    spec = load_spec()
    if args.component:
        targets = [args.component]
    elif args.all:
        targets = buildable_components(spec)
        hand_written = sorted(
            key for key, meta in spec["components"].items() if meta.get("hand_written")
        )
        if hand_written:
            print(
                "skipping hand_written component(s): " + ", ".join(hand_written)
                + " (exempt from generation parity, NP006)"
            )
    else:
        targets = buildable_components(spec)

    if args.write:
        changed = []
        for component in targets:
            if component not in spec["components"]:
                print(f"unknown component: {component}", file=sys.stderr)
                return 1
            meta = spec["components"][component]
            # A hand-written pack is NOT derivable from the spec. Rendering it
            # replaces real code with a generated stub, exits 0, and destroys the
            # only copy when the pack is untracked. Refuse before writing anything.
            if meta.get("hand_written") and not args.force:
                print(
                    f"hand_written pack, refusing to render: {component} "
                    f"(edit it by hand; it is exempt from generation parity; "
                    f"pass --force to overwrite it anyway)",
                    file=sys.stderr,
                )
                return 1
            if not meta.get("buildable_today"):
                print(f"not buildable_today, refusing to render: {component}", file=sys.stderr)
                return 1
            changed.extend(write_component(component, spec))
        print(f"wrote {len(changed)} file(s) for {len(targets)} component(s)")
        return 0

    problems: list[str] = []
    for component in targets:
        meta = spec["components"].get(component, {})
        if meta.get("hand_written"):
            # Never re-render a hand-written pack to compare it: that diffs the
            # generated stub against real code and reports guaranteed drift. The
            # pack files ARE the record; NP001 skips them and NP006 ratchets the
            # exemption count, so "not applicable" is printed rather than hidden.
            print(
                f"{component}: hand_written — generation parity not applicable "
                f"(NP006 ratchets the exemption count)"
            )
            continue
        problems.extend(check_component(component, spec))

    if problems:
        print("NVIDIA pack drift:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"NVIDIA packs match the spec ({len(targets)} buildable component(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
