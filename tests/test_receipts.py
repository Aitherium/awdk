"""Receipts: the hash-chained, Ed25519-signed action log (adk.receipts).

Every tamper case must exit 1, every cannot-judge case must exit 2, and an
untouched log must exit 0. A verifier that says 0 on silence is the failure
this module exists to prevent, so the cannot-judge cases matter as much as the
tamper ones.
"""

import json
import threading

import pytest

pytest.importorskip("cryptography")

from adk import receipts  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolate every key location: no awseal key, fallback key under tmp."""
    agent_home = tmp_path / "agent-home"
    monkeypatch.setattr(receipts, "_home_dir", lambda: agent_home)
    monkeypatch.setattr(receipts, "_awseal_private_key", lambda: None)
    for var in (receipts.PATH_ENV, receipts.KEY_ENV, receipts.PUBKEY_ENV):
        monkeypatch.delenv(var, raising=False)
    return agent_home


def _write_log(home, rows=4):
    log = home / "actions.jsonl"
    for i in range(rows):
        receipts.append("tool", f"tool_{i}", {"i": i}, {"ok": True},
                        approval="auto", path=log)
    return log


def _lines(log):
    return log.read_bytes().split(b"\n")[:-1]


def _put(log, lines):
    log.write_bytes(b"\n".join(lines) + b"\n")


def test_intact_log_verifies_zero(home):
    log = _write_log(home)
    code, reason = receipts.check(log)
    assert code == 0, reason
    rows = [json.loads(ln) for ln in _lines(log)]
    assert [r["seq"] for r in rows] == [0, 1, 2, 3]
    assert rows[0]["prev_sha256"] == receipts.GENESIS
    assert all(r["signed"] and r["sig"] for r in rows)


def test_fallback_key_is_created_mode_600(home):
    _write_log(home, rows=1)
    key = receipts.fallback_key_path()
    assert key.is_file()
    import os
    if os.name == "posix":
        assert (key.stat().st_mode & 0o777) == 0o600


def test_byte_edit_is_tampered(home):
    log = _write_log(home)
    data = bytearray(log.read_bytes())
    idx = data.index(b"tool_2")
    data[idx + 5] = ord("9")  # tool_2 -> tool_9
    log.write_bytes(bytes(data))
    assert receipts.verify(log) == 1


def test_edit_in_last_row_is_tampered_by_signature(home):
    """The last row has no successor to break its chain; the signature must catch it."""
    log = _write_log(home)
    lines = _lines(log)
    lines[-1] = lines[-1].replace(b'"auto"', b'"owner"')
    _put(log, lines)
    code, reason = receipts.check(log)
    assert code == 1 and "signature" in reason


def test_deleted_line_is_tampered(home):
    log = _write_log(home)
    lines = _lines(log)
    del lines[1]
    _put(log, lines)
    assert receipts.verify(log) == 1


def test_swapped_lines_are_tampered(home):
    log = _write_log(home)
    lines = _lines(log)
    lines[1], lines[2] = lines[2], lines[1]
    _put(log, lines)
    assert receipts.verify(log) == 1


def test_resigned_with_foreign_key_is_not_trusted(home, tmp_path):
    """A row signed by a key the verifier does not know cannot pass as intact."""
    log = _write_log(home, rows=2)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    other = tmp_path / "other.key"
    other.write_bytes(Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    receipts.append("tool", "forged", {}, {}, path=log, key_path=other)
    assert receipts.verify(log) == 2


def test_missing_key_cannot_judge(home):
    log = _write_log(home)
    receipts.fallback_key_path().unlink()
    code, reason = receipts.check(log)
    assert code == 2, reason


def test_missing_key_with_published_pubkey_still_verifies(home):
    log = _write_log(home)
    pub = receipts._pub_hex(receipts._fallback_private_key(create=False))
    receipts.fallback_key_path().unlink()
    assert receipts.verify(log, pubkey=pub) == 0


def test_missing_file_cannot_judge(home):
    assert receipts.verify(home / "nope.jsonl") == 2


def test_unsigned_rows_cannot_judge_but_tamper_still_detected(home, monkeypatch):
    monkeypatch.setattr(receipts, "_signing_key", lambda key_path=None, create=True: None)
    log = _write_log(home, rows=3)
    rows = [json.loads(ln) for ln in _lines(log)]
    assert all(r["signed"] is False and r["sig"] == "" for r in rows)
    assert receipts.verify(log) == 2
    lines = _lines(log)
    del lines[0]
    _put(log, lines)
    assert receipts.verify(log) == 1


def test_unsigned_middle_row_edit_is_caught_by_the_chain(home, monkeypatch):
    """With no key, only prev_sha256 can see an in-place edit of a middle row:
    seq still matches and there is no signature to fail."""
    monkeypatch.setattr(receipts, "_signing_key", lambda key_path=None, create=True: None)
    log = _write_log(home, rows=3)
    lines = _lines(log)
    row = json.loads(lines[1])
    row["name"] = "something_else"
    lines[1] = json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _put(log, lines)
    code, reason = receipts.check(log)
    assert code == 1 and "chain broken" in reason, reason


def test_signed_row_spliced_from_another_log_is_caught_by_the_chain(home):
    """Same key, same seq, a valid signature: only the chain can reject a row
    lifted from a different (e.g. older) copy of the log."""
    log_a = home / "a.jsonl"
    log_b = home / "b.jsonl"
    for i in range(3):
        receipts.append("tool", f"a_{i}", {"i": i}, "ok", approval="auto", path=log_a)
        receipts.append("tool", f"b_{i}", {"i": i}, "ok", approval="auto", path=log_b)
    assert receipts.verify(log_a) == 0 and receipts.verify(log_b) == 0
    lines_b = _lines(log_b)
    lines_b[1] = _lines(log_a)[1]
    _put(log_b, lines_b)
    code, reason = receipts.check(log_b)
    assert code == 1 and "chain broken" in reason, reason


def test_no_plaintext_secret_in_log(home):
    log = home / "actions.jsonl"
    secrets = [
        "Bearer demo.fixture-value.not-a-real-token",
        "hunter2-correct-horse",
        "481516",
        "NOTAREALKEYnotarealkeyNOTAREALKEY0000",
    ]
    receipts.append(
        "tool", "send_email",
        {"to": "bank@example.com", "password": secrets[1],
         "body": f"your code is {secrets[2]} key {secrets[0]}"},
        f"sent with token {secrets[3]}",
        path=log)
    text = log.read_text(encoding="utf-8")
    for s in secrets:
        assert s not in text
    row = json.loads(text.splitlines()[0])
    assert row["args_sha256"] == receipts.digest(
        {"to": "bank@example.com", "password": secrets[1],
         "body": f"your code is {secrets[2]} key {secrets[0]}"})
    assert len(row["args_preview"]) <= receipts.PREVIEW_CHARS
    assert len(row["result_preview"]) <= receipts.PREVIEW_CHARS
    assert receipts.verify(log) == 0


def test_preview_is_truncated(home):
    assert len(receipts.preview("word " * 200)) <= receipts.PREVIEW_CHARS


def test_tail_returns_last_n(home):
    log = _write_log(home, rows=5)
    rows = receipts.tail(2, path=log)
    assert [r["name"] for r in rows] == ["tool_3", "tool_4"]
    assert receipts.tail(3, path=home / "missing.jsonl") == []


def test_env_path_override(home, monkeypatch):
    target = home / "env" / "log.jsonl"
    monkeypatch.setenv(receipts.PATH_ENV, str(target))
    receipts.append("tool", "x", {}, {})
    assert target.is_file()
    assert receipts.verify() == 0


def test_concurrent_appends_keep_the_chain(home):
    log = home / "actions.jsonl"

    def worker(n):
        for i in range(10):
            receipts.append("tool", f"t{n}-{i}", {"i": i}, None, path=log)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(_lines(log)) == 40
    assert receipts.verify(log) == 0
