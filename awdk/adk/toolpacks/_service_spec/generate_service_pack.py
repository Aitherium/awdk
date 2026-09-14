#!/usr/bin/env python
"""Generate service toolpacks from the service specification.

Reads services-spec.yaml, produces a complete, self-contained pack for each
service with __init__.py, config.py, tools.py, and .toolpack.yaml, ready to
be discovered by adk's tool_pack_loader.

Deterministic: same spec in → byte-identical output.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import logging
import sys
import tempfile
from pathlib import Path

import yaml

logger = logging.getLogger("service_pack_generator")

SPEC_FILE = Path(__file__).parent / "services-spec.yaml"
TOOLPACKS_DIR = Path(__file__).parent.parent
TEMPLATE_PACK = TOOLPACKS_DIR / "nvidia_cuda"  # Use NVIDIA as style reference


def load_spec() -> dict:
    """Load and validate the service spec."""
    spec = yaml.safe_load(SPEC_FILE.read_text(encoding="utf-8"))
    if not spec or "services" not in spec:
        raise ValueError("spec missing 'services' key")
    return spec


def gen_init(service: str, tools_spec: list[dict]) -> str:
    """Generate __init__.py for a pack."""
    tool_names = [t["name"] for t in tools_spec]
    tool_names_str = ",\n    ".join(f'"{t}"' for t in tool_names)

    return f'''"""AitherOS {service} pack — auto-generated.

Tool registration for {service} service endpoints. This pack is OPTIONAL and
fails gracefully when not authenticated — an agent with no credential still
registers these tools with "not configured" status, never crashes.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("{service}_pack")

PACK_ID = "{service}"

_TOOL_NAMES = [
    {tool_names_str},
]


def register(registry) -> int:
    """Register all {service}_* tools. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools
        logger.warning("{service} pack unavailable (%s) — 0 tools registered", exc)
        return 0

    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("{service}: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool ≠ crash
            logger.debug("{service}: skip tool %s: %s", name, exc)

    logger.info("Service {service.replace('_', ' ').title()} pack registered %d "
                "{service}_* tools", n)
    return n
'''


def gen_config(service: str, config_meta: dict) -> str:
    """Generate config.py for a pack."""
    auth_type = config_meta.get("auth_type", "none")
    base_url = config_meta.get("base_url", "")
    port = config_meta.get("port", "8000")

    return f'''"""AitherOS {service} pack — config and auth.

AUTO-GENERATED from _service_spec/services-spec.yaml. Do not edit by hand:
check_service_pack_parity will report your change as drift. Edit the spec and
regenerate instead.

Handles authentication, credential storage, and endpoint configuration.
This pack is OPTIONAL — failures to authenticate are reported as a status
dict with a fix, never as an exception.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("{service}_pack")

# ── endpoints ───────────────────────────────────────────────────────────

BASE_URL = "{base_url}"
PORT = {port}

# ── auth config ─────────────────────────────────────────────────────────

# Auth types: internal_key, oauth_device_flow, none
AUTH_TYPE = "{auth_type}"

# Internal services authenticate via X-Internal-Key header.
# The key is read fresh on every call so a rotation is picked up without restart.
def get_internal_key() -> str:
    """Retrieve the internal API key from env or config."""
    # Prefer env var; fallback to config file
    env_key = os.environ.get("AITHER_INTERNAL_SECRET", "").strip()
    if env_key:
        return env_key
    # Fallback to config file (if it exists)
    config_file = Path.home() / ".aither" / "internal_key.txt"
    try:
        return config_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def redact(value) -> str:
    """Strip API keys from text headed for logs or the agent."""
    text = str(value)
    key = get_internal_key()
    if key and len(key) > 8:
        text = text.replace(key, "***REDACTED***")
    return text
'''


def gen_tools(service: str, tools_spec: list[dict]) -> str:
    """Generate tools.py stub for a pack.

    Each tool MUST fail CLOSED on auth failure:
    - HTTP 401 → status: "not_authenticated" with a fix instruction
    - HTTP 5xx → status: "service_error" with details
    - Other error → status: "error" with message

    NOT HTTP 200 with empty results — that looks like working.
    """
    tool_funcs = []
    for tool in tools_spec:
        name = tool["name"]
        desc = tool.get("description", "")
        args = tool.get("args", [])
        arg_sig = ", ".join(args) if args else ""

        tool_funcs.append(f'''
def {name}({arg_sig}) -> dict:
    """{desc}

    Returns:
        {{"status": "success", "data": <result>}} on success
        {{"status": "not_authenticated", "fix": "..."}} on auth failure (401)
        {{"status": "service_error", "message": "..."}} on service error (5xx)
        {{"status": "not_configured"}} if service is unreachable
    """
    return {{
        "status": "not_configured",
        "fix": "Service pack stub — implement the {name} function",
    }}
''')

    return f'''"""AitherOS {service} pack — tool implementations.

AUTO-GENERATED from _service_spec/services-spec.yaml. Do not edit by hand:
check_service_pack_parity will report your change as drift. Edit the spec and
regenerate instead.

These are stubs generated from the spec. Implement each tool function to
make the pack actually useful. Each must return a dict and handle errors
gracefully (no exceptions). Always fail CLOSED on auth failure (401).
"""
from __future__ import annotations

import logging

logger = logging.getLogger("{service}_pack")
{"".join(tool_funcs)}
'''


def gen_manifest(service: str, metadata: dict) -> str:
    """Generate .toolpack.yaml manifest."""
    display_name = metadata.get("display_name", service)
    description = metadata.get("description", "")
    tool_prefix = metadata.get("tool_prefix", service)
    buildable_today = metadata.get("buildable_today", False)

    return f"""# AitherOS {display_name} toolpack manifest
# Auto-generated from services-spec.yaml

id: svc_{service}
version: 1.0.0
display_name: "{display_name}"
description: "{description}"

# Capabilities
mcp_tools:
  - "{tool_prefix}_*"
tool_modules: []  # empty: pack is self-contained (loaded by file path)

# Metadata
available: {str(buildable_today).lower()}
buildable_today: {str(buildable_today).lower()}
optional: true
fail_soft: true  # unauthenticated packs register with "not_configured" status

# Skills
skills: []  # populated if there are published doctrine assets

# Attributes for discovery
tags:
  - aitheros-platform
  - {tool_prefix}
  - service-api
"""


def generate_pack(service: str, metadata: dict) -> dict[str, str]:
    """Generate a complete pack. Returns dict of filename -> content."""
    tools_spec = metadata.get("tools", [])
    if not tools_spec:
        raise ValueError(f"{service}: no tools defined")

    return {
        "__init__.py": gen_init(service, tools_spec),
        "config.py": gen_config(service, metadata),
        "tools.py": gen_tools(service, tools_spec),
        ".toolpack.yaml": gen_manifest(service, metadata),
    }


def pack_dir_for(service: str) -> Path:
    """Resolve a service key to its on-disk pack directory.

    Packs are namespaced `svc_*` on disk so they cannot collide with other
    toolpacks. A spec key becomes `svc_<name>` on disk.
    """
    name = service if service.startswith("svc_") else f"svc_{service}"
    return TOOLPACKS_DIR / name


def write_pack(
    service: str,
    generated: dict[str, str],
    *,
    force: bool = False,
    hand_written: bool = False,
) -> Path:
    """Write a generated pack to disk. Returns pack directory path.

    `hand_written` comes from the spec, not from a name comparison — hardcoding
    the one pack that happens to be hand-written today means the next one is
    silently unprotected and gets overwritten by a routine regeneration.
    """
    pack_dir = pack_dir_for(service)
    if pack_dir.exists() and hand_written and not force:
        raise RuntimeError(
            f"will not overwrite {service} (hand-written pack; use --force to override)"
        )
    pack_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in generated.items():
        (pack_dir / filename).write_text(content, encoding="utf-8")
    return pack_dir


def check_parity(service: str, generated: dict[str, str]) -> tuple[bool, str]:
    """Compare generated pack against on-disk version.

    Returns (matches, detailed_diff_or_empty).
    """
    pack_dir = pack_dir_for(service)
    if not pack_dir.exists():
        return False, f"pack directory {pack_dir} does not exist"

    diffs = []
    for filename, expected in generated.items():
        file_path = pack_dir / filename
        if not file_path.exists():
            diffs.append(f"missing: {filename}")
            continue
        actual = file_path.read_text(encoding="utf-8")
        if actual != expected:
            delta = "\n".join(
                difflib.unified_diff(
                    actual.splitlines(keepends=True),
                    expected.splitlines(keepends=True),
                    fromfile=f"on-disk/{filename}",
                    tofile=f"generated/{filename}",
                    lineterm="",
                )
            )
            diffs.append(delta)

    if diffs:
        return False, "\n\n".join(diffs)
    return True, ""


def main():
    parser = argparse.ArgumentParser(
        description="Generate service toolpacks from the service spec."
    )
    parser.add_argument(
        "--service",
        default="",
        help="Generate only this service (e.g. nexus, directory). Default: all.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Generate all services (default if no --service given).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Generate in-memory and diff against on-disk; exit 1 on mismatch.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite on-disk packs even if hand-written.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run self-tests (construct a test spec, generate, verify).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    if args.self_test:
        return self_test()

    spec = load_spec()
    services = spec.get("services", {})

    if not services:
        logger.error("spec has no services")
        return 1

    targets = []
    if args.service:
        targets = [args.service]
    elif args.all or not args.check:
        targets = list(services.keys())

    if args.check:
        any_mismatch = False
        checked = 0
        for target in targets or list(services.keys()):
            if target not in services:
                logger.error("service not found: %s", target)
                return 1
            metadata = services[target]
            # A service we cannot legally build has no pack on disk BY DESIGN.
            if not metadata.get("buildable_today", False):
                logger.info("%s: SKIPPED (buildable_today=false)", target)
                continue
            if metadata.get("hand_written", False):
                logger.info("%s: SKIPPED (hand_written=true)", target)
                continue
            checked += 1
            generated = generate_pack(target, metadata)
            matches, diff = check_parity(target, generated)
            if matches:
                logger.info("%s: matches on-disk", target)
            else:
                logger.error("%s: does not match on-disk\n%s", target, diff)
                any_mismatch = True
        if checked == 0:
            logger.error("no buildable service was checked; cannot emit a verdict")
            return 2
        return 1 if any_mismatch else 0

    for target in targets:
        if target not in services:
            logger.error("service not found: %s", target)
            return 1
        metadata = services[target]
        try:
            generated = generate_pack(target, metadata)
            pack_dir = write_pack(
                target,
                generated,
                force=args.force,
                hand_written=metadata.get("hand_written", False),
            )
            logger.info("generated %s -> %s", target, pack_dir)
        except Exception as exc:  # noqa: BLE001
            logger.error("generate %s failed: %s", target, exc)
            return 1

    return 0


def self_test() -> int:
    """Test that the generator works and can detect drift."""
    spec = load_spec()
    services = spec.get("services", {})
    # Probe with the first buildable service rather than a hardcoded name.
    buildable = [k for k, v in services.items() if v.get("buildable_today")]
    if not buildable:
        logger.error("spec declares no buildable service; cannot self-test")
        return 2
    probe = buildable[0]
    metadata = services[probe]
    generated = generate_pack(probe, metadata)

    # Verify we generated all files
    required_files = {"__init__.py", "config.py", "tools.py", ".toolpack.yaml"}
    if set(generated.keys()) != required_files:
        logger.error(
            "generated unexpected file set: %s (expected %s)",
            set(generated.keys()),
            required_files,
        )
        return 1

    # Verify content is non-empty
    for filename, content in generated.items():
        if not content or len(content) < 100:
            logger.error(
                "generated content for %s is suspiciously small (%d bytes)",
                filename,
                len(content),
            )
            return 1
        if "auto-generated" not in content.lower():
            logger.error(
                "generated %s carries no AUTO-GENERATED marker; a human would not "
                "know not to edit it",
                filename,
            )
            return 1

    # Verify determinism: generate again and compare hash
    generated2 = generate_pack(probe, metadata)
    hash1 = hashlib.sha256(
        "".join(generated[k] for k in sorted(generated.keys())).encode()
    ).hexdigest()
    hash2 = hashlib.sha256(
        "".join(generated2[k] for k in sorted(generated2.keys())).encode()
    ).hexdigest()
    if hash1 != hash2:
        logger.error("generator is not deterministic: hashes differ")
        return 1

    # MUTATION GUARD — prove the drift detector can actually FAIL.
    global TOOLPACKS_DIR
    real_root = TOOLPACKS_DIR
    with tempfile.TemporaryDirectory() as tmp:
        try:
            TOOLPACKS_DIR = Path(tmp)
            write_pack(probe, generated)
            clean, _ = check_parity(probe, generated)
            if not clean:
                logger.error("mutation guard: freshly written pack did not match itself")
                return 1

            victim = pack_dir_for(probe) / "tools.py"
            victim.write_text(
                victim.read_text(encoding="utf-8") + "\n# drift introduced by self-test\n",
                encoding="utf-8",
            )
            drifted, diff = check_parity(probe, generated)
            if drifted:
                logger.error(
                    "mutation guard FAILED: check_parity reported a corrupted pack as clean"
                )
                return 1
            if not diff:
                logger.error("mutation guard: drift detected but no diff was produced")
                return 1
        finally:
            TOOLPACKS_DIR = real_root

    logger.info(
        "self-test passed: generator is deterministic, complete, and drift is detectable"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
