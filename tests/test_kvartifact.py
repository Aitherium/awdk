"""adk.kvartifact: a KV cache as a portable, sha256-verified, model-revision-bound file,
and SlotStore export/import over it."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from adk import kvartifact as ka
from adk.kvpool import model_fingerprint
from adk.kvslot import SlotStore, scoped_fingerprint

PROPS = {
    "model_path": "/models/qwen3-8b-q4_k_m.gguf",
    "build_info": "b6500-abc123",
    "default_generation_settings": {"n_ctx": 32768},
}


def _binding(**over) -> ka.KVBinding:
    known = {"cache_type_k": "f16", "cache_type_v": "f16", "split": "single:5090", **over}
    return ka.KVBinding.from_llama_props(PROPS, **known)


def _payload(tmp: Path, n: int = 300_000) -> Path:
    p = tmp / "slot.bin"
    p.write_bytes(os.urandom(n))
    return p


def test_binding_from_props_and_digest_is_stable():
    b = _binding()
    assert b.model == PROPS["model_path"] and b.build == "b6500-abc123" and b.n_ctx == 32768
    assert b.digest() == _binding().digest()
    assert b.digest() != _binding(cache_type_k="q8_0").digest()
    assert b.diff(_binding(split="rpc:dgx,5090")) == {
        "split": ("single:5090", "rpc:dgx,5090")
    }
    # the informational note is not part of validity
    assert b.digest() == _binding(note="built at 03:00").digest()
    with pytest.raises(ValueError):
        ka.KVBinding.from_llama_props({})


def test_pack_verify_unpack_round_trip(tmp_path):
    src = _payload(tmp_path)
    art = tmp_path / "prefix.awkv"
    h = ka.pack(src, art, binding=_binding(), key="ab" * 16, tokens=1536, scope_fp="cd" * 8)
    assert h.payload_nbytes == src.stat().st_size
    got = ka.verify(art, _binding(), scope_fp="cd" * 8)
    assert got.tokens == 1536 and got.key == "ab" * 16
    out = tmp_path / "restored.bin"
    ka.unpack(art, out, _binding(), scope_fp="cd" * 8)
    assert out.read_bytes() == src.read_bytes()


@pytest.mark.parametrize("field,value", [
    ("cache_type_k", "q8_0"),          # KV quant changed
    ("split", "rpc:dgx,5090 ts=3,1"),  # layer split changed
    ("rope", "yarn:4"),                # RoPE scaling changed (post-RoPE K differs)
    ("build", "b6600-def456"),         # server build changed
    ("weights_sha256", "00" * 32),     # different weights revision
])
def test_binding_mismatch_refused_before_any_payload_is_written(tmp_path, field, value):
    art = tmp_path / "a.awkv"
    ka.pack(_payload(tmp_path), art, binding=_binding(), key="aa", tokens=1)
    out = tmp_path / "out.bin"
    with pytest.raises(ka.BindingMismatchError) as exc:
        ka.unpack(art, out, _binding(**{field: value}))
    assert field in exc.value.fields and field in str(exc.value)
    assert not out.exists() and not list(tmp_path.glob("*.tmp"))


def test_scope_mismatch_refused(tmp_path):
    art = tmp_path / "a.awkv"
    ka.pack(_payload(tmp_path), art, binding=_binding(), scope_fp="11" * 8)
    with pytest.raises(ka.ScopeMismatchError):
        ka.verify(art, _binding(), scope_fp="22" * 8)


def test_flipped_payload_byte_is_a_digest_mismatch_and_leaves_nothing(tmp_path):
    art = tmp_path / "a.awkv"
    ka.pack(_payload(tmp_path), art, binding=_binding())
    raw = bytearray(art.read_bytes())
    raw[-1000] ^= 0x01
    art.write_bytes(bytes(raw))
    out = tmp_path / "out.bin"
    with pytest.raises(ka.DigestMismatchError):
        ka.unpack(art, out, _binding())
    assert not out.exists()


def test_truncated_payload_is_a_digest_mismatch(tmp_path):
    art = tmp_path / "a.awkv"
    ka.pack(_payload(tmp_path), art, binding=_binding())
    art.write_bytes(art.read_bytes()[:-10])
    with pytest.raises(ka.DigestMismatchError):
        ka.verify(art, _binding())


def test_tampered_header_binding_is_a_format_error(tmp_path):
    art = tmp_path / "a.awkv"
    ka.pack(_payload(tmp_path, 10), art, binding=_binding(cache_type_k="q8_0"))
    raw = art.read_bytes()
    # rewrite the header to CLAIM f16 without fixing the digest: must not pass as f16
    forged = raw.replace(b'"cache_type_k": "q8_0"', b'"cache_type_k": "f16" ', 1)
    assert forged != raw
    art.write_bytes(forged)
    with pytest.raises(ka.FormatError, match="digest"):
        ka.verify(art, _binding())


def test_not_an_artifact(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"GGUF....")
    with pytest.raises(ka.FormatError, match="magic"):
        ka.read_header(p)


def test_cli_verify_exit_codes(tmp_path, capsys):
    art = tmp_path / "a.awkv"
    ka.pack(_payload(tmp_path), art, binding=_binding())
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_binding().canonical()), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_binding(cache_type_v="q4_0").canonical()), encoding="utf-8")
    assert ka.main(["verify", str(art), "--binding", str(good)]) == 0
    assert '"VALID"' in capsys.readouterr().out
    assert ka.main(["verify", str(art), "--binding", str(bad)]) == 1
    assert "cache_type_v" in capsys.readouterr().out
    junk = tmp_path / "junk"
    junk.write_bytes(b"nope")
    assert ka.main(["verify", str(junk)]) == 2
    assert ka.main(["inspect", str(art)]) == 0


# ── SlotStore export -> another node's import ───────────────────────────────


def test_slotstore_export_import_moves_a_snapshot_between_nodes(tmp_path):
    fp = scoped_fingerprint(model_fingerprint("llama-server-slot", "m", "b"), "tenant-a")
    key = bytes.fromhex("ab" * 20)
    node_a = SlotStore(tmp_path / "a")
    meta = node_a.put(fp, key, _payload(tmp_path), tokens=512, source="A")
    art = node_a.export_artifact(fp, key, _binding(), tmp_path / "ship.awkv")

    node_b = SlotStore(tmp_path / "b")
    got = node_b.import_artifact(art, fp, _binding())
    assert got.sha256 == meta.sha256 and got.tokens == 512
    assert got.extra["binding_digest"] == _binding().digest()
    dst = tmp_path / "slot-dir" / "kvp-x.bin"
    assert node_b.fetch(fp, key, dst) is not None
    assert not list((tmp_path / "b").glob(".import-*"))


def test_slotstore_import_refuses_other_config_and_other_scope(tmp_path):
    fp_a = scoped_fingerprint(model_fingerprint("llama-server-slot", "m", "b"), "tenant-a")
    fp_b = scoped_fingerprint(model_fingerprint("llama-server-slot", "m", "b"), "tenant-b")
    key = bytes.fromhex("cd" * 20)
    node_a = SlotStore(tmp_path / "a")
    node_a.put(fp_a, key, _payload(tmp_path), tokens=256)
    art = node_a.export_artifact(fp_a, key, _binding(), tmp_path / "ship.awkv")

    node_b = SlotStore(tmp_path / "b")
    with pytest.raises(ka.BindingMismatchError):
        node_b.import_artifact(art, fp_a, _binding(split="rpc:dgx,5090"))
    with pytest.raises(ka.ScopeMismatchError):
        node_b.import_artifact(art, fp_b, _binding())
    assert node_b.entries() == []


def test_slotstore_export_refuses_a_corrupt_snapshot(tmp_path):
    fp = scoped_fingerprint(model_fingerprint("x"), "s")
    key = bytes.fromhex("ef" * 20)
    store = SlotStore(tmp_path / "a")
    store.put(fp, key, _payload(tmp_path, 100), tokens=1)
    blob, _ = store.paths(fp, key)
    blob.write_bytes(b"corrupted")
    with pytest.raises(KeyError):
        store.export_artifact(fp, key, _binding(), tmp_path / "x.awkv")
