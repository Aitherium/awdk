"""Device-certificate enrollment.

HISTORY — why these tests changed shape (2026-07-25):
The previous suite tested `_request_device_cert`, which POSTed to
`/v1/nodes/mtls-cert` — **a route that has never existed on the identity service**.
Every real enrollment logged `mtls-cert request HTTP 405: Method Not Allowed` (405, not
404, because the path matched the GET-only `/v1/nodes/{node_id}` route with
node_id="mtls-cert"), the failure was swallowed as "best-effort", and every node enrolled
WITHOUT a device cert — silently falling back to bearer-token auth, the exact spoofable
posture the cert exists to remove.

The tests passed the whole time, because they mocked `httpx.AsyncClient.post` to return a
200 with a cert body. A mock made a nonexistent endpoint look real. That is the failure
mode these tests now guard against: `test_rich_enroll_makes_no_second_cert_request` asserts
the number of HTTP calls, so re-introducing a call to a phantom endpoint fails the suite
instead of passing it.

`register_node` already mints the cert and returns the full bundle in its own response, so
the fix was to read what we were already handed.
"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adk import enrollment


CERT = "-----BEGIN CERTIFICATE-----\nCERT\n-----END CERTIFICATE-----"
KEY = "FAKE-TEST-PRIVATE-KEY-MATERIAL-NOT-REAL"
CHAIN = "-----BEGIN CERTIFICATE-----\nCHAIN\n-----END CERTIFICATE-----"

REGISTER_OK = {
    "status": "registered",
    "node_id": "test-node",
    "tenant_id": "tnt_abc123",
    "public_url": "https://gateway.aitherium.com/nodes/test-node",
    "workspace_id": "ws_xyz789",
    "workspace": {"name": "My Workspace"},
    "bearer_token": "bearer_token_123",
    "mtls": {
        "issued": True,
        "cn": "devcert--tnt_abc123--test-node",
        "certificate": CERT,
        "private_key": KEY,
        "chain": CHAIN,
    },
}


@pytest.fixture
def temp_identity_dir():
    """Create a temporary directory for device identity testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def test_persists_the_cert_the_register_response_returned(temp_identity_dir):
    with patch("adk.sync.device_identity.save_enrolled_identity") as mock_save:
        result = enrollment._persist_device_cert(REGISTER_OK)

    assert result["success"] is True
    assert result["mtls"]["certificate"] == CERT
    mock_save.assert_called_once()
    # The bundle handed to the store must carry the key, or mTLS cannot be used at all.
    assert mock_save.call_args.args[0]["private_key"] == KEY


def test_reports_failure_when_the_server_says_the_cert_was_not_issued():
    """`register_node` returns {issued: False, reason: ...} when minting fails. That is a
    real negative answer and must not be reported as an enrolled cert."""
    body = {**REGISTER_OK, "mtls": {"issued": False, "reason": "CA unreachable"}}

    with patch("adk.sync.device_identity.save_enrolled_identity") as mock_save:
        result = enrollment._persist_device_cert(body)

    assert result["success"] is False
    assert "CA unreachable" in result["error"]
    mock_save.assert_not_called()


def test_reports_failure_when_the_bundle_has_no_private_key():
    """A cert without its key is unusable — accepting it would 'enroll' an identity that
    cannot authenticate, which is worse than a clean failure."""
    body = {**REGISTER_OK, "mtls": {"issued": True, "certificate": CERT}}

    with patch("adk.sync.device_identity.save_enrolled_identity") as mock_save:
        result = enrollment._persist_device_cert(body)

    assert result["success"] is False
    assert "incomplete" in result["error"]
    mock_save.assert_not_called()


def test_reports_failure_when_there_is_no_mtls_block_at_all():
    result = enrollment._persist_device_cert({"status": "registered"})
    assert result["success"] is False


def test_persistence_error_is_reported_not_swallowed():
    with patch(
        "adk.sync.device_identity.save_enrolled_identity",
        side_effect=OSError("disk full"),
    ):
        result = enrollment._persist_device_cert(REGISTER_OK)

    assert result["success"] is False
    assert "disk full" in result["error"]


def _mock_register(monkeyed_response):
    """Patch httpx so POST /v1/nodes/register returns `monkeyed_response`; hand back the
    client mock so callers can assert on how many requests were actually made."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = monkeyed_response
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post.return_value = mock_resp
    return mock_client


@pytest.mark.asyncio
async def test_rich_enroll_enrolls_the_cert_from_the_register_response():
    mock_client = _mock_register(REGISTER_OK)

    with patch("httpx.AsyncClient", return_value=mock_client):
        with patch("adk.enrollment._save_workspace"):
            with patch("adk.sync.device_identity.save_enrolled_identity") as mock_save:
                result = await enrollment.rich_enroll(
                    "https://identity.example.com",
                    "user_token",
                    "test-node",
                    enable_heartbeat=False,
                )

    assert result["enrolled"] is True
    assert result["cert_enrolled"] is True
    assert result["tenant_id"] == "tnt_abc123"
    mock_save.assert_called_once()


@pytest.mark.asyncio
async def test_rich_enroll_makes_no_second_cert_request():
    """THE REGRESSION GUARD. The original bug was a second POST to a route that does not
    exist, hidden because the test mocked that POST to succeed. Assert the CALL COUNT:
    enrollment is exactly one request (register), and the cert comes back inside it."""
    mock_client = _mock_register(REGISTER_OK)

    with patch("httpx.AsyncClient", return_value=mock_client):
        with patch("adk.enrollment._save_workspace"):
            with patch("adk.sync.device_identity.save_enrolled_identity"):
                await enrollment.rich_enroll(
                    "https://identity.example.com",
                    "user_token",
                    "test-node",
                    enable_heartbeat=False,
                )

    assert mock_client.post.await_count == 1
    called_url = mock_client.post.await_args.args[0]
    assert called_url.endswith("/v1/nodes/register")
    assert "mtls-cert" not in called_url


@pytest.mark.asyncio
async def test_rich_enroll_survives_a_cert_that_was_not_issued():
    """A missing cert is non-fatal for enrollment itself — but it must be REPORTED as
    cert_enrolled False rather than quietly looking like a full enrollment."""
    body = {**REGISTER_OK, "mtls": {"issued": False, "reason": "CA unreachable"}}
    mock_client = _mock_register(body)

    with patch("httpx.AsyncClient", return_value=mock_client):
        with patch("adk.enrollment._save_workspace"):
            result = await enrollment.rich_enroll(
                "https://identity.example.com",
                "user_token",
                "test-node",
                enable_heartbeat=False,
            )

    assert result["enrolled"] is True
    assert result["cert_enrolled"] is False
