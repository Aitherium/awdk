#!/usr/bin/env python3
"""Prove the app's command-channel code agrees with Identity's, byte for byte.

    python awdk/android/aither/check_commands.py --json-jar json.jar [--self-test]

Signs commands the way the server does (canonical JSON + HMAC-SHA256), runs the app's
Canon/Commands on a desktop JVM (org.json from --json-jar, the Android stubs from the SDK),
and checks every verdict: a valid command runs; a bad signature, another device, an
expired, already-run, unknown-verb, bad-argument or fractional command is refused; and a
result the app signs verifies the way Identity verifies it. --self-test also feeds a
tampered signature and expects this check to notice.
Exit: 0 agree, 1 disagree, 2 could not run (no JDK, jar or SDK).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src" / "com" / "aitherium" / "aither"
SIGNED = ("id", "tenant_id", "node_id", "verb", "args", "issued_by", "issued_at", "expires_at")
RESULT = ("id", "node_id", "ok", "output")
KEY, NODE, NOW = "ab" * 32, "kvh-test", 1_759_530_000


def canonical(rec: dict, fields: tuple = SIGNED) -> bytes:
    return json.dumps({f: rec.get(f) for f in fields}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode()


def sign(payload: bytes) -> str:
    return hmac.new(KEY.encode(), payload, hashlib.sha256).hexdigest()


def cases(tamper: bool) -> list:
    def cmd(**kw: object) -> dict:
        c = dict(id="cmd_x", tenant_id="t1", node_id=NODE, verb="collect-diagnostics", args={},
                 issued_by="user:ü☃\U0001F600", issued_at=NOW - 5, expires_at=NOW + 3600)
        c.update(kw)
        return c

    rows = [
        (cmd(verb="lend-on", args={"via": "lan"}), "RUN"),
        (cmd(issued_by='a"b\\c/d\x7f\n\x01'), "RUN"),
        (cmd(tenant_id=None, verb="check-update"), "RUN"),
        (cmd(node_id="someone-else"), "REFUSE addressed to another device"),
        (cmd(expires_at=NOW), "REFUSE expired"),
        (cmd(id="cmd_seen"), "REFUSE already run"),
        (cmd(verb="shell"), "REFUSE verb not allowed on this device"),
        (cmd(verb="lend-on", args={"via": "anywhere"}), "REFUSE argument not allowed for lend-on"),
        (cmd(issued_at=NOW + 0.5), "REFUSE not canonical: a fraction has no canonical form"),
    ]
    out = []
    for c, want in rows:
        c["sig"] = sign(canonical(c))
        out.append((c, want))
    bad = cmd()
    bad["sig"] = sign(canonical(bad))[:-1] + ("0" if not tamper else "1")
    bad["issued_by"] = "user:mallory"  # signed fields changed after signing
    out.append((bad, "REFUSE bad signature" if not tamper else "RUN"))
    return out


def android_jar() -> Path | None:
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or str(
        Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk")
    jar = Path(sdk) / "platforms" / "android-35" / "android.jar"
    return jar if jar.exists() else None


def run(json_jar: Path, tamper: bool) -> int:
    jar = android_jar()
    if not shutil.which("javac") or not jar or not json_jar.exists():
        print("check_commands: needs javac, the android-35 platform and --json-jar", file=sys.stderr)
        return 2
    rows = cases(tamper)
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        (t / "cmds.jsonl").write_text("".join(json.dumps(c) + "\n" for c, _ in rows),
                                      encoding="utf-8")
        cp_build = os.pathsep.join([str(json_jar), str(jar)])
        srcs = [str(SRC / "Canon.java"), str(HERE / "test" / "CanonCheck.java"),
                str(SRC / "Commands.java")]
        # Commands references the rest of the app; compile it all, run only the pure parts.
        srcs += [str(p) for p in SRC.glob("*.java") if str(p) not in srcs]
        r = subprocess.run(["javac", "-encoding", "UTF-8", "-nowarn", "-cp", cp_build, "-d",
                            str(t / "cls"), *srcs], capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stderr[-2000:], file=sys.stderr)
            return 2
        cp_run = os.pathsep.join([str(t / "cls"), str(json_jar), str(jar)])
        r = subprocess.run(["java", "-Dstdout.encoding=UTF-8", "-cp", cp_run,
                            "com.aitherium.aither.CanonCheck", KEY, NODE, str(NOW),
                            str(t / "cmds.jsonl")],
                           capture_output=True, text=True, encoding="utf-8")
    lines = r.stdout.splitlines()
    if r.returncode != 0 or len(lines) != len(rows) + 1:
        print(r.stdout[-1000:], r.stderr[-2000:], file=sys.stderr)
        return 2
    bad = 0
    for (c, want), got in zip(rows, lines):
        if got != want:
            bad += 1
            print(f"DISAGREE {c['verb']} {c['id']}: want {want!r}, app said {got!r}")
    res = json.loads(lines[-1][len("RESULT "):])
    if not hmac.compare_digest(sign(canonical(res, RESULT)), str(res.get("sig"))):
        bad += 1
        print("DISAGREE: Identity would not verify the app's signed result")
    print(f"check_commands: {len(rows)} commands + 1 result, {bad} disagreement(s)")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json-jar", required=True, type=Path)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        got = run(a.json_jar, tamper=True)
        if got == 2:
            return 2
        if got != 1:
            print("self-test: a forged command was not noticed")
            return 1
        print("self-test: a forged command is noticed")
    return run(a.json_jar, tamper=False)


if __name__ == "__main__":
    sys.exit(main())
