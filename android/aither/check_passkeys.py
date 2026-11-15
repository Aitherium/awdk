#!/usr/bin/env python3
"""Prove the app's passkey authenticator works against a real WebAuthn relying party.

    python awdk/android/aither/check_passkeys.py [--json-jar json.jar] [--self-test]

python-fido2's ``Fido2Server`` is the relying party. The app's ``Passkeys`` class runs on a
desktop JVM (pure java.security + org.json, the same source the phone runs):

1. register: the RP's creation options -> the app makes a passkey -> ``register_complete``
   accepts it; the authenticator data says UP, UV, BE=1, BS=1, the Aither AAGUID and
   signCount 0; the private key exists only in the vault item the app wrote.
2. authenticate: the RP's request -> a FRESH JVM that holds nothing but that vault item
   (a second enrolled device after the vault synced it) signs -> ``authenticate_complete``
   accepts it.
3. origin rules: rpId/origin matching, a non-browser app claiming a web origin, the
   native-app origin and the assetlinks check give the expected verdicts.

``--self-test`` flips one bit of the assertion signature and requires the RP to refuse it.
Without ``--json-jar`` the pinned org.json jar is fetched once from Maven Central into
``~/.cache/aither`` and checked by SHA-256 before use.
Exit: 0 all agree, 1 a disagreement, 2 could not run (no JDK, jar or python-fido2).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src" / "com" / "aitherium" / "aither"
RP_ID, ORIGIN = "example.aither.test", "https://example.aither.test"
AAGUID = "3d2e3505-9a19-4709-9cd2-516ba4ae1085"
JSON_JAR = ("https://repo1.maven.org/maven2/org/json/json/20240303/json-20240303.jar",
            "3cf6cd6892e32e2b4c1c39e0f52f5248a2f5b37646fdfbb79a66b46b618414ed")
ORIGIN_WANT = [
    "RP ALLOW", "RP ALLOW", "RP ALLOW", "RP REFUSE", "RP REFUSE", "RP REFUSE", "RP REFUSE",
    "RP ALLOW", "CALLER REFUSE",
    None,  # NATIVE line, checked by shape below
    "LINKS true false",
]


def fetch_json_jar() -> Path | None:
    """The pinned org.json jar (the real one: android.jar's org.json is stubs)."""
    url, sha = JSON_JAR
    dest = Path.home() / ".cache" / "aither" / url.rsplit("/", 1)[1]
    if not dest.exists():
        import urllib.request

        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 - pinned https
                data = r.read()
        except OSError as exc:
            print(f"check_passkeys: could not fetch org.json ({exc})", file=sys.stderr)
            return None
        if hashlib.sha256(data).hexdigest() != sha:
            print("check_passkeys: org.json jar does not match its pinned SHA-256",
                  file=sys.stderr)
            return None
        dest.write_bytes(data)
    if hashlib.sha256(dest.read_bytes()).hexdigest() != sha:
        print(f"check_passkeys: {dest} does not match its pinned SHA-256", file=sys.stderr)
        return None
    return dest


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def client_data(kind: str, challenge: str) -> bytes:
    return json.dumps({"type": kind, "challenge": challenge, "origin": ORIGIN,
                       "crossOrigin": False}, separators=(",", ":")).encode()


class Jvm:
    def __init__(self, tmp: Path, json_jar: Path):
        self.tmp, self.cp = tmp, os.pathsep.join([str(tmp / "cls"), str(json_jar)])

    def compile(self, json_jar: Path) -> str:
        r = subprocess.run(["javac", "-encoding", "UTF-8", "-nowarn", "-cp", str(json_jar),
                            "-d", str(self.tmp / "cls"), str(SRC / "Passkeys.java"),
                            str(HERE / "test" / "PasskeysCheck.java")],
                           capture_output=True, text=True, encoding="utf-8")
        return "" if r.returncode == 0 else r.stderr[-2000:]

    def run(self, *args: str) -> str:
        r = subprocess.run(["java", "-cp", self.cp, "com.aitherium.aither.PasskeysCheck", *args],
                           capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            raise RuntimeError(r.stderr[-2000:])
        return r.stdout.strip()


def run(json_jar: Path, tamper: bool) -> int:
    try:
        from fido2.server import Fido2Server
        from fido2.webauthn import (
            AuthenticatorData,
            PublicKeyCredentialRpEntity,
            PublicKeyCredentialUserEntity,
            UserVerificationRequirement,
        )
    except ImportError:
        print("check_passkeys: needs python-fido2 2.x", file=sys.stderr)
        return 2
    if not shutil.which("javac") or not shutil.which("java") or not json_jar.exists():
        print("check_passkeys: needs javac/java and --json-jar", file=sys.stderr)
        return 2
    bad: list[str] = []
    server = Fido2Server(PublicKeyCredentialRpEntity(id=RP_ID, name="Aither RP fixture"))
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        jvm = Jvm(t, json_jar)
        err = jvm.compile(json_jar)
        if err:
            print(err, file=sys.stderr)
            return 2
        allow = HERE / "assets" / "privileged_browsers.json"

        # 1. register
        opts, state = server.register_begin(
            PublicKeyCredentialUserEntity(id=b"user-aither-1", name="a@b.c", display_name="A"),
            user_verification=UserVerificationRequirement.REQUIRED)
        pk = dict(opts)["publicKey"]
        (t / "create.json").write_text(json.dumps(pk), encoding="utf-8")
        cdj = client_data("webauthn.create", pk["challenge"])
        vault = t / "vault-item.json"
        reg = json.loads(jvm.run("create", str(t / "create.json"), RP_ID,
                                 hashlib.sha256(cdj).hexdigest(), str(vault)))
        reg["response"]["clientDataJSON"] = b64u(cdj)
        try:
            auth_data = server.register_complete(state, reg)
        except Exception as exc:  # noqa: BLE001 - any RP refusal is the verdict
            print(f"DISAGREE: the RP refused the registration: {exc}")
            return 1
        flags = auth_data.flags
        want = {"UP": AuthenticatorData.FLAG.UP, "UV": AuthenticatorData.FLAG.UV,
                "BE": AuthenticatorData.FLAG.BE, "BS": AuthenticatorData.FLAG.BS}
        for name, bit in want.items():
            if not flags & bit:
                bad.append(f"registration flag {name} is not set")
        if str(auth_data.credential_data.aaguid) != AAGUID:
            bad.append(f"AAGUID {auth_data.credential_data.aaguid} is not Aither's")
        if auth_data.counter != 0:
            bad.append(f"signCount {auth_data.counter} is not 0")
        item = json.loads(vault.read_text(encoding="utf-8"))
        if item.get("type") != "passkey" or not item.get("private_key_pkcs8"):
            bad.append("the vault item does not carry the passkey")

        # 2. authenticate from a fresh JVM holding only the vault item ("second device")
        req, astate = server.authenticate_begin(
            [auth_data.credential_data], user_verification=UserVerificationRequirement.REQUIRED)
        rq = dict(req)["publicKey"]
        (t / "get.json").write_text(json.dumps(rq), encoding="utf-8")
        cdj2 = client_data("webauthn.get", rq["challenge"])
        out = jvm.run("get", str(t / "get.json"), RP_ID, hashlib.sha256(cdj2).hexdigest(),
                      str(vault))
        if out == "NO-CANDIDATE":
            bad.append("the second device found no passkey for the RP")
        else:
            asr = json.loads(out)
            asr["response"]["clientDataJSON"] = b64u(cdj2)
            if tamper:
                sig = bytearray(unb64u(asr["response"]["signature"]))
                sig[-1] ^= 1
                asr["response"]["signature"] = b64u(bytes(sig))
            try:
                server.authenticate_complete(astate, [auth_data.credential_data], asr)
                adata = AuthenticatorData(unb64u(asr["response"]["authenticatorData"]))
                if not (adata.flags & AuthenticatorData.FLAG.BS) or adata.counter != 0:
                    bad.append("assertion flags/counter are not BS=1, signCount 0")
            except Exception as exc:  # noqa: BLE001
                bad.append(f"the RP refused the assertion: {exc}")

        # 3. origin rules
        lines = jvm.run("origin", str(allow)).splitlines()
        if len(lines) != len(ORIGIN_WANT):
            bad.append(f"origin cases: {len(lines)} lines, want {len(ORIGIN_WANT)}")
        else:
            for got, want_line in zip(lines, ORIGIN_WANT):
                if want_line is None:
                    if not (got.startswith("NATIVE android:apk-key-hash:")
                            and got.endswith(" false")):
                        bad.append(f"native origin: {got!r}")
                elif got != want_line:
                    bad.append(f"origin case: want {want_line!r}, app said {got!r}")
    for b in bad:
        print("DISAGREE:", b)
    print(f"check_passkeys: register + second-device sign-in + {len(ORIGIN_WANT)} origin "
          f"cases, {len(bad)} disagreement(s)")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json-jar", type=Path)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.json_jar is None:
        a.json_jar = fetch_json_jar()
        if a.json_jar is None:
            return 2
    if a.self_test:
        got = run(a.json_jar, tamper=True)
        if got == 2:
            return 2
        if got != 1:
            print("self-test: a forged assertion was accepted")
            return 1
        print("self-test: a forged assertion is refused")
    return run(a.json_jar, tamper=False)


if __name__ == "__main__":
    sys.exit(main())
