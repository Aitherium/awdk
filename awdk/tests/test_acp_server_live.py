"""adk's ACP SERVER driven by Zed's REFERENCE ACP CLIENT.

The mirror of `test_acp_live_reference.py` (which proves our client). Together
they prove awdk speaks ACP in BOTH directions:
  * client: adk can drive any ACP agent (hermes, ...)
  * server: any ACP host (Zed / VS Code / JetBrains) can drive an adk agent

The driver runs under the venv that has `agent-client-protocol` installed and
emits a single `RESULT {json}` line; this test asserts on that.
Skips (never fails) when the reference venv is absent.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# No default: the previous fallback was one developer's personal scratchpad path,
# shipped in the public package — it leaked a username and pointed somewhere no
# other machine can have. Set ADK_ACP_REF_DIR to a local reference checkout to run
# these; unset, they skip exactly as they did before.
_SCRATCH = Path(os.environ.get("ADK_ACP_REF_DIR", ""))
_VENV_PY = _SCRATCH / "acpvenv" / "Scripts" / "python.exe"
_DRIVER = _SCRATCH / "ref_client_drive_adk.py"
_RUNNER = _SCRATCH / "adk_acp_server_runner.py"

pytestmark = pytest.mark.skipif(
    not (_VENV_PY.exists() and _DRIVER.exists() and _RUNNER.exists()),
    reason="reference agent-client-protocol venv/driver not present",
)


@pytest.fixture(scope="module")
def drive_result() -> dict:
    proc = subprocess.run(
        [str(_VENV_PY), "-u", str(_DRIVER), sys.executable],
        capture_output=True,
        text=True,
        timeout=180,
    )
    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), None
    )
    assert line, f"driver produced no RESULT line.\nstdout={proc.stdout[-2000:]}\nstderr={proc.stderr[-2000:]}"
    return json.loads(line[len("RESULT ") :])


def test_reference_client_completes_handshake(drive_result):
    assert drive_result.get("ok") is True, drive_result.get("error")
    # initialize -> our agentInfo/protocolVersion/agentCapabilities parsed by the REAL client
    assert drive_result["agent_name"] == "adk-proof"
    assert drive_result["agent_version"] == "2.0.0"
    assert drive_result["protocol_version"] == 1
    assert drive_result["load_session"] is True


def test_reference_client_gets_session_and_stop_reason(drive_result):
    assert drive_result["session_id"].startswith("adk-"), "server must mint a sessionId"
    assert drive_result["stop_reason"] == "end_turn"


def test_reference_client_receives_streamed_message(drive_result):
    assert drive_result["texts"] == ["adk-says:ping"], (
        "agent_message_chunk must reach the real client with the agent's output"
    )


def test_reference_client_receives_real_tool_call_stream(drive_result):
    # ACP shape: tool_call (start) then tool_call_update with a terminal status
    assert drive_result["tool_start_ids"] == ["tc-9"]
    assert drive_result["tool_titles"] == ["grep"]
    assert drive_result["tool_update_status"] == ["completed"]
