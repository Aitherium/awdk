"""The ACP Registry's admission contract, asserted here so CI can't discover it.

`agentclientprotocol/registry` will not list an agent that fails its auth check.
That check is a REAL handshake against a spawned process on a machine with no
fleet, no Ollama and no API key, and every way it fails looks like something
else:

  * no `authMethods` in the initialize result       -> "No authMethods in response"
  * a method typed anything but agent/terminal       -> silently ignored, reads as no auth
  * the process needs a backend to start             -> "Timeout waiting for initialize",
                                                        i.e. reported as a protocol bug
  * `protocolVersion: 1` from the validator          -> we speak v2 and must negotiate down

None of those is visible from inside this repo: the server starts, `initialize`
answers, and the fleet is right there. Hence these tests, each mutation-checked
against the shape that was live before 2026-08-07.

The validator's own logic is duplicated in `_valid_auth_types` rather than
imported — the registry is not a dependency, and vendoring the assertion is what
lets this run in our CI.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from queue import Empty, Queue

import pytest
from adk.acp_common import AUTH_METHOD_TYPES
from adk.acp_server import ACPServer

# ---------------------------------------------------------------------------
# The registry's rule, restated (AUTHENTICATION.md + .github/workflows/client.py)
# ---------------------------------------------------------------------------


def _method_type(method: dict) -> str:
    """Type detection exactly as the registry's parse_auth_methods does it."""
    auth_type = method.get("type")
    if not auth_type:
        meta = method.get("_meta", {})
        if isinstance(meta, dict):
            if "terminal-auth" in meta:
                auth_type = "terminal"
            elif "agent-auth" in meta:
                auth_type = "agent"
    return auth_type or "agent"  # documented default


def _valid_auth_types(auth_methods: list[dict]) -> list[str]:
    return [t for t in map(_method_type, auth_methods) if t in {"agent", "terminal"}]


class _NullAgent:
    name = "test-agent"

    async def run(self, prompt, **kwargs):  # pragma: no cover - never called here
        raise AssertionError("initialize must not touch the agent")


# ---------------------------------------------------------------------------


async def test_initialize_advertises_registry_valid_auth_methods():
    """The headline rule: at least one method typed agent or terminal."""
    server = ACPServer(_NullAgent(), name="aither", version="3.0.2")
    result = await server.handle_initialize({"protocolVersion": 1})

    methods = result.get("authMethods")
    assert methods, "authMethods empty -> registry rejects with 'No authMethods in response'"
    assert _valid_auth_types(methods), (
        f"no method typed agent/terminal; got {[_method_type(m) for m in methods]}"
    )


async def test_both_auth_types_are_offered():
    """A GUI editor cannot host a terminal; a headless box has no browser.

    Offering only one type makes the agent unusable in half the clients that
    would otherwise install it — and nothing fails, the user just cannot sign in.
    """
    server = ACPServer(_NullAgent())
    methods = (await server.handle_initialize({}))["authMethods"]
    assert set(_valid_auth_types(methods)) == {"agent", "terminal"}


async def test_terminal_method_carries_relaunch_args():
    """Terminal Auth REPLACES our args with these, so they must name a real command."""
    server = ACPServer(_NullAgent())
    methods = (await server.handle_initialize({}))["authMethods"]
    terminal = [m for m in methods if _method_type(m) == "terminal"]
    assert terminal, "no terminal method"
    assert terminal[0].get("args"), (
        "terminal method has no args -> the client relaunches the ACP server itself"
    )
    # Asserted against the CLI in test_acp_cli_has_login_subcommand below.
    assert terminal[0]["args"] == ["acp", "login"]


async def test_every_advertised_type_is_one_the_registry_accepts():
    """A type outside {agent, terminal} is DROPPED by the validator, not rejected.

    That is the nasty direction: authMethods looks populated locally and the
    registry reports "no auth method with type agent or terminal".
    """
    server = ACPServer(_NullAgent())
    methods = (await server.handle_initialize({}))["authMethods"]
    for m in methods:
        assert m["type"] in AUTH_METHOD_TYPES, f"{m['id']} advertises unusable type {m['type']!r}"


async def test_auth_methods_advertised_even_when_already_signed_in():
    """The verifier handshakes cold; a "we're logged in, so []" answer fails CI."""
    server = ACPServer(_NullAgent())
    server._authenticated = True
    assert (await server.handle_initialize({}))["authMethods"]


async def test_logout_capability_is_advertised():
    """Clients MUST NOT call logout unless agentCapabilities.auth.logout is present."""
    server = ACPServer(_NullAgent())
    result = await server.handle_initialize({})
    assert result.get("agentCapabilities", {}).get("auth", {}).get("logout") == {}


async def test_v1_client_gets_v1_echoed():
    """The validator sends protocolVersion 1. Echoing 2 is a version we were not asked for."""
    server = ACPServer(_NullAgent())
    assert (await server.handle_initialize({"protocolVersion": 1}))["protocolVersion"] == 1


async def test_unknown_auth_method_is_refused_not_silently_accepted():
    from adk.acp_common import RpcError

    server = ACPServer(_NullAgent())
    with pytest.raises(RpcError):
        await server.handle_auth_login({"methodId": "not-a-real-method"})


def test_acp_cli_has_login_subcommand():
    """`aither-terminal` names `adk acp login`; a missing subcommand fails inside
    the editor, where nobody sees the output."""
    proc = subprocess.run(
        [sys.executable, "-m", "adk.cli", "acp", "login", "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert proc.returncode == 0, f"`adk acp login --help` failed:\n{proc.stderr[-800:]}"


def test_uvx_entrypoint_exists():
    """`uvx awdk ...` runs the console script NAMED awdk.

    Without it the registry's uvx distribution installs fine and then cannot
    launch — and the registry's own validation only checks that the PyPI package
    exists, so this passes CI and fails on every user's machine.
    """
    import pathlib
    import re

    pyproject = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    scripts = text.split("[project.scripts]", 1)[1].split("\n[", 1)[0]
    assert re.search(r"^awdk\s*=", scripts, re.M), (
        "no `awdk` console script -> `uvx awdk acp serve` cannot launch"
    )


@pytest.mark.timeout(180)
def test_handshake_survives_a_box_with_no_backend():
    """The registry's runner has no fleet, no Ollama and no API key.

    Before 2026-08-07 `adk acp serve` awaited `get_provider()` at startup and
    exited there, so the validator timed out waiting for `initialize` — which
    reads as a protocol bug rather than "we probed for a model too early".
    """
    home = tempfile.mkdtemp(prefix="acp-registry-ci-")
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("AITHER", "OPENAI", "ANTHROPIC", "OLLAMA", "DEEPSEEK"))
    }
    env.update({"HOME": home, "USERPROFILE": home, "PYTHONPATH": repo_root})

    proc = subprocess.Popen(
        [sys.executable, "-m", "adk.cli", "acp", "serve"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=env, cwd=repo_root,
    )
    try:
        q: Queue = Queue()
        threading.Thread(target=lambda: q.put(proc.stdout.readline()), daemon=True).start()
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientInfo": {"name": "ACP Registry Validator", "version": "1.0.0"},
                "clientCapabilities": {
                    "terminal": True,
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "_meta": {"terminal_output": True, "terminal-auth": True},
                },
            },
        }) + "\n")
        proc.stdin.flush()
        try:
            line = q.get(timeout=150)
        except Empty:
            line = None
    finally:
        proc.kill()

    assert line, "no initialize response on a backend-less box (registry CI would time out)"
    methods = json.loads(line).get("result", {}).get("authMethods", [])
    assert _valid_auth_types(methods), f"no usable auth method on a clean box: {methods}"
