"""auth.md client — agent registration & credential acquisition.

Implements the AGENT side of the auth.md protocol (github.com/workos/auth.md):
discovering an auth.md service, registering an agent identity, handling the
optional claim ceremony (human confirmation), exchanging for an access_token,
and refreshing/revoking credentials.

Public API:
    AuthMdClient — the main orchestrator. Stateful per registration.
    AuthMdStore — credential persistence (workspace-scoped vault).
    AuthMdError — protocol-level rejection with error code.
    ConsentRequiredError — ceremony required, surface code+URL to human.

Usage:
    client = AuthMdClient()
    # On 401 from an API call, extract WWW-Authenticate header:
    await client.discover("https://api.service.example.com/")

    # Pick a method (identity_assertion, service_auth, anonymous):
    reg = await client.register(method="anonymous")

    # Optional: if ceremony required, surface to user:
    try:
        token = await client.exchange_assertion(reg)
    except ConsentRequiredError as e:
        print(f"Visit: {e.verification_uri}")
        print(f"Enter code: {e.user_code}")
        token = await client.poll_ceremony(e, timeout_s=600)

    # Use the token:
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    response = await client.http_client.get("/api/resource", headers=headers)

    # On expiry, refresh by re-exchanging the cached identity_assertion:
    token = await client.exchange_assertion(reg)
"""

from .client import AuthMdClient, AuthMdError, ConsentRequiredError
from .store import AuthMdStore

__all__ = [
    "AuthMdClient",
    "AuthMdError",
    "ConsentRequiredError",
    "AuthMdStore",
]
