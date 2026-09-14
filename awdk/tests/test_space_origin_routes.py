"""The node's Space routes: the guard, the cap, and the fail-closed verifier.

Three arms cover the guard, and each must REFUSE — a non-loopback peer, an
Origin that is not first-party, and the kill switch. The mutation that turns
them red is dropping ``_handoff_guard`` from either route.

The fourth arm is the one that is easiest to get wrong and hardest to see:
a POST whose signature does not verify must leave the file BYTE-IDENTICAL.
"Returned 400" and "did not write" are different claims, and only the second
one is the rule — a handler that truncates the record and then refuses has
already destroyed what the device was serving. So the arm asserts the file's
mtime AND its bytes, not the status code alone.

Signed envelopes come from the shared cross-language fixture, so this suite,
the browser signer and the platform verifier are all pinned to one artifact.
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "AitherOS" / "dev" / "tests" / "fixtures" / "space_doc_v1.json"
)

pytestmark = pytest.mark.skipif(
    not _FIXTURE.is_file(),
    reason="the shared space-document fixture is not present in this checkout",
)

GOOD_ORIGIN = "https://aitherium.com"
LOOPBACK = ("127.0.0.1", 41234)
CHALLENGE = "nonce-abcdefgh12345678"


def _cases():
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]


def _app():
    from adk.config import Config
    from adk.server import create_app

    config = Config()
    config.gateway_url = ""
    config.aither_api_key = ""
    agent = MagicMock()
    agent.name = "test"
    agent.llm = MagicMock()
    agent.llm.provider_name = "test"
    agent._identity = MagicMock()
    agent._identity.name = "test"
    agent._identity.description = "Test"
    agent._identity.skills = []
    agent._tools = MagicMock()
    agent._tools.list_tools = MagicMock(return_value=[])
    agent._safety = None
    return create_app(agent=agent, identity="test", config=config)


@pytest.fixture()
def space_file(tmp_path, monkeypatch):
    """Point the node's record at a temp path, never the real home directory."""
    path = tmp_path / "space.json"
    monkeypatch.setenv("AITHER_SPACE_FILE", str(path))
    monkeypatch.delenv("AITHER_BROWSER_HANDOFF", raising=False)
    return path


@pytest.fixture()
def client(space_file):
    # client=LOOPBACK is what makes request.client.host a loopback address.
    # The default TestClient peer is "testclient", which the guard rightly
    # refuses — so a suite that forgets this reports the guard working while
    # exercising nothing else.
    return TestClient(_app(), client=LOOPBACK)


class TestGuard:
    """Loopback peer + first-party Origin + the kill switch. Each REFUSES."""

    def test_a_non_loopback_peer_is_refused(self, space_file):
        remote = TestClient(_app(), client=("203.0.113.7", 5555))
        resp = remote.get("/space", headers={"Origin": GOOD_ORIGIN})
        assert resp.status_code == 403

    def test_an_origin_that_is_not_first_party_is_refused(self, client):
        resp = client.get("/space", headers={"Origin": "https://evil.example.com"})
        assert resp.status_code == 403

    def test_a_two_label_subdomain_is_refused(self, client):
        # The origin rule is ONE label deep on purpose.
        resp = client.get("/space", headers={"Origin": "https://a.b.aitherium.com"})
        assert resp.status_code == 403

    def test_a_missing_origin_is_refused(self, client):
        # A loopback peer is not scarce: every page the user opens runs on this
        # machine. Without an Origin there is nothing to judge, so it refuses.
        assert client.get("/space").status_code == 403

    def test_the_kill_switch_hides_the_surface(self, client, monkeypatch):
        monkeypatch.setenv("AITHER_BROWSER_HANDOFF", "0")
        resp = client.get("/space", headers={"Origin": GOOD_ORIGIN})
        assert resp.status_code == 404

    def test_the_kill_switch_covers_the_write_route_too(self, client, monkeypatch):
        monkeypatch.setenv("AITHER_BROWSER_HANDOFF", "0")
        resp = client.post(
            "/space",
            headers={"Origin": GOOD_ORIGIN},
            json={"envelope": _cases()["good"]["envelope"], "challenge": CHALLENGE},
        )
        assert resp.status_code == 404

    def test_the_write_route_refuses_a_non_loopback_peer(self, space_file):
        remote = TestClient(_app(), client=("203.0.113.7", 5555))
        resp = remote.post(
            "/space",
            headers={"Origin": GOOD_ORIGIN},
            json={"envelope": _cases()["good"]["envelope"], "challenge": CHALLENGE},
        )
        assert resp.status_code == 403
        assert not space_file.exists()


class TestRead:
    def test_no_record_is_none_not_an_error(self, client):
        resp = client.get("/space", headers={"Origin": GOOD_ORIGIN})
        assert resp.status_code == 200
        assert resp.json() == {"state": "none"}

    def test_a_corrupt_record_is_none_never_serving(self, client, space_file):
        space_file.write_text("{not json", encoding="utf-8")
        resp = client.get("/space", headers={"Origin": GOOD_ORIGIN})
        assert resp.status_code == 200
        # Reporting a record it cannot produce would put a Space in the
        # directory that this device cannot serve.
        assert resp.json()["state"] == "none"


class TestWrite:
    def test_a_signed_document_is_written_and_echoed(self, client, space_file):
        case = _cases()["good"]
        resp = client.post(
            "/space",
            headers={"Origin": GOOD_ORIGIN},
            json={"envelope": case["envelope"], "challenge": CHALLENGE},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["state"] == "serving"
        assert body["handle"] == case["expect_handle"]
        assert body["challenge_echo"] == CHALLENGE

        record = json.loads(space_file.read_text(encoding="utf-8"))
        assert record["envelope"] == case["envelope"]
        assert record["handle"] == case["expect_handle"]

        read_back = client.get("/space", headers={"Origin": GOOD_ORIGIN}).json()
        assert read_back["state"] == "serving"
        assert read_back["doc"] == case["envelope"]
        assert read_back["challenge_echo"] == CHALLENGE

    @pytest.mark.parametrize(
        "case_name",
        [
            "X4_missing_sig",
            "X5_wrong_version",
            "X6_payload_not_b64",
            "X7_payload_swapped_under_good_sig",
            "X8_signed_non_json",
        ],
    )
    def test_an_unverifiable_document_does_not_touch_the_file(
        self, client, space_file, case_name
    ):
        cases = _cases()
        # Seed a good record first: the rule is that a refused write leaves the
        # PREVIOUS document intact, which an empty directory cannot show.
        assert (
            client.post(
                "/space",
                headers={"Origin": GOOD_ORIGIN},
                json={"envelope": cases["good"]["envelope"], "challenge": CHALLENGE},
            ).status_code
            == 200
        )
        before_bytes = space_file.read_bytes()
        before_mtime = space_file.stat().st_mtime_ns
        time.sleep(0.01)

        resp = client.post(
            "/space",
            headers={"Origin": GOOD_ORIGIN},
            json={"envelope": cases[case_name]["envelope"], "challenge": CHALLENGE},
        )
        assert resp.status_code == 400, resp.text
        assert space_file.read_bytes() == before_bytes
        assert space_file.stat().st_mtime_ns == before_mtime

    def test_a_document_whose_key_does_not_own_the_handle_is_refused(
        self, client, space_file
    ):
        # The handle IS the key's fingerprint, so a document naming a handle it
        # cannot derive is refused with no directory to consult.
        cases = _cases()
        env = dict(cases["good"]["envelope"])
        env["pubkey"] = cases["X1_pubkey_mismatch"]["expect_pubkey_b64"]
        resp = client.post(
            "/space",
            headers={"Origin": GOOD_ORIGIN},
            json={"envelope": env, "challenge": CHALLENGE},
        )
        assert resp.status_code == 400
        assert not space_file.exists()

    def test_a_missing_challenge_is_refused(self, client, space_file):
        resp = client.post(
            "/space",
            headers={"Origin": GOOD_ORIGIN},
            json={"envelope": _cases()["good"]["envelope"]},
        )
        assert resp.status_code == 400
        assert not space_file.exists()

    def test_a_300_kib_body_is_rejected_at_the_cap(self, client, space_file):
        env = dict(_cases()["good"]["envelope"])
        env["padding"] = "A" * (300 * 1024)
        resp = client.post(
            "/space",
            headers={"Origin": GOOD_ORIGIN},
            json={"envelope": env, "challenge": CHALLENGE},
        )
        assert resp.status_code == 413
        assert not space_file.exists()


def test_the_record_path_default_is_absolute(monkeypatch):
    """A relative default resolves against whatever the cwd happened to be."""
    monkeypatch.delenv("AITHER_SPACE_FILE", raising=False)
    from adk import server as adk_server

    src = Path(adk_server.__file__).read_text(encoding="utf-8")
    assert 'Path.home() / ".aither" / "space.json"' in src
    assert os.path.isabs(str(Path.home() / ".aither" / "space.json"))


# ── The two verifiers, one table ────────────────────────────────────────────
#
# There are TWO implementations of this contract and there always will be: the
# platform's, and this node's. The node cannot import the platform's — the
# package it ships in must not reach the monorepo — so the only thing that can
# keep them in step is the SHARED fixture, driven through both.
#
# They do NOT answer identically, and that is by design rather than drift. The
# node holds no directory row, so four of the fixture's refusals are questions
# it structurally cannot ask. Those are ACCEPTED here, on purpose, each with the
# reason — because an abstention that is written down is a decision, and one
# that is not is a hole somebody later "fixes" by making the node enforce a rule
# it has no data for.
#
# The rest must match the platform's status WORD, not merely its verdict: two
# verifiers refusing the same envelope for two different stated reasons send
# whoever reads the log looking for two different defects.

#: fixture case -> why the NODE cannot judge it, so it writes the document.
_NODE_ABSTAINS = {
    # NOT here, and the fixture is why: M4 keeps the ORIGINAL handle while
    # swapping the key, so the node's own fingerprint check catches it and
    # answers the platform's exact word (`handle_pubkey_mismatch`) with no
    # directory at all. It was listed as an abstention on the first pass and
    # this table refused the claim -- which is the whole reason the table
    # drives every case instead of the ones someone thought to pick.
    "X1_pubkey_mismatch":
        "the envelope nominates a key the DIRECTORY disagrees with; the "
        "envelope itself is self-consistent, so only the directory can object",
    "M5a_replay":
        "monotonicity needs the newest issued_at already accepted for the "
        "handle, which lives in the directory",
    "M5b_future_skew":
        "same: the node keeps no per-handle floor and judges no clock",
    "M6_config_too_large":
        "the anon config byte cap is a platform limit; the node enforces only "
        "its own 256 KiB body cap",
    "X3_schema":
        "the config block schema is the platform's, and validating it here "
        "would be a second copy of a rule that changes without this file",
}


#: fixture case -> (the word the NODE gives, why it differs from the platform's).
#: A third category, and it earns its place: the node REFUSES these, but for a
#: question of its own, because the platform's question needs the directory.
#: Recording the substitute word is what stops "same verdict, two stories" from
#: being discovered by a human reading a log at 2am.
_NODE_OWN_REASON = {
    "X2_handle_mismatch": (
        "handle_pubkey_mismatch",
        "the platform compares the doc's handle to the DIRECTORY's; the node "
        "compares it to the fingerprint of the key that signed it. Both refuse "
        "this envelope, and the node's reason is the only one it has the data "
        "to give.",
    ),
}


def test_every_fixture_case_gets_the_platform_s_own_word_or_a_named_abstention(
    client, space_file
):
    cases = _cases()
    checked = 0
    for name, case in cases.items():
        expect = case.get("expect_status")
        if expect is None:
            continue
        checked += 1
        space_file.unlink(missing_ok=True)
        resp = client.post(
            "/space",
            headers={"Origin": GOOD_ORIGIN},
            json={"envelope": case["envelope"], "challenge": CHALLENGE},
        )
        if name in _NODE_ABSTAINS:
            assert resp.status_code == 200, (
                f"{name}: listed as an abstention ({_NODE_ABSTAINS[name]}) but the "
                f"node refused it. If the node CAN now judge this, delete the entry."
            )
            continue
        if expect == "ok":
            assert resp.status_code == 200, f"{name}: {resp.text}"
            continue
        assert resp.status_code == 400, f"{name}: expected a refusal, got {resp.text}"
        if name in _NODE_OWN_REASON:
            word, why = _NODE_OWN_REASON[name]
            assert resp.json()["detail"] == f"envelope refused: {word}", (
                f"{name}: recorded as refused-for-its-own-reason ({why}) with the "
                f"word {word!r}, but the node said {resp.json()['detail']!r}."
            )
            continue
        assert resp.json()["detail"] == f"envelope refused: {expect}", (
            f"{name}: the node refused for a different REASON than the platform "
            f"({resp.json()['detail']!r} vs {expect!r}) -- same verdict, two stories."
        )
    # An empty table is the cleanest possible false pass this arm could give.
    assert checked >= 18, f"only {checked} fixture cases carried an expect_status"
