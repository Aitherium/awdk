"""A config write that cannot read the file must REFUSE, not truncate it.

`save_saved_config` is a whole-file overwrite that merges into whatever
`load_saved_config` returns -- and that loader swallows every read failure into
an empty dict. So a read that fails for any reason (a transient lock, a
half-written file, an import error in the yaml branch) leaves nothing to merge
into, and the write then DELETES every key the caller did not pass.

Measured 2026-08-23 on the fleet host: ~/.aither/config.yaml went from 11 keys to
exactly the TWO that `adk relay` writes (relay_token, relay_nick), losing url,
gateway_url, safety_level, stream, rich_output, show_thinking, show_metadata and
session_id. The caller succeeded, nothing logged, and the loss surfaced only
because an unrelated gate read one of the dropped keys an hour later.

Same shape as the fail-open gate in security-review-patterns.md #1, applied to a
file rather than an authz decision: every error path has to reach the REFUSAL,
because the alternative is silent data loss on a file that holds a credential.
"""

import json

import pytest

from adk.config import load_saved_config, save_saved_config


def test_merge_preserves_keys_the_caller_did_not_pass(tmp_path):
    """The happy path, asserted positively -- a refusal-only test would pass on
    an implementation that refuses everything and never writes at all."""
    p = tmp_path / "config.yaml"
    p.write_text(json.dumps({"url": "http://x", "safety_level": "professional"}),
                 encoding="utf-8")

    save_saved_config({"relay_token": "tok", "relay_nick": "nick"}, config_path=p)

    got = json.loads(p.read_text(encoding="utf-8"))
    assert got["relay_token"] == "tok", "the new key was not written"
    assert got["url"] == "http://x", "an existing key was destroyed by the merge"
    assert got["safety_level"] == "professional", "an existing key was destroyed"


def test_refuses_to_write_over_a_file_it_could_not_parse(tmp_path):
    """The regression. A non-empty file that parses to nothing must abort the
    write -- writing would leave exactly the caller's keys and nothing else."""
    p = tmp_path / "config.yaml"
    p.write_text("\x00\x01 not json, not yaml: [unclosed", encoding="utf-8")
    before = p.read_text(encoding="utf-8")

    with pytest.raises(OSError) as exc:
        save_saved_config({"relay_token": "tok"}, config_path=p)

    assert "refusing to write" in str(exc.value)
    assert p.read_text(encoding="utf-8") == before, \
        "the file was modified despite the refusal"


def test_an_absent_file_is_still_created(tmp_path):
    """The refusal must not break first-run. No file means nothing to lose, so
    the write proceeds -- a guard that also blocked creation would make the CLI
    unable to save anything on a fresh machine."""
    p = tmp_path / "nested" / "config.yaml"

    save_saved_config({"relay_token": "tok"}, config_path=p)

    assert json.loads(p.read_text(encoding="utf-8")) == {"relay_token": "tok"}


def test_an_empty_file_is_treated_as_absent(tmp_path):
    """A zero-byte file carries no keys, so there is nothing to protect."""
    p = tmp_path / "config.yaml"
    p.write_text("", encoding="utf-8")

    save_saved_config({"relay_token": "tok"}, config_path=p)

    assert json.loads(p.read_text(encoding="utf-8")) == {"relay_token": "tok"}


def test_round_trip_through_the_loader(tmp_path):
    """What was written must be what the loader reads back -- the two halves are
    what the whole file is for."""
    p = tmp_path / "config.yaml"
    p.write_text(json.dumps({"url": "http://x"}), encoding="utf-8")

    save_saved_config({"search_url": "https://127.0.0.1:8114"}, config_path=p)

    got = load_saved_config(p)
    assert got["search_url"] == "https://127.0.0.1:8114"
    assert got["url"] == "http://x"
