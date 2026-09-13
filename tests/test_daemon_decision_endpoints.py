"""Test new decision endpoints: needs-human, triage-patterns, wait."""

import pytest
from fastapi.testclient import TestClient
import time
import asyncio


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Create a test client with isolation."""
    # Isolate to a temp decisions dir
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))

    # Set auth token for testing
    test_token = "test-token-for-testing-only"
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", test_token)

    # Import here to pick up env vars
    from adk.harnesses.daemon import create_app

    app = create_app()

    # Create a test client with auth
    client = TestClient(app)
    # Add bearer auth header to all requests
    client.headers = {"Authorization": f"Bearer {test_token}"}
    return client


class TestNeedsHumanEndpoint:
    """GET /decisions/needs-human filters to decisions only."""

    def test_needs_human_filters_decisions(self, client, tmp_path, monkeypatch):
        """Only DECISION cards returned, context filtered out."""
        monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))

        from adk.decisions.store import get_store, DecisionCard, DecisionOption

        store = get_store()

        # Create a context card (hourly digest) - use valid ID alphabet
        digest = DecisionCard(
            id="d-2n4x",
            title="Hourly digest",
            kind="info",
            options=[],
        )
        store.create(digest)

        # Create a decision card (with options)
        decision = DecisionCard(
            id="d-5bky",
            title="Choose plan",
            default_key="a",
            options=[
                DecisionOption(key="a", label="Plan A"),
                DecisionOption(key="b", label="Plan B"),
            ],
        )
        store.create(decision)

        resp = client.get("/decisions/needs-human")
        assert resp.status_code == 200
        data = resp.json()

        assert data["count"] == 1
        assert len(data["decisions"]) == 1
        assert data["decisions"][0]["id"] == "d-5bky"

    def test_includes_credential_and_blocked(self, tmp_path, monkeypatch):
        """Credential and blocked cards are decisions even without options."""
        # Use fresh temp dir for this test with isolation
        fresh_dir = tmp_path / "fresh_cred_test"
        monkeypatch.setenv("AITHER_DECISIONS_DIR", str(fresh_dir))
        monkeypatch.setenv("AITHER_STEER_DIR", str(fresh_dir / "steer"))

        # Reset the store singleton to force reinitialization
        import adk.decisions.store as store_module
        import threading
        original_store = store_module._STORE
        store_module._STORE = None

        # Set auth token for testing
        test_token = "test-token-for-testing-only"
        monkeypatch.setenv("AITHER_HARNESS_TOKEN", test_token)

        try:
            # Create fresh client and app with new env
            from adk.harnesses.daemon import create_app
            from fastapi.testclient import TestClient
            from adk.decisions.store import get_store, DecisionCard

            app = create_app()
            client = TestClient(app)
            client.headers = {"Authorization": f"Bearer {test_token}"}

            store = get_store()

            # Credential: no options, but is a decision
            cred = DecisionCard(
                id="d-7mq2",
                title="Enter key",
                kind="credential",
                secret_name="API_KEY",
                credential_format="api_key",
                credential_description="Need your API key",
            )
            store.create(cred)

            # Blocked: no options, but is a decision
            blocked = DecisionCard(
                id="d-9vn5",
                title="Permission denied",
                kind="blocked",
            )
            store.create(blocked)

            resp = client.get("/decisions/needs-human")
            assert resp.status_code == 200
            data = resp.json()

            assert data["count"] == 2
            ids = {c["id"] for c in data["decisions"]}
            assert ids == {"d-7mq2", "d-9vn5"}
        finally:
            # Restore original store
            with store_module._STORE_LOCK:
                store_module._STORE = original_store


class TestTriagePatternsEndpoint:
    """GET /decisions/triage-patterns exports patterns for desk."""

    def test_patterns_exported(self, client):
        """Patterns JSON is valid and has expected structure."""
        resp = client.get("/decisions/triage-patterns")
        assert resp.status_code == 200
        data = resp.json()

        assert "decision_kinds" in data
        assert "context_phrases" in data

        # Check structure
        assert isinstance(data["decision_kinds"], list)
        assert isinstance(data["context_phrases"], list)

        # Should have at least the documented kinds
        assert "credential" in data["decision_kinds"]
        assert "blocked" in data["decision_kinds"]

        # Patterns should be strings (regex)
        assert all(isinstance(p, str) for p in data["context_phrases"])


class TestWaitEndpoint:
    """GET /decisions/{id}/wait long-polls for an answer."""

    def test_wait_timeout(self, client, tmp_path, monkeypatch):
        """Timeout returns 408 after specified seconds."""
        monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))

        from adk.decisions.store import get_store, DecisionCard, DecisionOption

        store = get_store()

        # Create a card that won't be answered
        card = DecisionCard(
            id="d-3pqr",
            title="Choose",
            default_key="a",
            options=[DecisionOption(key="a", label="Option A")],
        )
        store.create(card)

        # Wait with short timeout
        resp = client.get("/decisions/d-3pqr/wait?timeout=1")
        # Should timeout after ~1 second
        assert resp.status_code == 408
        assert "timeout" in resp.json()["detail"].lower()

    def test_wait_returns_immediately_if_closed(self, client, tmp_path, monkeypatch):
        """If card is already answered, return immediately."""
        monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))

        from adk.decisions.store import get_store, DecisionCard, DecisionOption

        store = get_store()

        # Create and answer a card
        card = DecisionCard(
            id="d-8wxz",
            title="Choose",
            default_key="a",
            options=[DecisionOption(key="a", label="Option A")],
        )
        store.create(card)
        store.answer(card.id, "a")

        # Wait should return immediately with the answered card
        resp = client.get("/decisions/d-8wxz/wait?timeout=30")
        assert resp.status_code == 200
        data = resp.json()

        assert data["card"]["status"] == "answered"
        assert data["card"]["answer"] == "a"
        assert data["waited_seconds"] < 1.0  # Should be nearly instant

    def test_wait_invalid_card_id(self, client):
        """Waiting on a nonexistent card returns 404."""
        resp = client.get("/decisions/d-fakx/wait?timeout=1")
        assert resp.status_code == 404
