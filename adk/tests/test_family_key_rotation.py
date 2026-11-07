"""The family key's membership chain, judged ON the device, never on the household's word.

The household here is a DUMB, COMPLICIT store: it appends whatever record it is handed at
the next seq, lists any device as "approved", and can be made to rewrite history. Every
defence below is one a family computer applies itself:

* membership is exactly the newest verified record's ``members``; a record is valid only
  if it extends the head this computer verified and its signer was a member before it;
* record 0 carries a possession proof per member, opened here under the family key -- a
  computer that never held the key cannot start (or re-start) a chain;
* the family key is wrapped only to verified members; removing a member is a record that
  also rotates the key, so the removed device gets nothing new;
* a rewrite or fork of an already-verified record is refused, never resolved;
* re-encryption loops and then proves, read-only, that no chunk is on an old key.
"""
from __future__ import annotations

import base64
import json

import pytest

from adk import family_drive_cli as cli
from adk import family_drive_files as files
from adk import family_vault as fv
from adk.tests.test_family_drive_files import Household

T, W = "fam", "ws_fam"


def _ids(tmp_path, name):
    root = tmp_path / name
    return root, fv.device_keypair(root=root)[1].hex(), fv.signing_keypair(root=root)[1].hex()


def _genesis(root, key, members):
    proofs = {s: fv.possession_proof(key, T, W, s, x).hex() for s, x in members.items()}
    me = fv.signing_public_hex(root=root)
    rec = fv.chain_record(tenant=T, workspace=W, seq=0, prev_hash="", members=members,
                          signer=me, key_id_hex=fv.key_id(key).hex(), proofs=proofs)
    return rec, fv.sign_record(rec, root=root)


T0 = 1_700_000_000
DAY = fv.REMOVAL_DELAY


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """The devices' clock (record ts): starts at T0 + 10 days, advanced by tests."""
    now = {"t": T0 + 10 * DAY}
    monkeypatch.setattr(fv, "_now", lambda: now["t"])
    return now


def test_the_chain_rule(tmp_path, clock):
    a_root, ax, a_s = _ids(tmp_path, "a")
    b_root, bx, b_s = _ids(tmp_path, "b")
    _c_root, cx, c_s = _ids(tmp_path, "c")
    k1 = fv.new_family_key()
    clock["t"] = T0
    head = fv.verify_genesis(*_genesis(a_root, k1, {a_s: ax}), tenant=T, workspace=W, key=k1)
    clock["t"] = T0 + 10 * DAY

    def rec(root, signer, seq, prev, members, key_id_hex="", removed=(), pending=None,
            ts=T0, epoch=0):
        r = fv.chain_record(tenant=T, workspace=W, seq=seq, prev_hash=prev, members=members,
                            signer=signer, key_id_hex=key_id_hex, removed=removed,
                            pending_removals=removed if pending is None else pending,
                            ts=ts, epoch=epoch)
        return r, fv.sign_record(r, root=root)

    h1 = fv.verify_extends(head, *rec(a_root, a_s, 1, head["hash"], {a_s: ax, b_s: bx}))
    assert set(h1["members"]) == {a_s, b_s}
    with pytest.raises(fv.VaultError, match="not a member"):  # B was not a member at seq 0
        fv.verify_extends(head, *rec(b_root, b_s, 1, head["hash"], {a_s: ax, b_s: bx}))
    with pytest.raises(fv.VaultError, match="does not extend"):
        fv.verify_extends(h1, *rec(a_root, a_s, 1, head["hash"], {a_s: ax}))
    with pytest.raises(fv.VaultError, match="encryption key"):
        fv.verify_extends(h1, *rec(a_root, a_s, 2, h1["hash"], {a_s: ax, b_s: cx}))
    with pytest.raises(fv.VaultError, match="epoch"):
        fv.verify_extends(h1, *rec(a_root, a_s, 2, h1["hash"], {a_s: ax, b_s: bx}, epoch=1))
    with pytest.raises(fv.VaultError, match="time"):  # before the previous record
        fv.verify_extends(h1, *rec(a_root, a_s, 2, h1["hash"], {a_s: ax, b_s: bx},
                                   ts=T0 - 1))
    with pytest.raises(fv.VaultError, match="time"):  # in the future
        fv.verify_extends(h1, *rec(a_root, a_s, 2, h1["hash"], {a_s: ax, b_s: bx},
                                   ts=T0 + 20 * DAY))
    # a drop needs an EARLIER record announcing it -- never the dropping record alone
    with pytest.raises(fv.VaultError, match="announced by an earlier record"):
        fv.verify_extends(h1, *rec(a_root, a_s, 2, h1["hash"], {a_s: ax},
                                   key_id_hex="ef" * 8, removed=[b_s], ts=T0 + 2 * DAY))
    hp = fv.verify_extends(h1, *rec(a_root, a_s, 2, h1["hash"], {a_s: ax, b_s: bx},
                                    pending=[b_s], ts=T0 + 10))
    assert hp["pending_since"] == {b_s: T0 + 10}
    with pytest.raises(fv.VaultError, match="pending"):  # B can no longer sign
        fv.verify_extends(hp, *rec(b_root, b_s, 3, hp["hash"], {a_s: ax, b_s: bx, c_s: cx},
                                   pending=[], ts=T0 + 20))
    with pytest.raises(fv.VaultError, match="must not reach"):  # a new key, B still in
        fv.verify_extends(hp, *rec(a_root, a_s, 3, hp["hash"], {a_s: ax, b_s: bx},
                                   key_id_hex="99" * 8, pending=[b_s], ts=T0 + 20))
    with pytest.raises(fv.VaultError, match="delay"):  # too early
        fv.verify_extends(hp, *rec(a_root, a_s, 3, hp["hash"], {a_s: ax},
                                   key_id_hex="ab" * 8, removed=[b_s], ts=T0 + 10 + DAY - 1))
    with pytest.raises(fv.VaultError, match="not requested"):  # drop only announced ones
        fv.verify_extends(hp, *rec(a_root, a_s, 3, hp["hash"], {a_s: ax},
                                   key_id_hex="ab" * 8, removed=[b_s], pending=[],
                                   ts=T0 + 10 + DAY))
    with pytest.raises(fv.VaultError, match="reuses"):  # never the same key id twice
        fv.verify_extends(hp, *rec(a_root, a_s, 3, hp["hash"], {a_s: ax},
                                   key_id_hex=fv.key_id(k1).hex(), removed=[b_s],
                                   ts=T0 + 10 + DAY))
    h2 = fv.verify_extends(hp, *rec(a_root, a_s, 3, hp["hash"], {a_s: ax},
                                    key_id_hex="ab" * 8, removed=[b_s], ts=T0 + 10 + DAY))
    assert h2["members"] == {a_s: ax} and h2["key_id"] == "ab" * 8
    # removing the FIRST device is just another record (B announces, waits, then drops A)
    hq = fv.verify_extends(h1, *rec(b_root, b_s, 2, h1["hash"], {a_s: ax, b_s: bx},
                                    pending=[a_s], ts=T0 + 10))
    hr = fv.verify_extends(hq, *rec(b_root, b_s, 3, hq["hash"], {b_s: bx},
                                    key_id_hex="cd" * 8, removed=[a_s], ts=T0 + 10 + DAY))
    assert hr["members"] == {b_s: bx}
    # B adds C; C is a member because a record LISTS it -- A, gone, can never sign again
    h4 = fv.verify_extends(hr, *rec(b_root, b_s, 4, hr["hash"], {b_s: bx, c_s: cx},
                                    ts=T0 + 10 + DAY))
    with pytest.raises(fv.VaultError, match="not a member"):
        fv.verify_extends(h4, *rec(a_root, a_s, 5, h4["hash"], {a_s: ax, c_s: cx},
                                   ts=T0 + 10 + DAY))
    # a recovery genesis (epoch 1) must open under a family key version held here
    g1 = fv.chain_record(tenant=T, workspace=W, seq=0, prev_hash="", members={a_s: ax},
                         signer=a_s, key_id_hex=fv.key_id(k1).hex(), epoch=1,
                         proofs={a_s: fv.possession_proof(k1, T, W, a_s, ax).hex()})
    sig = fv.sign_record(g1, root=a_root)
    with pytest.raises(fv.VaultError, match="recovery"):
        fv.verify_genesis(g1, sig, tenant=T, workspace=W, keys={})
    assert fv.verify_genesis(g1, sig, tenant=T, workspace=W,
                             keys={fv.key_id(k1).hex(): k1})["epoch"] == 1


def test_a_genesis_by_a_computer_without_the_key_is_refused(tmp_path):
    k1 = fv.new_family_key()
    e_root, ex, e_s = _ids(tmp_path, "evil")
    fake = fv.chain_record(tenant=T, workspace=W, seq=0, prev_hash="", members={e_s: ex},
                           signer=e_s, key_id_hex=fv.key_id(k1).hex(),
                           proofs={e_s: fv.seal(fv.new_family_key(), b"x").hex()})
    with pytest.raises(fv.VaultError, match="possession"):
        fv.verify_genesis(fake, fv.sign_record(fake, root=e_root), tenant=T, workspace=W,
                          key=k1)
    with pytest.raises(fv.VaultError, match="another household"):
        fv.verify_genesis(*_genesis(e_root, k1, {e_s: ex}), tenant=T, workspace="other",
                          key=k1)
    # the proof binds BOTH device keys: it does not vouch for another encryption key
    proof = fv.possession_proof(k1, T, W, e_s, ex)
    assert fv.check_possession(k1, proof, T, W, e_s, ex)
    assert not fv.check_possession(k1, proof, T, W, e_s, "00" * 32)


class ChainHousehold(Household):
    """A household that appends any record at the next seq and trusts nothing."""

    def __init__(self):
        super().__init__()
        self.devices = {}      # x25519 hex -> {"label", "sign_pubkey", "possession_b64"}
        self.wraps = {}        # (x25519, key_id) -> b64
        self.records = []      # {"seq", "record_b64", "signature_b64"}
        self.pending = []      # sign keys whose removal was requested
        self.recovery_excluded = []  # the devices a guardian recovery was run against
        self.reencrypted = set()

    def head_body(self):
        return json.loads(base64.b64decode(self.records[-1]["record_b64"])) if self.records \
            else None

    def key_ids(self):
        return [json.loads(base64.b64decode(r["record_b64"]))["key_id"] for r in self.records
                if json.loads(base64.b64decode(r["record_b64"]))["key_id"]]

    def __call__(self, method, path, *, json_body=None, content=None, params=None):
        if path == "/family/storage/keys/devices" and method == "GET":
            hb = self.head_body()
            mem = {m["ed25519"] for m in hb["members"]} if hb else set()
            return 200, {"tenant": T, "workspace": W, "devices": [
                {"pubkey": p, **d, "bound": True, "approved": True or d["sign_pubkey"] in mem,
                 "has_key": [k for (pp, k) in self.wraps if pp == p]}
                for p, d in self.devices.items()]}
        if path == "/family/storage/keys/devices" and method == "POST":
            self.devices[json_body["pubkey"]] = {
                "label": json_body.get("label", ""), "sign_pubkey": json_body["sign_pubkey"],
                "possession_b64": json_body.get("possession_b64", "")}
            return 200, {"approved": False}
        if path.startswith("/family/storage/keys/devices/") and method == "DELETE":
            pub = path.rsplit("/", 1)[1]
            d = self.devices.pop(pub)
            self.wraps = {k: v for k, v in self.wraps.items() if k[0] != pub}
            self.pending.append(d["sign_pubkey"])
            return 200, {"removed": [pub]}
        if path == "/family/storage/keys/chain" and method == "GET":
            return 200, {"tenant": T, "workspace": W, "records": list(self.records),
                         "recovery_excluded": list(self.recovery_excluded)}
        if path == "/family/storage/keys/chain" and method == "POST":
            body = json.loads(base64.b64decode(json_body["record_b64"]))
            if body["seq"] != len(self.records):
                raise cli.ApiError(409, "chain_moved")
            self.records.append({"seq": body["seq"], **json_body})
            self.pending = [s for s in self.pending if s not in body["removed"]]
            return 200, {}
        if path == "/family/storage/keys/wraps" and method == "POST":
            self.wraps[(json_body["to_pubkey"], json_body["key_id"])] = json_body["blob_b64"]
            return 200, {}
        if path == "/family/storage/keys/wraps" and method == "GET":
            return 200, {"wraps": [{"key_id": k, "blob_b64": b}
                                   for (p, k), b in self.wraps.items()
                                   if p == params["pubkey"]]}
        if path == "/family/storage/keys/rotation" and method == "GET":
            hb = self.head_body()
            kids = self.key_ids()
            mem = {m["ed25519"] for m in hb["members"]} if hb else set()
            return 200, {"rotation_pending": bool(self.pending),
                         "pending": [{"sign_pubkey": s, "in_chain": s in mem}
                                     for s in self.pending],
                         "head": ({"seq": hb["seq"], "key_id": kids[-1],
                                   "reencrypted": kids[-1] in self.reencrypted}
                                  if hb else None),
                         "reencrypt_pending": bool(hb and kids[-1] not in self.reencrypted)}
        if path == "/family/storage/keys/rotation/reencrypted":
            self.reencrypted.add(json_body["key_id"])
            return 200, {}
        return super().__call__(method, path, json_body=json_body, content=content,
                                params=params)


@pytest.fixture
def house(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_FAMILY_DEVICE_TOKEN", "fdt_test")
    monkeypatch.setenv("AITHER_HOME", str(tmp_path / "home"))
    h = ChainHousehold()
    monkeypatch.setattr(cli, "request", h)
    monkeypatch.setattr(files, "_sleep", lambda s: None)
    assert cli.main(["key", "init", "--label", "desk"]) == 0  # genesis
    return h


def _home(monkeypatch, tmp_path, name):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path / name))


def _join(monkeypatch, tmp_path, name, by="home"):
    """A new computer registers; a member computer adds it after checking its fingerprint."""
    _home(monkeypatch, tmp_path, name)
    assert cli.main(["key", "register", "--label", name]) == 0
    x, s, fp = fv.public_key_hex(), fv.signing_public_hex(), cli._my_fingerprint()
    _home(monkeypatch, tmp_path, by)
    assert cli.main(["key", "approve", x, fp]) == 0
    _home(monkeypatch, tmp_path, name)
    assert cli.main(["key", "accept"]) == 0
    _home(monkeypatch, tmp_path, "home")
    return x, s


def test_remove_rotates_and_existing_files_still_open_under_the_new_key(house, tmp_path,
                                                                        monkeypatch, clock):
    monkeypatch.setattr(files, "CHUNK", 8)
    src = tmp_path / "taxes.pdf"
    src.write_bytes(b"family tax return 2026")
    assert cli.main(["put", str(src), "--copies"]) == 0
    old = cli.family_key()
    laptop, _ls = _join(monkeypatch, tmp_path, "laptop")
    stolen, stolen_s = _join(monkeypatch, tmp_path, "stolen")
    old_chunks = list(files.read_index(old)[1]["files"]["taxes.pdf"]["chunks"])

    assert cli.main(["key", "remove", "stolen"]) == 3  # announced; waits the delay
    assert cli.family_key() == old
    clock["t"] += DAY
    assert cli.main(["key", "rotate"]) == 0

    new = cli.family_key()
    nkid = fv.key_id(new).hex()
    assert new != old and nkid in house.reencrypted
    assert (stolen, nkid) not in house.wraps
    assert fv.unwrap_key(base64.b64decode(house.wraps[(laptop, nkid)]),
                         root=tmp_path / "laptop" / "family_storage") == new
    last = house.head_body()
    assert last["removed"] == [stolen_s] and last["key_id"] == nkid
    assert stolen_s not in {m["ed25519"] for m in last["members"]}
    assert not set(files.read_index(new)[1]["files"]["taxes.pdf"]["chunks"]) & set(old_chunks)
    assert files.count_old_objects(new) == 0
    out = tmp_path / "back.pdf"
    assert cli.main(["get", "taxes.pdf", "--out", str(out)]) == 0
    assert out.read_bytes() == b"family tax return 2026"
    _home(monkeypatch, tmp_path, "laptop")
    # the laptop first SEES the announcement only now: by its own clock the drop is too
    # early, so it stops there (head kept at the announcement) and follows a day later
    assert cli.main(["key", "accept"]) == 2 and fv.load_family_key("family") == old
    assert fv.load_head("family")["pending"] == [stolen_s]
    clock["t"] += DAY
    assert cli.main(["key", "accept"]) == 0 and fv.load_family_key("family") == new


def test_removing_the_first_device_then_rotating_works(house, tmp_path, monkeypatch,
                                                       clock):
    desk_x = fv.public_key_hex()
    laptop, _ls = _join(monkeypatch, tmp_path, "laptop")
    house("DELETE", f"/family/storage/keys/devices/{desk_x}")  # the genesis device
    _home(monkeypatch, tmp_path, "laptop")
    assert cli.main(["key", "rotate", "--no-reencrypt"]) == 3  # announce
    assert cli.main(["key", "rotate", "--no-reencrypt"]) == 3  # still waiting
    clock["t"] += DAY
    assert cli.main(["key", "rotate", "--no-reencrypt"]) == 0
    assert {m["ed25519"] for m in house.head_body()["members"]} == {fv.signing_public_hex()}


def test_a_device_only_listed_by_the_household_never_gets_the_key(house, tmp_path,
                                                                 monkeypatch):
    """A surviving guardian session enrolls a NEW device and registers it; the household
    lists it as approved and even appends a record adding it that the device signed
    itself. Members never wrap to it, and the forged record is refused."""
    _home(monkeypatch, tmp_path, "evil")
    assert cli.main(["key", "register", "--label", "evil"]) == 0
    ex, es = fv.public_key_hex(), fv.signing_public_hex()
    _home(monkeypatch, tmp_path, "home")
    assert cli.main(["key", "share"]) == 0
    assert cli.main(["key", "rotate", "--no-reencrypt"]) == 0
    assert not any(p == ex for p, _k in house.wraps)
    head = fv.load_head("family")
    forged = fv.chain_record(tenant=T, workspace=W, seq=head["seq"] + 1, prev_hash=head["hash"],
                             members={**head["members"], es: ex}, signer=es)
    house("POST", "/family/storage/keys/chain", json_body={
        "record_b64": base64.b64encode(forged).decode(),
        "signature_b64": base64.b64encode(fv.sign_record(
            forged, root=tmp_path / "evil" / "family_storage")).decode()})
    assert cli.main(["key", "share"]) == 2  # the chain no longer verifies: stop, say so
    assert not any(p == ex for p, _k in house.wraps)


def test_a_device_whose_removal_is_pending_cannot_extend_the_chain(house, tmp_path,
                                                                     monkeypatch):
    """The household says the stolen device's removal is requested (signed into the next
    record); a complicit household then appends a record the stolen device signed."""
    stolen, stolen_s = _join(monkeypatch, tmp_path, "stolen")
    house.pending.append(stolen_s)
    h0 = cli._sync_chain()[0]
    note = fv.chain_record(tenant=T, workspace=W, seq=h0["seq"] + 1, prev_hash=h0["hash"],
                           members=h0["members"], signer=fv.signing_public_hex(),
                           pending_removals=cli._pending(h0))  # the desk signs it in
    cli._append(note)
    head = cli._sync_chain()[0]
    assert head["pending"] == [stolen_s]
    evil = fv.chain_record(tenant=T, workspace=W, seq=head["seq"] + 1, prev_hash=head["hash"],
                           members={**head["members"], "ee" * 32: "ff" * 32}, signer=stolen_s)
    house("POST", "/family/storage/keys/chain", json_body={
        "record_b64": base64.b64encode(evil).decode(),
        "signature_b64": base64.b64encode(fv.sign_record(
            evil, root=tmp_path / "stolen" / "family_storage")).decode()})
    assert cli.main(["key", "share"]) == 2 and fv.load_head("family") == head


def test_key_remove_lists_and_can_drop_the_members_it_added(house, tmp_path, monkeypatch,
                                                            capsys, clock):
    laptop, lap_s = _join(monkeypatch, tmp_path, "laptop")
    kid, kid_s = _join(monkeypatch, tmp_path, "kid", by="laptop")
    pet, pet_s = _join(monkeypatch, tmp_path, "pet", by="kid")  # added through kid
    capsys.readouterr()
    assert cli.main(["key", "remove", "laptop", "--no-reencrypt"]) == 3
    out = capsys.readouterr().out
    assert "these members: kid, pet" in out  # transitively
    clock["t"] += DAY
    assert cli.main(["key", "rotate", "--no-reencrypt"]) == 0
    assert {kid_s, pet_s} <= set(fv.load_head("family")["members"])  # they stay
    tablet, _ts = _join(monkeypatch, tmp_path, "tablet")
    toy, toy_s = _join(monkeypatch, tmp_path, "toy", by="tablet")
    ball, ball_s = _join(monkeypatch, tmp_path, "ball", by="toy")
    assert cli.main(["key", "remove", "tablet", "--with-added", "--no-reencrypt"]) == 3
    clock["t"] += DAY
    assert cli.main(["key", "rotate", "--no-reencrypt"]) == 0
    assert not {toy_s, ball_s} & set(fv.load_head("family")["members"])


def test_a_rewritten_record_is_a_fork_and_is_refused(house, tmp_path, monkeypatch):
    laptop, _ls = _join(monkeypatch, tmp_path, "laptop")
    _home(monkeypatch, tmp_path, "laptop")
    seen = fv.load_head("family")
    # the household rewrites seq 1 (a record the laptop verified) with another one
    desk_root = tmp_path / "home" / "family_storage"
    g = fv.parse_record(base64.b64decode(house.records[0]["record_b64"]),
                        base64.b64decode(house.records[0]["signature_b64"]))
    other = fv.chain_record(tenant=T, workspace=W, seq=1, prev_hash=fv.record_hash(
        base64.b64decode(house.records[0]["record_b64"])), members=g["members"],
        signer=g["signer"], key_id_hex="")
    house.records[1] = {"seq": 1, "record_b64": base64.b64encode(other).decode(),
                        "signature_b64": base64.b64encode(
                            fv.sign_record(other, root=desk_root)).decode()}
    assert cli.main(["key", "accept"]) == 2
    assert fv.load_head("family") == seen


def test_a_legacy_household_is_bootstrapped_only_by_a_key_holder(tmp_path, monkeypatch):
    """A household from before the chain: two computers hold the key, a third never did.
    The non-holder's genesis is refused by holders; the holder's lists every bound
    computer whose possession proof opens -- and not the non-holder."""
    monkeypatch.setenv("AITHER_FAMILY_DEVICE_TOKEN", "fdt_test")
    h = ChainHousehold()
    monkeypatch.setattr(cli, "request", h)
    k1 = fv.new_family_key()
    signers = {}
    for name, holds in (("desk", True), ("laptop", True), ("evil", False)):
        _home(monkeypatch, tmp_path, name)
        if holds:
            fv.save_family_key("family", k1)
        assert cli.main(["key", "register", "--label", name]) == 0
        signers[name] = fv.signing_public_hex()
    _home(monkeypatch, tmp_path, "evil")
    assert cli.main(["key", "init"]) == 0  # makes its OWN key; writes a genesis on it
    evil_genesis = h.records.pop()  # (a real household refuses it: not the wrapped key)
    _home(monkeypatch, tmp_path, "desk")
    h.records.append(evil_genesis)
    assert cli.main(["key", "accept"]) == 2  # a holder will not join a non-holder's chain
    h.records.clear()
    assert cli.main(["key", "init"]) == 0
    mem = {m["ed25519"] for m in h.head_body()["members"]}
    assert mem == {signers["desk"], signers["laptop"]}


def test_no_reencrypt_keeps_the_marker_and_old_files_still_read(house, tmp_path):
    src = tmp_path / "a.txt"
    src.write_bytes(b"before the rotation")
    cli.main(["put", str(src)])
    assert cli.main(["key", "rotate", "--no-reencrypt"]) == 0
    assert not house.reencrypted
    out = tmp_path / "a.out"
    assert cli.main(["get", "a.txt", "--out", str(out)]) == 0
    assert out.read_bytes() == b"before the rotation"
    assert cli.main(["key", "reencrypt"]) == 0 and house.reencrypted


def test_reencrypt_loops_and_proves_no_chunk_is_on_an_old_key(house, tmp_path, monkeypatch):
    src = tmp_path / "a.txt"
    src.write_bytes(b"first")
    cli.main(["put", str(src)])
    old = cli.family_key()
    assert cli.main(["key", "rotate", "--no-reencrypt"]) == 0
    new = cli.family_key()
    real_put = files._put_like
    fired = []

    def racing_put(sealed, index, name, entry):
        if not fired:
            fired.append(1)
            oid = files.put_object(fv.seal(old, b"late file on the old key"), replicas=2)
            files.update_index(old, lambda ix: ix["files"].update(
                {"late.txt": {"size": 24, "chunks": [oid], "replicas": 2,
                              "redundancy": "copies"}}))
        return real_put(sealed, index, name, entry)

    monkeypatch.setattr(files, "_put_like", racing_put)
    assert cli.main(["key", "reencrypt"]) == 0
    assert files.count_old_objects(new) == 0
    house.reencrypted.clear()
    monkeypatch.setattr(files, "count_old_objects", lambda key, wait_s=600: 1)
    assert cli.main(["key", "reencrypt"]) == 2 and not house.reencrypted


def test_share_with_a_removal_pending_rotates_instead(house, tmp_path, monkeypatch, clock):
    old = cli.family_key()
    stolen, _ss = _join(monkeypatch, tmp_path, "stolen")
    house("DELETE", f"/family/storage/keys/devices/{stolen}")
    assert cli.main(["key", "share"]) == 3 and cli.family_key() == old  # announced
    clock["t"] += DAY
    assert cli.main(["key", "share"]) == 0
    assert cli.family_key() != old and not house.pending


def test_a_new_epoch_is_surfaced_and_followed_only_on_request(house, tmp_path, monkeypatch):
    """A guardian recovery restarts the chain; a device refuses to follow it silently."""
    laptop, _ls = _join(monkeypatch, tmp_path, "laptop")
    head = fv.load_head("family")
    key = cli.family_key()
    me_s, me_x = fv.signing_public_hex(), fv.public_key_hex()
    g1 = fv.chain_record(tenant=T, workspace=W, seq=0, prev_hash="", members={me_s: me_x},
                         signer=me_s, key_id_hex=fv.key_id(key).hex(), epoch=1,
                         proofs={me_s: fv.possession_proof(key, T, W, me_s, me_x).hex()})
    house.records = [{"seq": 0, "record_b64": base64.b64encode(g1).decode(),
                      "signature_b64": base64.b64encode(fv.sign_record(g1)).decode()}]
    _home(monkeypatch, tmp_path, "laptop")
    capsys_err = cli.main(["key", "accept"])
    assert capsys_err == 2 and fv.load_head("family")["epoch"] == head["epoch"] == 0
    assert cli.main(["key", "accept", "--accept-new-epoch"]) == 2  # not a member of it
    assert fv.load_head("family")["epoch"] == 1


def test_a_new_epoch_signed_by_a_named_device_is_never_followed(house, tmp_path,
                                                                monkeypatch):
    """The guardians ran the recovery against the desk; a genesis the desk signed is
    refused even with --accept-new-epoch."""
    laptop, _ls = _join(monkeypatch, tmp_path, "laptop")
    key = cli.family_key()
    me_s, me_x = fv.signing_public_hex(), fv.public_key_hex()
    g1 = fv.chain_record(tenant=T, workspace=W, seq=0, prev_hash="", members={me_s: me_x},
                         signer=me_s, key_id_hex=fv.key_id(key).hex(), epoch=1,
                         proofs={me_s: fv.possession_proof(key, T, W, me_s, me_x).hex()})
    house.records = [{"seq": 0, "record_b64": base64.b64encode(g1).decode(),
                      "signature_b64": base64.b64encode(fv.sign_record(g1)).decode()}]
    house.recovery_excluded = [me_s]
    _home(monkeypatch, tmp_path, "laptop")
    assert cli.main(["key", "accept", "--accept-new-epoch"]) == 2
    assert fv.load_head("family")["epoch"] == 0  # stayed on the chain it verified


def test_accept_skips_a_bad_wrap_and_drops_retired_keys_once_resealed(house, tmp_path,
                                                                     monkeypatch):
    laptop, _ls = _join(monkeypatch, tmp_path, "laptop")
    assert cli.main(["key", "rotate"]) == 0
    _home(monkeypatch, tmp_path, "laptop")
    house.wraps[(laptop, "00" * 8)] = base64.b64encode(b"AFW1" + b"\0" * 92).decode()
    assert cli.main(["key", "accept"]) == 0
    assert fv.retired_keys("family") == {}


def test_a_backdated_announcement_buys_nothing_on_a_watching_device(tmp_path, clock):
    """HIGH reproduced (client): the announcing record claims an old ts, then the drop
    follows at once. A device that watched the chain starts the delay when IT first saw
    the announcement, and judges the drop by its own clock."""
    a_root, ax, a_s = _ids(tmp_path, "a")
    _b_root, bx, b_s = _ids(tmp_path, "b")
    k1 = fv.new_family_key()
    clock["t"] = T0
    g = fv.verify_genesis(*_genesis(a_root, k1, {a_s: ax, b_s: bx}), tenant=T, workspace=W,
                          key=k1)
    clock["t"] = T0 + 5 * DAY  # the device sees the announcement now ...

    def rec(seq, prev, members, ts, **kw):
        r = fv.chain_record(tenant=T, workspace=W, seq=seq, prev_hash=prev, members=members,
                            signer=a_s, ts=ts, **kw)
        return r, fv.sign_record(r, root=a_root)

    hp = fv.verify_extends(g, *rec(1, g["hash"], {a_s: ax, b_s: bx}, T0,  # ... dated T0
                                   pending_removals=[b_s]), seen_at=int(clock["t"]))
    assert hp["pending_since"] == {b_s: T0 + 5 * DAY}
    drop = rec(2, hp["hash"], {a_s: ax}, T0 + 5 * DAY, key_id_hex="ab" * 8, removed=[b_s],
               pending_removals=[b_s])
    with pytest.raises(fv.VaultError, match="delay"):
        fv.verify_extends(hp, *drop, seen_at=int(clock["t"]))
    clock["t"] += DAY
    assert fv.verify_extends(hp, *drop, seen_at=int(clock["t"]))["members"] == {a_s: ax}


def test_a_recovery_this_device_cannot_verify_says_what_it_needs(tmp_path):
    a_root, ax, a_s = _ids(tmp_path, "a")
    k1 = fv.new_family_key()
    g1 = fv.chain_record(tenant=T, workspace=W, seq=0, prev_hash="", members={a_s: ax},
                         signer=a_s, key_id_hex=fv.key_id(k1).hex(), epoch=1,
                         proofs={a_s: fv.possession_proof(k1, T, W, a_s, ax).hex()})
    with pytest.raises(fv.VaultError, match="recovery pending: it needs a device holding "
                                            f"family key version {fv.key_id(k1).hex()}"):
        fv.verify_genesis(g1, fv.sign_record(g1, root=a_root), tenant=T, workspace=W, keys={})


def test_a_device_refuses_a_recovery_genesis_from_a_named_device(tmp_path):
    a_root, ax, a_s = _ids(tmp_path, "a")
    b_root, bx, b_s = _ids(tmp_path, "b")
    k1 = fv.new_family_key()

    def g1(root, signer, members):
        rec = fv.chain_record(tenant=T, workspace=W, seq=0, prev_hash="", members=members,
                              signer=signer, key_id_hex=fv.key_id(k1).hex(), epoch=1,
                              proofs={s: fv.possession_proof(k1, T, W, s, x).hex()
                                      for s, x in members.items()})
        return rec, fv.sign_record(rec, root=root)

    with pytest.raises(fv.VaultError, match="signed by a device the recovery was run"):
        fv.verify_genesis(*g1(b_root, b_s, {b_s: bx}), tenant=T, workspace=W, key=k1,
                          excluded=[b_s])
    with pytest.raises(fv.VaultError, match="keeps a device the recovery was run against"):
        fv.verify_genesis(*g1(a_root, a_s, {a_s: ax, b_s: bx}), tenant=T, workspace=W,
                          key=k1, excluded=[b_s])
    head = fv.verify_genesis(*g1(a_root, a_s, {a_s: ax}), tenant=T, workspace=W, key=k1,
                             excluded=[b_s])
    assert head["epoch"] == 1 and head["members"] == {a_s: ax}
