"""adk.lan_pair: the LAN pairing advert contract, the flood-proof candidate book and the
time-boxed pairing mode. Every guard has a test that fails when the guard is removed."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

from adk import lan_pair as lp

RID = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
RID2 = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


def txt(**over: Any) -> Dict[str, Any]:
    t: Dict[str, Any] = {"v": "1", "rid": RID, "class": "watch"}
    t.update(over)
    return {k: v for k, v in t.items() if v is not None}


# -- build_txt -------------------------------------------------------------------------


def test_build_txt_is_exactly_three_keys():
    assert lp.build_txt(RID, "Watch") == {"v": "1", "rid": RID, "class": "watch"}


@pytest.mark.parametrize("rid", ["", "short", "a" * 33, "has space in it!!", "a/b" * 8,
                                 RID.upper(), "Zx9_kq-3Lm0pQrStUvWxZx9_kq-3Lm0p"])
def test_build_txt_refuses_a_bad_rid(rid):
    with pytest.raises(ValueError):
        lp.build_txt(rid, "phone")


@pytest.mark.parametrize("cls", ["toaster", "spark", "sovereign", "tablet"])
def test_build_txt_refuses_a_class_identity_will_not_join(cls):
    with pytest.raises(ValueError):
        lp.build_txt(RID, cls)


# -- parse_txt (untrusted) -------------------------------------------------------------


def test_parse_txt_accepts_bytes_and_case():
    got = lp.parse_txt({b"V": b"1", b"RID": RID.encode(), b"Class": b"PHONE"})
    assert got == {"v": "1", "rid": RID, "class": "phone"}


def test_parse_txt_ignores_unknown_keys_but_never_surfaces_them():
    got = lp.parse_txt(txt(owner="mallory", url="http://evil"))
    assert got == {"v": "1", "rid": RID, "class": "watch"}


@pytest.mark.parametrize("key", sorted(lp.FORBIDDEN_KEYS))
def test_parse_txt_drops_an_advert_carrying_a_secret(key):
    assert lp.parse_txt(txt(**{key: "123456"})) is None


def test_parse_txt_drops_secret_key_in_any_case():
    assert lp.parse_txt({**txt(), "SAS": "123456"}) is None


@pytest.mark.parametrize("bad", [
    txt(v="2"), txt(v=None), txt(rid="short"), txt(rid=None), txt(rid="x" * 44),
    txt(**{"class": "toaster"}), txt(**{"class": None}),
])
def test_parse_txt_drops_off_contract(bad):
    assert lp.parse_txt(bad) is None


def test_parse_txt_drops_oversize():
    assert lp.parse_txt(txt(junk="x" * 65)) is None
    many = txt(**{f"k{i}": "x" for i in range(6)})
    assert lp.parse_txt(many) is None  # 9 keys
    fat = txt(**{c * 30: "x" * 64 for c in "abcde"})
    assert lp.parse_txt(fat) is None  # > 400 bytes


def test_parse_txt_drops_duplicate_after_case_fold():
    assert lp.parse_txt({"rid": RID, "RID": RID2, "v": "1", "class": "phone"}) is None


def test_parse_txt_rejects_non_mapping():
    assert lp.parse_txt(["v=1"]) is None  # type: ignore[arg-type]


def test_clean_label_strips_controls_and_bidi():
    assert lp.clean_label("Kid‮enohp\n\x07's   Watch") == "Kidenohp's Watch"
    assert len(lp.clean_label("x" * 100)) == lp.MAX_LABEL
    assert lp.clean_label(b"Den TV") == "Den TV"


# -- CandidateBook ---------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def rid(i: int) -> str:
    return f"{i:032x}"


def test_book_add_refresh_list():
    c = Clock()
    b = lp.CandidateBook(clock=c)
    assert b.offer(txt(), label="Pixel\x00 Watch", source="10.0.0.5") == "added"
    c.t += 10
    assert b.offer(txt(), label="renamed", source="10.0.0.5") == "refreshed"
    assert b.list() == [{"rid": RID, "class": "watch", "label": "Pixel Watch",
                         "verified": False}]


def test_book_rejects_bad_advert():
    b = lp.CandidateBook(clock=Clock())
    assert b.offer(txt(sas="123456"), source="a") == "rejected"
    assert b.list() == []


def test_book_expires_and_refresh_does_not_extend():
    c = Clock()
    b = lp.CandidateBook(clock=c, ttl_s=300)
    b.offer(txt(), source="a")
    for _ in range(10):
        c.t += 31
        b.offer(txt(), source="a")
    assert b.list() == []  # 310 s after first sight, however often it re-announced


def test_book_conflict_drops_and_bans_rid():
    c = Clock()
    b = lp.CandidateBook(clock=c)
    assert b.offer(txt(), source="10.0.0.5") == "added"
    assert b.offer(txt(), source="10.0.0.66") == "conflict"
    assert b.list() == []
    assert b.offer(txt(), source="10.0.0.5") == "banned"
    c.t += 301
    assert b.offer(txt(), source="10.0.0.5") == "added"


def test_book_class_flip_is_a_conflict():
    b = lp.CandidateBook(clock=Clock())
    b.offer(txt(), source="a")
    assert b.offer(txt(**{"class": "laptop"}), source="a") == "conflict"


def test_book_per_source_cap():
    b = lp.CandidateBook(clock=Clock(), max_per_source=2)
    assert [b.offer(txt(rid=rid(i)), source="evil") for i in range(3)] == \
        ["added", "added", "source-cap"]
    assert b.offer(txt(rid=rid(9)), source="other") == "added"


def test_book_total_cap():
    b = lp.CandidateBook(clock=Clock(), max_candidates=3, max_per_source=99)
    outs = [b.offer(txt(rid=rid(i)), source=f"s{i}") for i in range(4)]
    assert outs == ["added", "added", "added", "full"]
    assert len(b.list()) == 3


def test_book_rate_limit_then_refill():
    c = Clock()
    b = lp.CandidateBook(clock=c, burst=5, refill_per_s=1, max_per_source=99,
                         max_candidates=99)
    outs = [b.offer(txt(rid=rid(i)), source=f"s{i}") for i in range(8)]
    assert outs.count("flood") == 3
    assert b.dropped == 3
    c.t += 2
    assert b.offer(txt(rid=rid(50)), source="s50") == "added"


# -- the Phase-1 seam (identity_device_join on feat/device-join-same-account-qr) ---------

#: Pinned against Phase 1's compute_sas (identity_device_join.py) and WearJoin.sas.
SAS_VECTOR = ("0f1e2d3c4b5a69788796a5b4c3d2e1f0", "ab" * 32, "11" * 16, "435822")


def test_compute_sas_matches_identity_vector():
    rid_, pk, nonce, want = SAS_VECTOR
    assert lp.compute_sas(rid_, pk, nonce) == want


class Key:
    """A real Ed25519 key, as awseal holds one."""

    def __init__(self) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        self.private = Ed25519PrivateKey.generate()
        self.pubkey = self.private.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw).hex()

    def sign(self, message: bytes) -> bytes:
        return self.private.sign(message)


KEY = Key()
NONCE = "22" * 16


def good_req(**over: Any) -> Dict[str, Any]:
    d: Dict[str, Any] = {"rid": RID, "nonce": NONCE,
                         "sas": lp.compute_sas(RID, KEY.pubkey, NONCE),
                         "claim_secret": "c" * 43, "join_code": "ABCD2345",
                         "expires_in": 600, "state": "pending"}
    d.update(over)
    return d


def test_validate_join_request_clamps_window_and_hides_secrets():
    req = lp.validate_join_request(good_req(), KEY.pubkey)
    assert req.expires_in == lp.MAX_WINDOW_S
    assert req.join_code == "ABCD2345"
    shown = repr(req)
    assert req.sas not in shown and "c" * 43 not in shown and "ABCD2345" not in shown


def test_validate_join_request_refuses_a_number_for_another_key():
    other = Key()
    with pytest.raises(ValueError, match="does not match"):
        lp.validate_join_request(good_req(), other.pubkey)


@pytest.mark.parametrize("over", [
    {"rid": "bad rid"}, {"rid": RID.upper()}, {"nonce": "zz"}, {"sas": "12345"},
    {"sas": "abcdef"}, {"claim_secret": "short"}, {"claim_secret": "c" * 129},
    {"join_code": "ABC"}, {"join_code": None}, {"expires_in": 0},
    {"expires_in": "soon"},
])
def test_validate_join_request_refuses(over):
    with pytest.raises(ValueError):
        lp.validate_join_request(good_req(**over), KEY.pubkey)


class Resp:
    def __init__(self, status: int, body: Any = None) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.mark.parametrize("status,body,want", [
    (200, {"state": "pending", "expires_in": 200}, {"state": "pending"}),
    (200, {"state": "approved", "code": "ab3d-ef7h"}, {"state": "approved",
                                                      "code": "AB3D-EF7H"}),
    (200, {"state": "approved"}, {"state": "approved"}),
    (200, {"state": "approved", "code": "<script>"}, {"state": "approved"}),
    (200, {"state": "claimed"}, {"state": "expired"}),
    (200, {"state": "denied"}, {"state": "denied"}),
    (200, {"state": "weird"}, {"state": "pending"}),
    (200, ValueError("no json"), {"state": "pending"}),
    (200, ["state"], {"state": "pending"}),
    (403, None, {"state": "expired"}),
    (404, None, {"state": "expired"}),
    (429, None, {"state": "pending"}),
    (502, None, {"state": "pending"}),
])
def test_claim_state_fails_closed(status, body, want):
    assert lp.claim_state(status, Resp(status, body)) == want


def test_identity_seam_open_then_signed_claim(monkeypatch):
    """The HTTP seam against Phase 1's shapes: the open body carries only class, key and
    label; the claim carries the secret and an Ed25519 signature Identity can verify."""
    import httpx
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    calls: List[Dict[str, Any]] = []

    def post(url, json=None, timeout=None, **kw):
        calls.append({"url": url, "json": json})
        if url.endswith("/v1/nodes/join/open"):
            return Resp(200, good_req())
        return Resp(200, {"state": "approved", "code": "WXYZ2345"})

    monkeypatch.setattr(httpx, "post", post)
    seam = lp.IdentityJoinRequests("https://identity.example/", KEY)
    req = seam.create("laptop", "Den\nPC")
    assert calls[0]["url"] == "https://identity.example/v1/nodes/join/open"
    assert calls[0]["json"] == {"device_class": "laptop", "pubkey": KEY.pubkey,
                                "label": "DenPC"}
    assert seam.poll(req) == {"state": "approved", "code": "WXYZ2345"}
    claim = calls[1]
    assert claim["url"] == f"https://identity.example/v1/nodes/join/requests/{RID}/claim"
    assert set(claim["json"]) == {"claim_secret", "signature"}
    sig = bytes.fromhex(claim["json"]["signature"])
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(KEY.pubkey)).verify(
        sig, f"aither-join-v1|{RID}|{NONCE}".encode())


@pytest.mark.parametrize("status,exc", [(404, lp.SeamUnavailableError),
                                        (405, lp.SeamUnavailableError),
                                        (429, RuntimeError), (500, RuntimeError)])
def test_identity_seam_create_refusals(monkeypatch, status, exc):
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: Resp(status, {}))
    with pytest.raises(exc):
        lp.IdentityJoinRequests("https://i", KEY).create("laptop", "x")


def test_identity_seam_refuses_swapped_key(monkeypatch):
    import httpx

    swapped = good_req(sas=lp.compute_sas(RID, Key().pubkey, NONCE))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: Resp(200, swapped))
    with pytest.raises(ValueError):
        lp.IdentityJoinRequests("https://i", KEY).create("laptop", "x")


def test_load_join_key_is_the_pairing_seal_key(monkeypatch, tmp_path):
    pytest.importorskip("awseal")
    monkeypatch.setenv("AWSEAL_KEY_PATH", str(tmp_path / "signing.key"))
    from adk.device_identity import seal_public_key

    key = lp.load_join_key()
    assert key.pubkey == seal_public_key(create=False)
    assert len(key.sign(b"x")) == 64
    assert repr(key) == f"SealKey(pubkey={key.pubkey!r})"


def test_load_join_key_refuses_a_key_pairing_would_not_present(monkeypatch, tmp_path):
    pytest.importorskip("awseal")
    monkeypatch.setenv("AWSEAL_KEY_PATH", str(tmp_path / "signing.key"))
    import adk.device_identity as di

    assert lp.load_join_key().pubkey  # the key exists and is readable
    monkeypatch.setattr(di, "seal_public_key", lambda create=True: "ab" * 32)
    with pytest.raises(lp.KeyUnavailableError, match="not the one pairing presents"):
        lp.load_join_key()


def test_load_join_key_without_awseal(monkeypatch):
    import builtins

    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "awseal" or name.startswith("awseal."):
            raise ImportError("no awseal")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(lp.KeyUnavailableError, match="awseal"):
        lp.load_join_key()


# -- render_service / advertisers ------------------------------------------------------


_PAIR_TEMPLATE = (Path(__file__).resolve().parents[2] / "AitherOS" / "apps" / "AitherDesktop"
                  / "atomic" / "config" / "avahi" / "aitheros-pair.service.in")
_CONTAINERFILE = (Path(__file__).resolve().parents[2] / "AitherOS" / "apps" / "AitherDesktop"
                  / "atomic" / "layers" / "Containerfile.aitheros")


@pytest.mark.skipif(not _PAIR_TEMPLATE.is_file(), reason="not in the monorepo")
def test_render_service_matches_shipped_template():
    tpl = _PAIR_TEMPLATE
    shipped = tpl.read_text(encoding="utf-8")
    assert shipped.replace("\r\n", "\n") == lp.SERVICE_TEMPLATE
    body = lp.render_service(lp.build_txt(RID, "laptop"), shipped)
    assert "<type>_aither-pair._tcp</type>" in body
    assert f"<txt-record>rid={RID}</txt-record>" in body
    assert "@" not in body.split("-->", 1)[1]


@pytest.mark.skipif(not _CONTAINERFILE.is_file(), reason="not in the monorepo")
def test_image_never_installs_the_pair_advert_always_on():
    cf = _CONTAINERFILE
    text = cf.read_text(encoding="utf-8")
    # every COPY instruction (continuation lines joined) that names the pair template
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    copies = [ln for ln in joined.splitlines()
              if ln.startswith("COPY") and "aitheros-pair" in ln]
    assert copies, "the pairing template is not shipped"
    for ln in copies:
        assert "/etc/avahi/services" not in ln


@pytest.mark.parametrize("key", ["SAS", "join_code", "Nonce"])
def test_advert_guard_refuses_secret_keys(key):
    with pytest.raises(ValueError):
        lp._check_no_secrets({"v": "1", key: "x"})


def test_render_service_refuses_secret():
    with pytest.raises(ValueError):
        lp.render_service({**lp.build_txt(RID, "laptop"), "sas": "123456"})


def test_avahi_publish_argv_has_no_secret_and_is_validated():
    a = lp.AvahiPublish(lp.build_txt(RID, "laptop"), name="Den\nPC")
    argv = a.argv()
    assert argv[:4] == ["avahi-publish-service", "DenPC", "_aither-pair._tcp", "9"]
    assert sorted(argv[4:]) == sorted(["v=1", f"rid={RID}", "class=laptop"])
    with pytest.raises(ValueError):
        lp.AvahiPublish({**lp.build_txt(RID, "laptop"), "code": "ABC123"})


def test_service_file_written_and_removed(tmp_path):
    a = lp.AvahiServiceFile(lp.build_txt(RID, "desktop"), tmp_path)
    a.start()
    assert (tmp_path / "aitheros-pair.service").read_text().count(RID) == 1
    a.stop()
    a.stop()
    assert list(tmp_path.iterdir()) == []


# -- run_pair_mode -----------------------------------------------------------------------


class FakeAdv:
    def __init__(self, log: List[str]) -> None:
        self.log = log

    def start(self) -> None:
        self.log.append("start")

    def stop(self) -> None:
        self.log.append("stop")


class FakeSeam:
    def __init__(self, states: List[Dict[str, Any]], **req: Any) -> None:
        self.states = states
        self.req = good_req(**req)
        self.polls = 0

    def create(self, device_class: str, label: str) -> lp.JoinRequest:
        return lp.validate_join_request(self.req, KEY.pubkey)

    def poll(self, req: lp.JoinRequest) -> Dict[str, Any]:
        self.polls += 1
        return self.states.pop(0) if self.states else {"state": "pending"}


def run(seam: Any, window: int = 300, complete: Any = None) -> Dict[str, Any]:
    clock = Clock()
    log: List[str] = []
    said: List[str] = []
    adverts: List[Dict[str, str]] = []
    done: List[str] = []

    def factory(t, name):
        adverts.append(dict(t))
        return FakeAdv(log)

    def sleep(s):
        clock.t += s

    def comp(code):
        done.append(code)
        log.append("complete")
        return complete(code) if complete else {"paired": True, "node_id": "n1"}

    rc = lp.run_pair_mode(seam, device_class="watch", label="w", window_s=window,
                          advertiser_factory=factory, complete=comp, say=said.append,
                          clock=clock, sleep=sleep)
    return {"rc": rc, "log": log, "said": said, "adverts": adverts, "done": done,
            "elapsed": clock.t - 1000.0}


def test_pair_mode_happy_path_stops_advert_before_join():
    r = run(FakeSeam([{"state": "pending"}, {"state": "approved", "code": "SECRETCD"}]))
    assert r["rc"] == 0
    assert r["log"][:3] == ["start", "stop", "complete"]
    assert r["adverts"] == [{"v": "1", "rid": RID, "class": "watch"}]
    assert r["done"] == ["SECRETCD"]
    assert not any("SECRETCD" in s for s in r["said"])
    assert not any("c" * 43 in s for s in r["said"])  # the claim secret, never shown
    sas = lp.compute_sas(RID, KEY.pubkey, NONCE)
    # the number and the join code: on this screen only, never in the advert
    assert any(f"{sas[:3]} {sas[3:]}" in s and "ABCD-2345" in s for s in r["said"])
    assert sas not in str(r["adverts"]) and "ABCD2345" not in str(r["adverts"])


def test_pair_mode_is_time_boxed_even_if_asked_longer():
    r = run(FakeSeam([]), window=3600)
    assert r["rc"] == 1
    assert r["elapsed"] <= lp.MAX_WINDOW_S + 2.0
    assert r["log"] == ["start", "stop"]


def test_pair_mode_time_box_holds_even_if_the_seam_says_longer():
    class LongSeam(FakeSeam):
        def create(self, device_class, label):
            req = lp.validate_join_request(self.req, KEY.pubkey)
            req.expires_in = 3600  # a seam that skipped the clamp
            return req

    r = run(LongSeam([]), window=3600)
    assert r["elapsed"] <= lp.MAX_WINDOW_S + 2.0


def test_pair_mode_respects_shorter_server_expiry():
    r = run(FakeSeam([], expires_in=30))
    assert r["elapsed"] <= 32


@pytest.mark.parametrize("state", ["denied", "expired"])
def test_pair_mode_stops_on_refusal(state):
    r = run(FakeSeam([{"state": state}]))
    assert r["rc"] == 1 and r["log"] == ["start", "stop"] and r["done"] == []


def test_pair_mode_approved_without_code_keeps_waiting():
    r = run(FakeSeam([{"state": "approved"}]), window=10)
    assert r["done"] == [] and r["rc"] == 1


def test_pair_mode_never_advertises_without_a_server_rid():
    class NoSeam:
        def create(self, *a):
            raise lp.SeamUnavailableError("no join API")

        def poll(self, req):  # pragma: no cover
            raise AssertionError

    r = run(NoSeam())
    assert r["rc"] == 3 and r["adverts"] == [] and r["log"] == []


def test_pair_mode_without_a_device_key_never_advertises():
    class NoKey:
        def create(self, *a):
            raise lp.KeyUnavailableError("awseal is not installed")

        def poll(self, req):  # pragma: no cover
            raise AssertionError

    r = run(NoKey())
    assert r["rc"] == 4 and r["adverts"] == [] and r["log"] == []


def test_pair_mode_refuses_malformed_server_answer():
    r = run(FakeSeam([], sas="12"))
    assert r["rc"] == 1 and r["adverts"] == []


def test_pair_mode_stops_advert_when_poll_raises():
    class Boom(FakeSeam):
        def poll(self, req):
            raise RuntimeError("network")

    with pytest.raises(RuntimeError):
        run(Boom([]))


def test_cli_registers_pair_mode():
    import argparse

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command")
    lp.add_parser(sub)
    a = ap.parse_args(["pair-mode", "--class", "watch", "--minutes", "3"])
    assert (a.device_class, a.minutes) == ("watch", 3)
    with pytest.raises(SystemExit):
        ap.parse_args(["pair-mode", "--minutes", "30"])


# -- review: pairing mode must end with the process, and a Wi-Fi blip is not a crash ----


def test_pair_mode_sigterm_stops_the_advert_and_restores_handlers():
    """A plain `kill` (SIGTERM) used to end Python without running ``finally``: the
    avahi-publish child was orphaned and a root service file stayed in
    /etc/avahi/services, advertising after pairing mode was over."""
    import signal

    before = signal.getsignal(signal.SIGTERM)

    class Killed(FakeSeam):
        def poll(self, req):
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler), "SIGTERM would skip the advert's stop"
            handler(signal.SIGTERM, None)
            raise AssertionError("the handler must end pairing mode")  # pragma: no cover

    with pytest.raises(SystemExit):
        r_log: List[str] = []
        lp.run_pair_mode(Killed([]), device_class="laptop", label="x", window_s=60,
                         advertiser_factory=lambda t, n: FakeAdv(r_log),
                         complete=lambda c: {}, say=lambda s: None,
                         clock=Clock(), sleep=lambda s: None)
    assert r_log == ["start", "stop"]
    assert signal.getsignal(signal.SIGTERM) == before


def test_avahi_publish_child_dies_with_pairing_mode():
    """SIGKILL cannot run ``finally``: on Linux the child asks the kernel to end it when
    its parent goes (PR_SET_PDEATHSIG), so the advert never outlives pairing mode."""
    import inspect

    src = inspect.getsource(lp.AvahiPublish.start)
    assert "preexec_fn" in src and "_die_with_parent" in src


def test_identity_seam_poll_survives_a_network_blip(monkeypatch):
    import httpx

    def post(*a, **k):
        raise httpx.ConnectError("wifi dropped")

    monkeypatch.setattr(httpx, "post", post)
    seam = lp.IdentityJoinRequests("https://i", KEY)
    req = lp.validate_join_request(good_req(), KEY.pubkey)
    assert seam.poll(req) == {"state": "pending"}
    with pytest.raises(RuntimeError):
        seam.create("laptop", "x")
