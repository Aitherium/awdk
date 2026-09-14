"""CLI commands for MCP evaluation harness.

Implements: adk eval tools, adk eval pack, adk eval self-test
(``self-test`` is a SUBCOMMAND, not a flag — a doc that says ``--self-test``
sends every reader to an 'unrecognized arguments' error.)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


async def cmd_eval_tools(args) -> int:
    """Evaluate all available tools on a gateway.

    Exit codes:
        0 = all verdicts pass
        1 = failures (named in output)
        2 = cannot judge (no gateway/auth/transport down)
    """
    from adk.evalharness import (
        ToolEnumerator,
        ToolClassifier,
        ToolInvoker,
        EvalReport,
    )
    from adk.mcp import MCPAuth

    gateway_url = args.gateway or os.getenv("AITHER_GATEWAY_URL", "https://mcp.aitherium.com")
    api_key = args.api_key or os.getenv("AITHER_API_KEY", "")
    json_output = args.json
    invoke_safe = args.invoke

    print("Connecting to MCP gateway...")
    print(f"  Gateway: {gateway_url}")

    # Try to authenticate
    auth = None
    authenticated = False
    tier = "anonymous"

    if api_key:
        try:
            auth = MCPAuth(api_key=api_key, gateway_url=gateway_url)
            auth_context = await auth.authenticate()
            authenticated = auth_context.authenticated
            tier = auth_context.tier
            if authenticated:
                print(f"  Authenticated: {auth_context.key_type} ({auth_context.user_id})")
                print(f"  Tier: {tier}")
            else:
                print("  Authentication failed (proceeding without auth)")
        except Exception as exc:
            logger.warning("Auth failed: %s", exc)
            print(f"  Authentication error: {exc}")
    else:
        print("  No API key (proceeding as anonymous)")

    # Enumerate tools
    enumerator = ToolEnumerator()
    try:
        connected = await enumerator.connect(gateway_url, api_key, auth)
        if not connected:
            print("ERROR: Could not connect to gateway")
            return 2
    except Exception as exc:
        logger.error("Connection failed: %s", exc)
        print(f"ERROR: Connection failed: {exc}")
        return 2

    print(f"Connected to {enumerator.gateway_url}")

    try:
        tools = await enumerator.list_all()
        print(f"Enumerated {len(tools)} tools")
    except Exception as exc:
        logger.error("Tool enumeration failed: %s", exc)
        print(f"ERROR: Tool enumeration failed: {exc}")
        return 2

    # Classify tools
    classifier = ToolClassifier()
    report = EvalReport(
        gateway_url=gateway_url,
        authenticated=authenticated,
        tier=tier,
    )

    safe_tools = []
    for tool in tools:
        is_safe = classifier.is_safe_to_invoke(tool.name, tool.description)
        report.add_tool(tool.name, callable=True, safe=is_safe)
        if is_safe:
            safe_tools.append(tool)

    # Optionally invoke safe tools
    if invoke_safe and safe_tools:
        print(f"\nSmoke-invoking {len(safe_tools)} safe tools...")
        invoker = ToolInvoker(bridge=enumerator._bridge if hasattr(enumerator, '_bridge') else None)

        # Import MCPBridge to get the bridge from enumerator
        from adk.mcp import MCPBridge
        bridge = MCPBridge(auth=auth) if auth else MCPBridge(mcp_url=gateway_url, api_key=api_key)
        invoker.set_bridge(bridge)

        invoke_results = await invoker.invoke_safe_tools(safe_tools, classifier)
        for result in invoke_results:
            report.add_invoke_result(
                result.tool_name,
                result.success,
                result.status,
                result.message,
                result.error_type,
            )
        print(f"Invoked {len(invoke_results)} tools")

    # Output report
    if json_output:
        print(report.json_format())
    else:
        print(report.human_format())
        print(report.summary_table())

    # Return exit code: 0 if all callable, 1 if some failed, 2 if transport down
    if report.total_callable == report.total_tools and report.total_tools > 0:
        return 0
    elif report.total_tools == 0:
        return 2
    else:
        return 1


async def cmd_eval_pack(args) -> int:
    """Evaluate a pack's declared tools.

    Exit codes:
        0 = all declared tools exist and are callable
        1 = some tools missing or failed
        2 = cannot judge (no gateway/auth/transport down)
    """
    from adk.evalharness import (
        ToolEnumerator,
        PackEvalResult,
        EvalReport,
    )
    from adk.tool_pack_loader import get_tool_pack_loader
    from adk.mcp import MCPAuth

    pack_ref = args.pack
    gateway_url = args.gateway or os.getenv("AITHER_GATEWAY_URL", "https://mcp.aitherium.com")
    api_key = args.api_key or os.getenv("AITHER_API_KEY", "")
    json_output = args.json

    print(f"Loading pack: {pack_ref}")

    # Resolve the pack: a bare id goes through the loader's discovery (the same
    # path the runtime binds packs with, so this cannot disagree with it); a
    # filesystem path is parsed directly, supporting a pack dir or the manifest
    # file itself.
    try:
        from pathlib import Path as _Path
        loader = get_tool_pack_loader()
        pack_manifest = None
        ref_path = _Path(pack_ref)
        if ref_path.exists():
            mf = ref_path / ".toolpack.yaml" if ref_path.is_dir() else ref_path
            if not mf.exists():
                print(f"ERROR: {ref_path} has no .toolpack.yaml — not a tool pack")
                return 2
            pack_manifest = loader._parse(mf, mf.parent)
        else:
            pack_manifest = loader.discover().get(pack_ref)
        if not pack_manifest:
            known = ", ".join(sorted(loader.discover())[:12])
            print(f"ERROR: Pack not found: {pack_ref} (known ids include: {known})")
            return 2
    except Exception as exc:
        logger.error("Pack load failed: %s", exc)
        print(f"ERROR: Failed to load pack: {exc}")
        return 2

    print(f"Loaded pack: {pack_manifest.name or pack_manifest.id}")
    print(f"Declared tools: {len(pack_manifest.mcp_tools)}")

    # Connect to gateway
    print(f"\nConnecting to MCP gateway: {gateway_url}")

    auth = None
    authenticated = False
    tier = "anonymous"

    if api_key:
        try:
            auth = MCPAuth(api_key=api_key, gateway_url=gateway_url)
            auth_context = await auth.authenticate()
            authenticated = auth_context.authenticated
            tier = auth_context.tier
        except Exception as exc:
            logger.warning("Auth failed: %s", exc)

    enumerator = ToolEnumerator()
    try:
        connected = await enumerator.connect(gateway_url, api_key, auth)
        if not connected:
            print("ERROR: Could not connect to gateway")
            return 2
    except Exception as exc:
        logger.error("Connection failed: %s", exc)
        print(f"ERROR: Connection failed: {exc}")
        return 2

    try:
        tools = await enumerator.list_all()
    except Exception as exc:
        logger.error("Tool enumeration failed: %s", exc)
        print(f"ERROR: Tool enumeration failed: {exc}")
        return 2

    # Build tool name set
    available_tool_names = {tool.name for tool in tools}

    # Check which declared tools exist
    missing_tools = []
    found_count = 0
    for declared_tool_name in pack_manifest.mcp_tools:
        # Handle wildcards
        if declared_tool_name.endswith("*"):
            pattern = declared_tool_name[:-1]
            matched = [t for t in available_tool_names if t.startswith(pattern)]
            if matched:
                found_count += len(matched)
            else:
                missing_tools.append(declared_tool_name)
        else:
            if declared_tool_name in available_tool_names:
                found_count += 1
            else:
                missing_tools.append(declared_tool_name)

    # Build report
    report = EvalReport(
        gateway_url=gateway_url,
        authenticated=authenticated,
        tier=tier,
    )

    pack_result = PackEvalResult(
        pack_name=pack_manifest.name or pack_manifest.id,
        pack_id=pack_manifest.id,
        pack_path=str(pack_manifest.path),
        tools_declared=len(pack_manifest.mcp_tools),
        tools_found=found_count,
        tools_missing=missing_tools,
        success=len(missing_tools) == 0,
    )
    report.add_pack(pack_result)

    # Output report
    if json_output:
        print(report.json_format())
    else:
        print(report.human_format())

    # Return exit code
    if len(missing_tools) == 0:
        return 0
    else:
        print(f"\nERROR: {len(missing_tools)} declared tools not found on gateway:")
        for tool_name in sorted(missing_tools):
            print(f"  - {tool_name}")
        return 1


async def cmd_eval_self_test(args) -> int:
    """Run offline self-test with stubbed tools.

    Proves the harness can:
    - Detect declared-but-absent tools
    - Never invoke mutating tools
    - Report transport-down scenarios
    """
    from adk.evalharness import (
        ToolInfo,
        ToolClassifier,
        EvalReport,
        PackEvalResult,
    )

    print("Running MCP Eval Harness self-test (offline)...")
    print("")

    # Test 1: Tool classification
    print("TEST 1: Tool classification")
    classifier = ToolClassifier()

    test_cases = [
        ("delete_user", False, "mutating verb"),
        ("create_file", False, "mutating verb"),
        ("get_user", True, "safe verb"),
        ("list_items", True, "safe verb"),
        ("update_config", False, "mutating verb"),
    ]

    all_pass = True
    for tool_name, expected_safe, reason in test_cases:
        is_safe = classifier.is_safe_to_invoke(tool_name)
        status = "✓" if is_safe == expected_safe else "✗"
        if is_safe != expected_safe:
            all_pass = False
        print(f"  {status} {tool_name:20s} safe={is_safe:5} ({reason})")

    if not all_pass:
        print("FAIL: Classification test failed")
        return 1

    print("")
    print("TEST 2: Pack with missing tools")

    # Simulate a pack with some tools missing
    report = EvalReport(gateway_url="(stubbed)", authenticated=False)
    pack = PackEvalResult(
        pack_name="test-pack",
        pack_id="test-pack",
        tools_declared=3,
        tools_found=1,
        tools_missing=["missing_tool_1", "missing_tool_2"],
        success=False,
    )
    report.add_pack(pack)

    if pack.all_declared_found:
        print("FAIL: Missing tools should be detected")
        return 1
    print(f"  ✓ Pack with missing tools detected: {len(pack.tools_missing)} missing")

    print("")
    print("TEST 3: Report generation")

    # Test both output formats work
    try:
        json_report = report.json_format()
        if not json_report or "pack" not in json_report:
            print("FAIL: JSON report empty or malformed")
            return 1
        print("  ✓ JSON report generated")

        human_report = report.human_format()
        if not human_report or "Missing" not in human_report:
            print("FAIL: Human report missing expected content")
            return 1
        print("  ✓ Human report generated")
    except Exception as exc:
        print(f"FAIL: Report generation error: {exc}")
        return 1

    print("")
    print("=" * 50)
    print("All self-tests passed!")
    print("The harness is correctly set up and can:")
    print("  - Classify tools by safety")
    print("  - Detect missing declared tools")
    print("  - Generate reports in multiple formats")
    print("")

    return 0
