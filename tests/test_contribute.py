"""adk contribute: consent defaults OFF, redaction happens before anything leaves,
uploads fail closed, and with consent off not one socket is opened.

Every planted value below is assembled at runtime from fragments so the
literal never sits in the file; each is a fake of a real shape and
authenticates nothing.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import types

import pytest

from adk import contribute as ctb

PLANTS = {
    "anthropic": "sk-" + "ant-" + "api03-" + "A1b2C3d4" * 12,
    "github": "gh" + "p_" + "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Jh1Gf0EdCbAz",
    "aws": "AK" + "IA" + "QWERTYUIOPASDFGH",
    "email": "jane.customer" + "@" + "examplecorp.com",
    "phone": "(415) 555-" + "0142",
    "ipv4": "10.44." + "12.7",
    "host": "build-" + "box7.corp",
    "tenant": "tnt_" + "acmewidgets",
    "customer": "Globex" + "Industries",
    "home": "C:\\Users\\" + "jdoe" + "\\projects\\x.py",
    "kvval": "Pa55" + "w0rdZ!xyz",
}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    chome = tmp_path / "contribute"
    mine = tmp_path / "awmine"
    chome.mkdir()
    mine.mkdir()
    monkeypatch.setenv("AITHER_CONTRIBUTE_HOME", str(chome))
    monkeypatch.setenv("AWMINE_OUT", str(mine))
    monkeypatch.delenv("AITHER_CONTRIBUTE_URL", raising=False)
    monkeypatch.delenv("AITHER_CONTRIBUTE_DENY", raising=False)
    # Never this machine's real sign-in: uploads have a default endpoint, so an
    # ambient token would let a test reach the network.
    monkeypatch.delenv("AITHER_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(ctb, "_token", lambda: os.environ.get("AITHER_AUTH_TOKEN") or None)
    (chome / "denylist.txt").write_text(PLANTS["customer"] + "\n", encoding="utf-8")
    # The audit engine (awmine) is optional and absent in CI. Stub it with an
    # identity redactor so every planted-value assertion below proves the
    # module's OWN patterns catch it; the real suspect scan is used when present.
    monkeypatch.setattr(ctb, "_awmine_redact", lambda: _AuditStub)
    return chome, mine


class _AuditStub:
    @staticmethod
    def redact_text(text, deny):
        return text, {}

    @staticmethod
    def suspect_hits(text):
        try:
            from awmine.redact import suspect_hits
        except ImportError:
            return []
        return suspect_hits(text)


def _plant(chome, mine):
    text = (f"deploying for {PLANTS['customer']} ({PLANTS['tenant']}) on {PLANTS['host']} "
            f"at {PLANTS['ipv4']}; mail {PLANTS['email']} or call {PLANTS['phone']}; "
            f"key {PLANTS['anthropic']} and {PLANTS['github']} and {PLANTS['aws']}; "
            f"file {PLANTS['home']}; DB_PASSWORD={PLANTS['kvval']}")
    rows = [{"claim": text, "human": "fix it", "session_id": "s1"},
            {"claim": "ran tests at 12:30:45", "api_token": PLANTS["kvval"]}]
    (mine / "lessons.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    skill = chome / "skills" / "my-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: my-skill\n---\n" + text, encoding="utf-8")
    (skill / ".env").write_text("OPENAI_API_KEY=" + PLANTS["anthropic"], encoding="utf-8")
    (skill / ".gitignore").write_text("drafts/\n", encoding="utf-8")
    (skill / "drafts").mkdir()
    (skill / "drafts" / "notes.md").write_text("internal " + PLANTS["customer"],
                                                encoding="utf-8")
    pack = chome / "packs" / "my-pack"
    pack.mkdir(parents=True)
    (pack / "manifest.json").write_text(json.dumps({"name": "my-pack", "owner":
                                                    PLANTS["email"]}), encoding="utf-8")


@pytest.fixture()
def no_network(monkeypatch):
    calls = []

    def _deny(*a, **k):
        calls.append(a)
        raise AssertionError("network touched")

    monkeypatch.setattr(socket.socket, "connect", _deny)
    monkeypatch.setattr(socket, "create_connection", _deny)
    monkeypatch.setattr(socket, "getaddrinfo", _deny)
    return calls


def test_consent_defaults_off(home):
    rec = ctb.load_consent()
    assert rec["categories"] == {c: False for c in ctb.CATEGORIES}
    assert ctb.build_batch()["items"] == []


def test_corrupt_consent_reads_as_off(home):
    (home[0] / "consent.json").write_text('{"categories": {"skills": "yes"}}', encoding="utf-8")
    assert ctb.load_consent()["categories"]["skills"] is False


def test_on_off_records_version_and_time(home):
    rec = ctb.set_consent(["skills"], True)
    assert rec["categories"]["skills"] is True
    assert rec["categories"]["harness_logs"] is False
    assert rec["contributor_id"].startswith("ctb_")
    assert rec["consent_version"] == ctb.CONSENT_VERSION and rec["updated_at"]
    rec = ctb.set_consent([], False)
    assert not any(rec["categories"].values())
    assert [h["action"] for h in rec["history"]] == ["on", "off"]


def test_unknown_category_refused(home):
    with pytest.raises(ctb.ContributeError):
        ctb.set_consent(["browser_history"], True)


def test_preview_carries_no_planted_value(home):
    _plant(*home)
    ctb.set_consent([], True)
    batch = ctb.build_batch()
    blob = json.dumps(batch, ensure_ascii=False)
    for name, value in PLANTS.items():
        assert value not in blob, f"{name} survived redaction"
    assert "jdoe" not in blob
    cats = {it["category"] for it in batch["items"]}
    assert {"harness_logs", "skills", "packs"} <= cats
    reasons = {(d.get("file"), d["reason"]) for d in batch["dropped"]}
    assert (".env", "secret-file") in reasons
    assert ("drafts/notes.md", "gitignored") in reasons
    assert batch["audit"]["judged"] is True


def test_clock_time_is_not_an_address(home):
    out, hits = ctb.redact_text("ran at 12:30:45 and 2026-10-22", deny=[])
    assert out == "ran at 12:30:45 and 2026-10-22" and not hits
    out, _ = ctb.redact_text("peer fe80::1ff:fe23:4567:890a up", deny=[])
    assert "fe80" not in out


def test_redaction_is_a_fixed_point(home):
    _plant(*home)
    ctb.set_consent([], True)
    for it in ctb.build_batch()["items"]:
        again, _ = ctb.redact_obj(it["payload"], ctb.load_denylist())
        assert again == it["payload"]


def test_preview_off_category_needs_explicit_ask(home):
    _plant(*home)
    assert ctb.build_batch()["categories"] == []
    forced = ctb.build_batch(["skills"], respect_consent=False)
    assert forced["categories"] == ["skills"] and forced["items"]


def test_consent_off_sends_nothing(home, no_network):
    _plant(*home)
    res = ctb.upload()
    assert res.status == "consent_off" and res.sent == 0
    assert no_network == []
    assert not (home[0] / "outbox").exists()


def test_uploads_fail_closed_until_terms(home, no_network, monkeypatch):
    _plant(*home)
    monkeypatch.setenv("AITHER_CONTRIBUTE_URL", "https://intake.example")
    monkeypatch.setenv("AITHER_AUTH_TOKEN", "t")
    ctb.set_consent([], True)
    monkeypatch.setattr(ctb, "TERMS_VERSION", None)
    res = ctb.upload()
    assert res.status == "refused" and "terms" in res.reason
    assert no_network == []


def test_audit_unavailable_blocks_send(home, no_network, monkeypatch):
    _plant(*home)
    monkeypatch.setattr(ctb, "TERMS_VERSION", "test-terms")
    monkeypatch.setattr(ctb, "_awmine_redact", lambda: None)
    monkeypatch.setenv("AITHER_CONTRIBUTE_URL", "https://intake.example")
    monkeypatch.setenv("AITHER_AUTH_TOKEN", "t")
    ctb.set_consent([], True)
    res = ctb.upload()
    assert res.status == "refused" and "audit" in res.reason
    assert no_network == []


def test_audit_drops_item_redactor_cannot_fix(home, monkeypatch):
    class _Strict:
        @staticmethod
        def redact_text(t, d):
            return t, {}

        @staticmethod
        def suspect_hits(t):
            return ["planted"] if "SENTINEL" in t else []

    monkeypatch.setattr(ctb, "_awmine_redact", lambda: _Strict)
    (home[1] / "lessons.jsonl").write_text(json.dumps({"claim": "SENTINEL"}) + "\n",
                                           encoding="utf-8")
    ctb.set_consent(["harness_logs"], True)
    batch = ctb.build_batch()
    assert batch["items"] == []
    assert batch["dropped"][0]["reason"] == "audit"


class _FakeResp:
    def __init__(self, code):
        self.status_code = code


class _FakeClient:
    posts: list = []
    fail_after = None

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None, headers=None):
        if _FakeClient.fail_after is not None and len(_FakeClient.posts) >= _FakeClient.fail_after:
            return _FakeResp(503)
        _FakeClient.posts.append((url, json, headers))
        return _FakeResp(202)


def test_send_is_batched_resumable_and_redacted(home, monkeypatch):
    _plant(*home)
    monkeypatch.setattr(ctb, "TERMS_VERSION", "test-terms")
    monkeypatch.setattr(ctb, "BATCH_ITEMS", 1)
    monkeypatch.setenv("AITHER_CONTRIBUTE_URL", "https://intake.example")
    monkeypatch.setenv("AITHER_AUTH_TOKEN", "t")
    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(Client=_FakeClient))
    _FakeClient.posts = []
    _FakeClient.fail_after = 2  # the consent record, then one batch
    ctb.set_consent([], True)
    first = ctb.upload()
    assert first.status == "partial" and first.sent == 1
    assert list((home[0] / "outbox").glob("batch-*.json"))
    _FakeClient.fail_after = None
    second = ctb.upload()
    assert second.status == "sent" and second.sent >= 1
    assert not list((home[0] / "outbox").glob("batch-*.json"))
    blob = json.dumps([p[1] for p in _FakeClient.posts], ensure_ascii=False)
    for name, value in PLANTS.items():
        assert value not in blob, f"{name} reached the wire"
    assert _FakeClient.posts[0][0].endswith("/v1/contribute/consent")
    assert _FakeClient.posts[0][1]["licences"]["skills"] == "train-only"
    for url, body, headers in [p for p in _FakeClient.posts if p[0].endswith("/batches")]:
        assert headers["Idempotency-Key"] == body["batch_id"]
        assert body["terms_version"] == "test-terms"
    third = ctb.upload()
    assert third.status == "sent" and third.sent == 0


def test_delete_withdraws_and_queues_removal(home, no_network):
    ctb.set_consent([], True)
    ob = home[0] / "outbox"
    ob.mkdir()
    (ob / "batch-abc.json").write_text("{}", encoding="utf-8")
    res = ctb.delete_all()
    assert res["deletion_queued"] and res["staged_removed"] == 1
    assert not any(ctb.load_consent()["categories"].values())
    req = json.loads((ob / "deletion.json").read_text(encoding="utf-8"))
    assert req["contributor_id"].startswith("ctb_")
    # The queued removal still obeys the terms gate: nothing goes out yet.
    assert ctb.upload().status == "refused"
    assert no_network == []


def test_cli_status_and_preview(home, capsys):
    from adk.cli import _cmd_contribute_data

    ns = types.SimpleNamespace
    assert _cmd_contribute_data(ns(), "status") == 0
    assert "harness_logs" in capsys.readouterr().out
    _plant(*home)
    assert _cmd_contribute_data(ns(categories=["skills"]), "on") == 0
    capsys.readouterr()
    assert _cmd_contribute_data(ns(category=[], limit=5, json=True), "preview") == 0
    out = capsys.readouterr().out
    assert json.loads(out)["categories"] == ["skills"]
    for value in PLANTS.values():
        assert value not in out


def _can_symlink(tmp: str) -> bool:
    import os
    import tempfile

    d = tempfile.mkdtemp(dir=tmp)
    try:
        os.symlink(__file__, os.path.join(d, "l"))
    except (OSError, NotImplementedError):
        return False
    return True


_SYMLINKS = _can_symlink(__import__("tempfile").gettempdir())


def test_non_string_values_are_redacted(home):
    obj = {"password": 482913, "api_key": ["s3cr3tvalue"],
           "auth": {"Bearer": "abcd1234efgh"}, "phone": 4155550142}
    out, hits = ctb.redact_obj(obj, deny=[])
    blob = json.dumps(out)
    for v in ("482913", "s3cr3tvalue", "abcd1234efgh", "4155550142"):
        assert v not in blob
    assert hits
    assert ctb.audit_obj(out, deny=[]).clean


def test_audit_flags_unredacted_credential_key(home):
    a = ctb.audit_obj({"password": 482913}, deny=[])
    assert a.judged and not a.clean
    assert any(f.startswith("credential-key:") for f in a.findings)


def test_pathological_inputs_are_linear(home):
    import time as _t

    for s in ("a." * 32768, "a" * 65536, "a:" * 32768, "1." * 32768):
        t0 = _t.monotonic()
        ctb.redact_text(s, deny=[])
        assert _t.monotonic() - t0 < 3.0, f"slow on {s[:4]!r}x{len(s)}"


def test_secret_dirs_are_never_read(home):
    skill = home[0] / "skills" / "s"
    (skill / "secrets").mkdir(parents=True)
    (skill / "secrets" / "notes.md").write_text("very private words", encoding="utf-8")
    (skill / "SKILL.md").write_text("a skill body long enough", encoding="utf-8")
    batch = ctb.build_batch(["skills"], respect_consent=False)
    assert "very private words" not in json.dumps(batch)
    assert {"file": "secrets/notes.md", "reason": "secret-dir"}.items() <= next(
        d for d in batch["dropped"] if d.get("file") == "secrets/notes.md").items()


@pytest.mark.skipif(not _SYMLINKS, reason="this OS user cannot create symlinks")
def test_symlink_out_of_tree_is_not_followed(home, tmp_path):
    import os

    target = tmp_path / "outside.md"
    target.write_text("auth material outside the skill", encoding="utf-8")
    skill = home[0] / "skills" / "s"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("a skill body long enough", encoding="utf-8")
    os.symlink(target, skill / "notes.md")
    batch = ctb.build_batch(["skills"], respect_consent=False)
    assert "auth material" not in json.dumps(batch)


def test_turning_off_drops_staged_batches(home):
    ctb.set_consent([], True)
    ob = home[0] / "outbox"
    ob.mkdir()
    (ob / "batch-old.json").write_text("{}", encoding="utf-8")
    ctb.set_consent(["harness_logs"], False)
    assert not list(ob.glob("batch-*.json"))


def test_parent_repo_gitignore_applies(home, tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    skill = repo / "skills" / "s"
    skill.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("*.local.md\n", encoding="utf-8")
    (skill / "SKILL.md").write_text("a skill body long enough", encoding="utf-8")
    (skill / "notes.local.md").write_text("ignored by the parent repo", encoding="utf-8")
    files, dropped = ctb._read_tree(skill)
    assert "notes.local.md" not in files
    assert {"file": "notes.local.md", "reason": "gitignored"} in dropped


def test_licence_defaults_to_train_only(home):
    rec = ctb.set_consent(["skills"], True)
    assert rec["licences"]["skills"] == "train-only"
    assert set(rec["licences"]) == set(ctb.CATEGORIES)


def test_licence_is_chosen_per_category(home):
    ctb.set_consent(["skills"], True, "cc-by", "Ada")
    ctb.set_consent(["harness_logs"], True, "cc0")
    rec = ctb.load_consent()
    assert rec["licences"]["skills"] == "cc-by-4.0"
    assert rec["licences"]["harness_logs"] == "cc0-1.0"
    assert rec["licences"]["tools"] == "train-only"
    assert rec["credit_name"] == "Ada"


def test_unknown_licence_refused_and_corrupt_falls_back_narrow(home):
    with pytest.raises(ctb.ContributeError):
        ctb.set_consent(["skills"], True, "all-rights-to-everyone")
    (home[0] / "consent.json").write_text(
        '{"categories": {"skills": true}, "licences": {"skills": "mit"}}', encoding="utf-8")
    assert ctb.load_consent()["licences"]["skills"] == "train-only"


def test_items_carry_their_licence(home):
    _plant(*home)
    ctb.set_consent(["skills"], True, "cc-by-4.0")
    ctb.set_consent(["harness_logs"], True)
    batch = ctb.build_batch()
    lic = {it["category"]: it["licence"] for it in batch["items"]}
    assert lic["skills"] == "cc-by-4.0" and lic["harness_logs"] == "train-only"


def test_changing_licence_drops_staged_batches(home):
    ctb.set_consent([], True)
    ob = home[0] / "outbox"
    ob.mkdir()
    (ob / "batch-old.json").write_text("{}", encoding="utf-8")
    ctb.set_consent(["skills"], True, "cc0")
    assert not list(ob.glob("batch-*.json"))
