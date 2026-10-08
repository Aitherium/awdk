"""One device, many facets: the machine id every Aither program on a computer shares,
and the lease that lets only one of them heartbeat for it.

The machine id is pinned by a vector shared with Desk (electron/device-identity.test.cjs)
and Identity (dev/tests/test_identity_nodes_one_device.py): change one, all three fail.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from adk import device_identity as di

RAW = "4C4C4544-0042-3510-8051-B4C04F4E4B32"
KEY = "b29b563a42ede6b8309258b8e2d048f4d51649d3f4e62c45b75decd7e2cfa02b"
TENANT_KEY = "d7e41ebcc79d72015735b98b7084bab5a45ee4228c7721d5117075b55426028b"


@pytest.fixture()
def device_file(tmp_path, monkeypatch):
    path = tmp_path / "device.json"
    monkeypatch.setenv(di.DEVICE_FILE_ENV, str(path))
    return path


def test_the_shared_vector():
    assert di.machine_key(RAW) == KEY
    assert di.tenant_machine_id("tnt_acme", KEY) == TENANT_KEY
    assert di.MACHINE_ID_VECTORS == [{"raw": RAW, "tenant": "tnt_acme",
                                      "machine_id": KEY, "tenant_machine_id": TENANT_KEY}]
    # case and whitespace of the OS value do not change the id
    assert di.machine_key(f"  {RAW.lower()}\n") == KEY


def test_machine_id_is_stable_across_a_reinstall(device_file, monkeypatch):
    monkeypatch.setattr(di, "os_machine_id", lambda: RAW)
    first = di.machine_id()
    assert first == KEY
    assert json.loads(device_file.read_text())["machine_id_source"] == "os"
    device_file.unlink()                      # every Aither program removed and reinstalled
    assert di.machine_id() == first


def test_machine_id_is_hashed_per_tenant(device_file, monkeypatch):
    monkeypatch.setattr(di, "os_machine_id", lambda: RAW)
    a, b = di.machine_id("tnt_a"), di.machine_id("tnt_b")
    assert a != b and KEY not in (a, b)
    assert a == di.tenant_machine_id("tnt_a", KEY)


def test_without_an_os_id_a_stored_one_is_kept(device_file, monkeypatch):
    monkeypatch.setattr(di, "os_machine_id", lambda: "")
    first = di.machine_id()
    assert len(first) == 64 and di.machine_id() == first
    assert json.loads(device_file.read_text())["machine_id_source"] == "random"


def test_registration_carries_machine_id_and_facet(device_file, monkeypatch):
    monkeypatch.setattr(di, "os_machine_id", lambda: RAW)
    monkeypatch.setattr(di, "seal_public_key", lambda create=True: "")
    assert di.registration_fields() == {"machine_id": KEY, "facet": "daemon"}
    assert di.registration_fields("awnode")["facet"] == "awnode"


def test_record_facet_keeps_the_answered_node_id(device_file):
    di.record_facet("daemon", "adk-aaaa-1111", capabilities=["llm_small"])
    di.record_facet("desk", "adk-aaaa-1111")
    data = di.load_device()
    assert data["node_id"] == "adk-aaaa-1111"
    assert set(data["facets"]) == {"daemon", "desk"}


# ── the lease (B3) ──────────────────────────────────────────────────────────

def test_one_holder_until_it_misses_three_intervals(device_file):
    t = 1000.0
    assert di.claim_lease("daemon", 60, pid=1, now=t)
    assert not di.claim_lease("daemon", 60, pid=2, now=t + 1)       # node_beat beside adk serve
    assert not di.claim_lease("desk", 60, pid=3, now=t + 1)
    assert di.claim_lease("daemon", 60, pid=1, now=t + 60)           # the holder renews
    assert not di.claim_lease("daemon", 60, pid=2, now=t + 60 + 180)  # exactly 3 missed: not yet
    assert di.claim_lease("daemon", 60, pid=2, now=t + 60 + 181)     # past 3 missed: takeover
    assert not di.claim_lease("daemon", 60, pid=1, now=t + 60 + 182)


def test_a_higher_facet_takes_the_lease_at_once(device_file):
    assert di.claim_lease("desk", 60, pid=3, now=0)
    assert di.claim_lease("daemon", 60, pid=1, now=1)       # daemon outranks desk
    assert not di.claim_lease("desk", 60, pid=3, now=2)
    assert di.claim_lease("awnode", 60, pid=7, now=3)       # awnode outranks daemon
    assert not di.claim_lease("daemon", 60, pid=1, now=4)


def test_release_hands_over_immediately(device_file):
    assert di.claim_lease("daemon", 60, pid=1, now=0)
    di.release_lease("daemon", pid=2)                        # not the holder: no effect
    assert not di.claim_lease("desk", 60, pid=3, now=1)
    di.release_lease("daemon", pid=1)
    assert di.claim_lease("desk", 60, pid=3, now=2)


def test_an_unwritable_device_file_still_beats(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv(di.DEVICE_FILE_ENV, str(blocker / "device.json"))  # parent is a file
    assert di.claim_lease("desk", 60, pid=3, now=0)


def test_a_non_leader_sends_nothing(device_file, monkeypatch):
    from adk import enrollment

    di.claim_lease("awnode", 60, pid=99999, now=__import__("time").time())

    class Client:
        posts = 0

        async def post(self, *a, **k):
            Client.posts += 1
            raise AssertionError("a standby facet must not reach the cloud")

    built = []
    monkeypatch.setattr(enrollment, "build_registration", lambda *a, **k: built.append(1))
    enrollment._heartbeat_state.update(enrollment._new_heartbeat_state())
    asyncio.run(enrollment._heartbeat_beats(
        Client(), "https://idp.invalid", {}, "adk-aaaa-1111", interval=0,
        inference_url=None, node_class="laptop", max_beats=2, reach_provider=None,
        harness_provider=None, beat_immediately=True, facet="daemon"))
    assert Client.posts == 0 and not built
    assert enrollment.heartbeat_status()["last_result"] == "standby"
    assert enrollment.heartbeat_status()["consecutive_failures"] == 0


def test_the_leader_beats(device_file, monkeypatch):
    from adk import enrollment

    class Resp:
        status_code = 200

        def json(self):
            return {"status": "ok"}

    sent = []

    class Client:
        async def post(self, url, json=None, headers=None):
            sent.append((url, json))
            return Resp()

    reg = {"inference_ready": False, "available_models": [], "gpu_vram_mb": 0,
           "inference_url": "", "inference_kind": "none"}
    monkeypatch.setattr(enrollment, "build_registration", lambda *a, **k: dict(reg))
    asyncio.run(enrollment._heartbeat_beats(
        Client(), "https://idp.invalid", {}, "adk-aaaa-1111", interval=0,
        inference_url=None, node_class="laptop", max_beats=1, reach_provider=None,
        harness_provider=None, beat_immediately=True, facet="daemon"))
    assert len(sent) == 1 and sent[0][0].endswith("/v1/nodes/heartbeat")
    assert di.load_device()["leader"]["facet"] == "daemon"
