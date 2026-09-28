"""The principals registry survives concurrent mints and is never rewritten from a
file that failed to parse -- either would silently erase every token, the owner's
phone-link tokens included."""

import json
import threading

import pytest

from adk.harnesses.daemon import mint_scoped_token


def test_concurrent_mints_all_survive(tmp_path):
    reg = tmp_path / "harness_tokens.json"
    reg.write_text(json.dumps({"keep-me": {"principal": "phone-link", "plan": "owner"}}))
    errors = []

    def mint(i):
        try:
            for j in range(5):
                mint_scoped_token(f"agent:t{i}-{j}", paths=("/sessions",), plan="agent",
                                  path=reg)
        except Exception as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=mint, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    data = json.loads(reg.read_text())
    assert "keep-me" in data
    assert sum(1 for v in data.values() if v.get("plan") == "agent") == 40
    assert not (tmp_path / "harness_tokens.json.lock").exists()


def test_a_corrupt_registry_is_refused_not_wiped(tmp_path):
    reg = tmp_path / "harness_tokens.json"
    reg.write_text("{ half written")
    with pytest.raises(ValueError):
        mint_scoped_token("agent:x", paths=("/sessions",), plan="agent", path=reg)
    assert reg.read_text() == "{ half written"


def test_a_stale_lock_from_a_crashed_minter_is_taken_over(tmp_path):
    import os
    import time

    reg = tmp_path / "harness_tokens.json"
    lock = tmp_path / "harness_tokens.json.lock"
    lock.write_text("")
    old = time.time() - 120
    os.utime(lock, (old, old))
    mint_scoped_token("agent:y", paths=("/sessions",), plan="agent", path=reg)
    assert json.loads(reg.read_text())
