"""adk.skilltasks.terminal: the re-read stall guard and the podman jail.

Stall (measured: the failed SASE runs re-read the same files, 29 of 36 reads in one run):

* a tool's printed result reaches the MODEL (the loop sandbox's stdout), not the host
  process -- before, 1 of 58 SASE turn results carried any stdout;
* (a) the evidence ledger keys a read by the file's sha256: re-reading an unchanged file
  returns "UNCHANGED since t<n>" and the known summary, not the text again;
* (b) a scripted model that keeps re-reading is stopped within ``stall_actions`` actions:
  intent=stuck, and PRISM rotates to test-first / minimal-patch (try_change);
* (c) the situation lists the files already read, and flags one that changed since.

Jail (on Windows the policy alone was not a jail): skipped where podman is not usable.

* a program the model WRITES and runs (which the command policy cannot see into) has no
  network, and cannot write outside the workspace; the verifier's private state is intact;
* do-nothing and the reference still score 0.0 and 1.0 with every command in the jail.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import List

import pytest
from adk.skilltasks import load_task
from adk.skilltasks.gate import _make_task
from adk.skilltasks.terminal import (
    POLICY_ONLY,
    STALL_ACTIONS,
    SkillTaskTerminalEnv,
    do_nothing_policy,
    reference_policy,
    run_policy,
)

CORPUS = Path(__file__).resolve().parents[2] / "AitherOS" / "lib" / "training" / "skilltasks"


@pytest.fixture(autouse=True)
def _policy_mode_by_default(monkeypatch):
    # the stall tests are about the loop, not the sandbox: never pay a container start
    monkeypatch.setenv("ADK_SKILLTASK_JAIL", "off")


def _env(tmp_path: Path, **kw) -> SkillTaskTerminalEnv:
    return SkillTaskTerminalEnv(load_task(_make_task(tmp_path, "t")), **kw)


class _Scripted:
    def __init__(self, replies: List[str]) -> None:
        self.name = self.model = "scripted"
        self.replies = replies
        self.calls: list = []

    async def generate(self, messages, *, temperature=0.7, max_tokens=None, **opts):
        from adk.core.model import ModelResponse

        self.calls.append(list(messages))
        text = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        return ModelResponse(
            text=text,
            model=self.model,
            finish_reason="stop",
            usage={"prompt_tokens": 50, "completion_tokens": 20},
        )


def _content(m) -> str:
    return str(m["content"] if isinstance(m, dict) else getattr(m, "content", ""))


def _cfg(sase: bool, calls: int):
    from adk.reasoning.solve import Budget, LoopConfig

    return LoopConfig(
        budget=Budget(max_llm_calls=calls, max_actions=60, turn_s=60.0),
        sase=sase,
        prism=sase,
        grounded=sase,
        planning=False,
        daydream=False,
    )


def _result_section(user: str) -> str:
    """The "Result of your previous code" part of a turn prompt (stdout lives there)."""
    tail = user.split("Result of your previous code:", 1)[1]
    return tail.split("\nMEMORY", 1)[0]


# ----------------------------------------------------------------- the model sees tool output
def test_tool_output_reaches_the_model_not_the_host_process(tmp_path, capsys):
    pytest.importorskip("numpy")
    from adk.reasoning.solve import solve

    env = _env(tmp_path)
    (env.root / "a.txt").write_text("MARKER-ALPHA-42\n", encoding="utf-8")
    # two reads in one turn: LAST ACTION shows only the second, so the first file's text
    # can reach the model ONLY through the printed result
    model = _Scripted(['Read both.\n```python\nread("a.txt")\nread("seed.txt")\n```\n'])
    try:
        asyncio.run(solve(env, model, config=_cfg(False, 2)))
        assert len(model.calls) == 2
        result = _result_section(_content(model.calls[1][-1]))
        assert "MARKER-ALPHA-42" in result, result
        assert "MARKER-ALPHA-42" not in capsys.readouterr().out  # not the host's stdout
    finally:
        env.close()


# ----------------------------------------------------------------- (a) unchanged re-reads
def test_a_reread_of_an_unchanged_file_returns_unchanged_and_the_summary(tmp_path):
    env = _env(tmp_path)
    body = "".join("line %d of the contract\n" % i for i in range(1, 11))
    try:
        env.write("big.txt", body)
        first = env.read("big.txt")
        assert first == body and env.rereads == 0
        assert env.steps[-1].family.startswith("read:big.txt@")  # the evidence row has the hash
        again = env.read("big.txt")
        assert again.startswith("UNCHANGED since t")
        assert "line 1 of the contract" in again  # the known summary...
        assert "line 10 of the contract" not in again  # ...not the text again
        assert env.rereads == 1 and env.steps[-1].reread and not env.steps[-1].new_evidence
        env.write("big.txt", body + "line 11 is new\n")  # a changed file is new evidence
        third = env.read("big.txt")
        assert "line 11 is new" in third and not third.startswith("UNCHANGED")
        assert env.steps[-1].new_evidence and env.rereads == 1
    finally:
        env.close()


# A re-read is "unchanged" only while the model can still SEE the text. The loop's working
# memory evicts whole turns (wm_tokens 1800 by default; terminal_config does not raise it),
# so after ~6 KB of other reads the turn that carried big.txt is gone: the re-read must
# return the text again, or patch(path, old, new) can never get its exact `old`.
def _evicting_run(tmp_path, mode: str):
    from adk.reasoning.solve import solve
    from adk.skilltasks.run import terminal_config

    env = _env(tmp_path)
    big = "".join("line %03d of big MARK%03d xxxxxxxxxxxxxx\n" % (i, i) for i in range(1, 150))
    (env.root / "big.txt").write_text(big, encoding="utf-8")
    for name in ("f1", "f2", "f3"):
        (env.root / (name + ".txt")).write_text(
            "".join("%s filler line %03d %s\n" % (name, i, "y" * 24) for i in range(120)),
            encoding="utf-8",
        )
    head = "SITUATION: s\nANALYSIS: a\nSYNTHESIS: s\nEXECUTION:\n" if "sase" in mode else ""
    replies = [
        '%s```python\nread("%s")\n```\n' % (head, f)
        for f in ("big.txt", "big.txt", "f1.txt", "f2.txt", "f3.txt", "big.txt", "big.txt")
    ]
    model = _Scripted(replies + ["%s```python\nls()\n```\n" % head])
    res = asyncio.run(solve(env, model, config=terminal_config(mode, 8, 600.0, None)))
    return env, model, res


@pytest.mark.parametrize("mode", ["terminal-plain", "terminal-sase"])
def test_a_reread_after_the_turn_was_evicted_returns_the_text_again(tmp_path, mode):
    pytest.importorskip("numpy")
    env, model, _res = _evicting_run(tmp_path, mode)
    try:
        assert len(model.calls) == 8
        seen = ["\n".join(_content(m) for m in call) for call in model.calls]
        results = [_result_section(_content(call[-1])) for call in model.calls]
        reads = [
            s
            for s in env.steps
            if s.done and s.source == "model" and s.args.get("path") == "big.txt"
        ]
        assert len(reads) == 4
        # turn 1 delivered the text; turn 2 re-read it while it was still in context
        assert "MARK100" in results[1]
        assert reads[1].reread and "UNCHANGED since t" in results[2]
        assert "MARK100" not in results[2] and "MARK100" in seen[2]  # still in an earlier turn
        # by turn 6 the turns that carried big.txt were evicted: the model cannot see it
        assert "MARK100" not in seen[5], "the scenario no longer evicts: grow the filler files"
        situation = _content(model.calls[5][-1])
        line = next(ln for ln in situation.splitlines() if ln.startswith("- big.txt"))
        assert "no longer in your context" in line
        # ... so the turn-6 re-read returns the TEXT, as evidence, not "unchanged"
        assert not reads[2].reread and reads[2].new_evidence
        assert "MARK100" in results[6] and "MARK149" in results[6]
        assert "UNCHANGED since" not in results[6]
        assert env.redelivered == 1
        # ... and once it is back in context, the next re-read is "unchanged" again
        assert reads[3].reread and "UNCHANGED since t" in results[7]
        line = next(
            ln for ln in _content(model.calls[7][-1]).splitlines() if ln.startswith("- big.txt")
        )
        assert "no longer in your context" not in line and "unchanged" in line
        assert env.rereads == 2
    finally:
        env.close()


# ----------------------------------------------------------------- (b) stuck + rotation
REREAD_REPLY = """SITUATION: I need the seed file.
ANALYSIS: none
SYNTHESIS: none
EXECUTION:
```python
for _ in range(12):
    read("seed.txt")
```
"""


def test_a_model_that_keeps_rereading_is_rotated_within_n_actions(tmp_path):
    pytest.importorskip("numpy")
    from adk.reasoning.solve import solve

    env = _env(tmp_path)
    model = _Scripted([REREAD_REPLY])
    try:
        res = asyncio.run(solve(env, model, config=_cfg(True, 2)))
        assert len(model.calls) == 2
        user2 = _content(model.calls[1][-1])
        # turn 1 ended after the first read (new) + STALL_ACTIONS re-reads, not 12
        assert "You took %d action(s)" % (STALL_ACTIONS + 1) in user2, user2[-1500:]
        assert "STALLED: no new evidence in the last %d actions" % STALL_ACTIONS in user2
        assert "INTENT: STUCK" in user2
        log = res.stats["prism"]["log"]
        assert log and "no new evidence" in log[0]["why"]
        # rule_first (test-first) was active, so it rotates to minimal-patch (try_change)
        assert log[0]["from"] == "rule_first" and log[0]["to"] == "analogy"
        assert "STRATEGY: minimal-patch" in user2
        assert env.stall_rotations >= 1
    finally:
        env.close()


# ----------------------------------------------------------------- (c) the situation
def test_the_situation_lists_the_files_already_read(tmp_path):
    env = _env(tmp_path)
    try:
        assert "FILES YOU ALREADY READ" not in env.render(env.observe(), None)
        env.read("seed.txt")
        text = env.render(env.observe(), None)
        assert "FILES YOU ALREADY READ" in text
        line = next(ln for ln in text.splitlines() if ln.startswith("- seed.txt"))
        assert "unchanged" in line and "t1" in line
        env.write("seed.txt", "changed\n")
        line = next(
            ln for ln in env.render(env.observe(), None).splitlines() if ln.startswith("- seed.txt")
        )
        assert "CHANGED since" in line
    finally:
        env.close()


# ----------------------------------------------------------------- the jail
def _podman_ok() -> bool:
    from adk.skilltasks.jail import probe_podman

    prefix, _why = probe_podman(build=False)
    return prefix is not None


needs_podman = pytest.mark.skipif(
    not _podman_ok(), reason="podman (and the jail image) is not usable on this host"
)

NET_PROBE = """m = __import__("so" + "cket")
try:
    m.create_connection(("1.1.1.1", 53), 3)
    print("CONNECTED")
except OSError as exc:
    print("NO-NETWORK", exc)
"""

ESCAPE = """import os
for target in (os.path.join(os.getcwd(), "..", "private", "pwned"), "/etc/pwned",
               os.path.join(os.getcwd(), "..", "sibling.txt")):
    try:
        with open(target, "w") as fh:
            fh.write("x")
        print("WROTE", target)
    except OSError as exc:
        print("REFUSED", target, type(exc).__name__)
"""


@needs_podman
def test_the_jail_has_no_network_even_for_a_program_the_policy_cannot_read(tmp_path):
    env = _env(tmp_path, jail="podman")
    try:
        env.write("probe.py", NET_PROBE)  # the obfuscated import passes the text policy
        out = env.sh("python probe.py")
        assert env.sandbox == "podman"
        assert "NO-NETWORK" in out and "CONNECTED" not in out, out
    finally:
        env.close()


@needs_podman
def test_writes_outside_the_workspace_really_fail_in_the_jail(tmp_path):
    env = _env(tmp_path, jail="podman")
    try:
        env.write("esc.py", ESCAPE)
        out = env.sh("python esc.py")
        assert env.sandbox == "podman"
        assert "WROTE" not in out and out.count("REFUSED") == 3, out
        assert not (env.ws.private / "pwned").exists()
        assert not (env.ws.run / "sibling.txt").exists()
        assert env.final().reward == 0.0 and not env.invalid  # scored, not tampered
    finally:
        env.close()


ESCAPE_TO_PRIVATE = """import os
target = os.path.join(os.getcwd(), "..", "private", "pwned")
with open(target, "w") as fh:
    fh.write("x")
print("WROTE", target)
"""


def _policy_shell_runs_python() -> bool:
    from adk.skilltasks.env import find_shell

    here = os.path.dirname(sys.executable)
    local = any(os.path.isfile(os.path.join(here, n)) for n in ("python", "python.exe"))
    return find_shell() is not None and (local or shutil.which("python") is not None)


@pytest.mark.skipif(not _policy_shell_runs_python(), reason="no POSIX shell + python here")
def test_without_the_jail_the_same_program_escapes_and_the_episode_is_invalid(tmp_path):
    """As it was before the jail: policy-only mode is loudly marked, and a written program walks
    out of the workspace (caught only by the tamper guard). Only the task's own private/ is
    targeted, so the host is never written outside the test's tmp_path."""
    env = _env(tmp_path, jail="off")
    try:
        env.write("esc.py", ESCAPE_TO_PRIVATE)
        out = env.sh("python esc.py")
        assert env.sandbox == POLICY_ONLY
        assert "WROTE" in out, out
        with pytest.raises(Exception, match="invalid"):
            env.final()
    finally:
        env.close()


def _nop_through_the_jail(env: SkillTaskTerminalEnv) -> None:
    env.sh("ls -la && cat seed.txt 2>/dev/null; true")
    do_nothing_policy(env)


@needs_podman
def test_do_nothing_and_reference_score_zero_and_one_through_the_jail(tmp_path):
    for policy, want in ((_nop_through_the_jail, 0.0), (lambda e: reference_policy(e, True), 1.0)):
        env = _env(tmp_path / str(want), jail="podman")
        try:
            assert run_policy(env, policy).reward == want
            assert env.sandbox == "podman" and env._jail is not None and env._jail.commands >= 1
        finally:
            env.close()


@needs_podman
@pytest.mark.skipif(not CORPUS.is_dir(), reason="the skill-task corpus is not in this checkout")
@pytest.mark.parametrize(
    "name", ["checker-exit-contract", "commit-only-mine", "dns-policy-inplace"]
)
def test_corpus_reference_scores_one_with_every_write_in_the_jail(name):
    env = SkillTaskTerminalEnv(CORPUS / name, jail="podman")
    try:
        assert run_policy(env, lambda e: reference_policy(e, True)).reward == 1.0
        assert env.sandbox == "podman" and env._jail is not None and env._jail.commands >= 1
    finally:
        env.close()


def _root_on_posix() -> bool:
    return os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0


needs_rootful = pytest.mark.skipif(
    not _root_on_posix(),
    reason="needs root on Linux (rootful podman): only there is a container uid a host uid",
)


@needs_podman
@needs_rootful
def test_the_jail_starts_as_root_under_rootful_podman_and_the_harness_reads_the_result(tmp_path):
    """Real rootful podman, harness = root: the workspace is root's and 0700 (what
    ``mkdtemp`` makes). The jail must START, write it, and the harness must read the
    result back -- and the files must never belong to a real host account."""
    import pwd

    from adk.skilltasks.jail import IDMAP_SIZE, JAIL_UID, open_jail

    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    (work / "seed.txt").write_text("from the harness\n", encoding="utf-8")
    assert work.stat().st_uid == 0
    jail, why = open_jail(work, "podman")
    assert jail is not None and why == ""
    try:
        uid = jail.host_uid()
        assert uid is not None and uid != JAIL_UID and uid >= IDMAP_SIZE
        with pytest.raises(KeyError):  # nobody's account: no real user owns a task file
            pwd.getpwuid(uid)
        rc, out, err, _to = jail.run("id -u && echo from-the-jail >> seed.txt && echo x > new.txt")
        assert (rc, out.strip()) == (0, str(JAIL_UID)), (rc, out, err)  # uid 1000 INSIDE
        # the harness (root) reads what the jail wrote, and owns nothing it must chase
        assert (work / "seed.txt").read_text(encoding="utf-8") == (
            "from the harness\nfrom-the-jail\n"
        )
        assert (work / "new.txt").read_text(encoding="utf-8") == "x\n"
        assert (work / "new.txt").stat().st_uid == uid and work.stat().st_uid == uid
        # a file the harness writes BETWEEN commands is root's: the jail can still edit it
        (work / "later.txt").write_text("a\n", encoding="utf-8")
        (work / "sub").mkdir(mode=0o700)
        assert (work / "later.txt").stat().st_uid == 0
        rc, out, err, _to = jail.run("echo b >> later.txt && touch sub/in && echo ok")
        assert (rc, out.strip()) == (0, "ok"), (rc, out, err)
        assert (work / "later.txt").read_text(encoding="utf-8") == "a\nb\n"
        # the mapping is the jail's own: container root is not host root either
        rc, out, _err, _to = jail.run("cat /proc/self/uid_map")
        assert out.split() == ["0", str(uid - JAIL_UID), str(IDMAP_SIZE)], out
    finally:
        jail.close()


@needs_podman
@needs_rootful
def test_a_planted_symlink_is_never_followed_when_the_workspace_is_handed_over(tmp_path):
    """The harness re-owns the workspace as ROOT before each command: a symlink the model
    left pointing outside must be re-owned itself, never its target."""
    from adk.skilltasks.jail import open_jail

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("root's\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    jail, _why = open_jail(work, "podman")
    assert jail is not None
    try:
        # inside the jail the target does not exist; on the HOST the same link resolves
        rc, _out, err, _to = jail.run("ln -s %s f && ln -s %s d && echo ok" % (
            outside / "secret.txt", outside))  # fmt: skip
        assert rc == 0, err
        assert jail.run("true")[0] == 0  # the walk that hands the workspace over ran again
        assert (work / "f").is_symlink() and (work / "d").is_symlink()
        assert (work / "f").lstat().st_uid == jail.host_uid()  # the LINK was re-owned ...
        # ... and what it points at on the host was not
        assert outside.stat().st_uid == 0 and (outside / "secret.txt").stat().st_uid == 0
    finally:
        jail.close()


# ----------------------------------------------------------------- the jail, without podman
# A stand-in for ``podman run``: it ignores its argv and speaks the command server's line
# protocol, so the client half (start checks, request/reply pairing, the flags) is tested
# on hosts where the real jail tests above are skipped.
_FAKE_PODMAN = r"""
import base64, json, os, sys
sys.stdout.write(os.environ["FAKE_JAIL_READY"] + "\n"); sys.stdout.flush()
for line in sys.stdin:
    req = json.loads(line)
    cmd = base64.b64decode(req["c"]).decode()
    sys.stdout.write("noise that is not a reply\n")
    sys.stdout.write(json.dumps({"id": req["id"] - 1, "rc": 9, "o": "", "e": ""}) + "\n")
    sys.stdout.write(json.dumps({"id": req["id"], "n": req["n"], "rc": 3, "to": False,
                                 "o": base64.b64encode(("ran " + cmd).encode()).decode(),
                                 "e": base64.b64encode(b"warn").decode()}) + "\n")
    sys.stdout.flush()
"""


def _fake_jail(tmp_path: Path, monkeypatch, ready: str):
    from adk.skilltasks.jail import Jail

    script = tmp_path / "fake_podman.py"
    script.write_text(_FAKE_PODMAN, encoding="utf-8")
    monkeypatch.setenv("FAKE_JAIL_READY", ready)
    work = tmp_path / "work"
    work.mkdir()
    return Jail(work, [sys.executable, str(script)])


def test_the_jail_argv_carries_every_confinement_flag(tmp_path, monkeypatch):
    jail = _fake_jail(tmp_path, monkeypatch, "{}")
    jail.name = "adk-jail-x"
    argv = jail.argv()
    for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--rm"):
        assert flag in argv, flag
    assert argv[argv.index("--user") + 1] == "1000:1000"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert len(mounts) == 1 and mounts[0].endswith(":/work:rw")  # only the workspace
    assert "--privileged" not in argv and not any(a.startswith("--network=host") for a in argv)


def test_a_mapped_jail_gets_its_own_user_namespace_and_the_verifier_shares_it(
    tmp_path, monkeypatch
):
    """Rootful podman: container uid 1000 must not be HOST uid 1000 (a real account). The
    argv maps the container's ids onto a block of its own, and the verifier jail takes the
    shell jail's block so both own the one workspace."""
    import adk.skilltasks.jail as jail_mod

    work = tmp_path / "work"
    work.mkdir()
    base = 0x40000000 + 7 * jail_mod.IDMAP_SIZE
    shell = jail_mod.Jail(work, ["podman"], idmap_base=base)
    shell.name = "adk-jail-x"
    argv = shell.argv()
    ids = "0:%d:%d" % (base, jail_mod.IDMAP_SIZE)
    assert argv[argv.index("--uidmap") + 1] == ids and argv[argv.index("--gidmap") + 1] == ids
    assert argv.index("--uidmap") < argv.index(shell.image)  # a run flag, not the command's
    assert argv[argv.index("--user") + 1] == "1000:1000"
    assert shell.host_uid() == base + 1000

    started = []
    monkeypatch.setattr(jail_mod.Jail, "start", lambda self: started.append(self))
    verifier = jail_mod.open_verifier_jail(shell, tmp_path / "private", tmp_path / "tests")
    assert started == [verifier] and verifier.idmap_base == base

    # an unmapped jail (not root, or Windows) keeps the engine's own mapping
    monkeypatch.setattr(jail_mod, "rootful_here", lambda: False)
    plain = jail_mod.Jail(work, ["podman"])
    plain.name = "adk-jail-y"
    assert plain.idmap_base is None and plain.host_uid() is None
    assert "--uidmap" not in plain.argv() and "--gidmap" not in plain.argv()

    # root on Linux: a block is picked for the episode
    monkeypatch.setattr(jail_mod, "rootful_here", lambda: True)
    monkeypatch.setattr(jail_mod, "pick_idmap_base", lambda: base)
    assert jail_mod.Jail(work, ["podman"]).idmap_base == base


@pytest.mark.parametrize("mapped", [True, False])
def test_an_unwritable_workspace_is_refused_with_its_real_cause(
    tmp_path, monkeypatch, caplog, mapped
):
    """The refusal used to blame "rootless podman" whatever happened, also under rootful
    podman. The HOST LOG names what was measured: the mount's owner and mode, and which
    case it is. The exception text reaches the model and the run report, so it is generic."""
    import adk.skilltasks.jail as jail_mod

    script = tmp_path / "fake_podman.py"
    script.write_text(_FAKE_PODMAN, encoding="utf-8")
    monkeypatch.setenv("FAKE_JAIL_READY", '{"ready": 1, "uid": 1000, "w": false, "nd": true}')
    monkeypatch.setattr(jail_mod, "rootful_here", lambda: False)
    monkeypatch.setattr(jail_mod, "_give_to", lambda root, uid: None)  # the chown "failed"
    work = tmp_path / "work"
    work.mkdir()
    base = 0x40000000
    jail = jail_mod.Jail(work, [sys.executable, str(script)], idmap_base=base if mapped else None)
    with caplog.at_level("WARNING"), pytest.raises(jail_mod.JailUnavailableError) as exc:
        jail.start()
    public = str(exc.value)
    assert "cannot write the workspace" in public and "host log" in public
    for secret in (str(work), str(tmp_path), "uid", "mode", str(base), str(base + 1000)):
        assert secret not in public, (secret, public)
    msg = caplog.text
    assert "cannot write the workspace" in msg and str(work) in msg
    assert "owned by host uid %d, mode " % work.stat().st_uid in msg
    if mapped:
        assert "host uid %d (rootful podman" % (base + 1000) in msg and "rootless" not in msg
    elif os.name == "nt":
        assert "WSL distro" in msg and "rootless" not in msg
    else:
        assert "rootless" in msg and "sub-uid" in msg


def test_the_jail_client_pairs_a_reply_with_its_request(tmp_path, monkeypatch):
    jail = _fake_jail(tmp_path, monkeypatch, '{"ready": 1, "uid": 1000, "w": true, "nd": true}')
    try:
        rc, out, err, timed_out = jail.run("echo 'a b' \"c\"", timeout=20)
        # the stale reply (another id, rc 9) and the non-JSON line are skipped
        assert (rc, out, err, timed_out) == (3, "ran echo 'a b' \"c\"", "warn", False)
        assert jail.run("second", timeout=20)[1] == "ran second"
        assert jail.starts == 1 and jail.commands == 2  # one container for the episode
    finally:
        jail.close()
    assert not jail.alive()


@pytest.mark.parametrize(
    "ready, why",
    [
        ('{"ready": 1, "uid": 0, "w": true, "nd": true}', "runs as root"),
        ('{"ready": 1, "uid": 1000, "w": false, "nd": true}', "cannot write the workspace"),
        ('{"nope": 1}', "did not start"),
    ],
)
def test_the_jail_refuses_to_start_as_root_or_without_a_writable_workspace(
    tmp_path, monkeypatch, ready, why
):
    from adk.skilltasks.jail import JailUnavailableError

    jail = _fake_jail(tmp_path, monkeypatch, ready)
    with pytest.raises(JailUnavailableError, match=why):
        jail.start()
    assert not jail.alive()


def test_a_jail_that_cannot_start_is_an_error_never_a_silent_policy_fallback(tmp_path, monkeypatch):
    import adk.skilltasks.terminal as terminal

    env = _env(tmp_path / "t", jail="podman")
    jail = _fake_jail(tmp_path, monkeypatch, '{"ready": 1, "uid": 0}')
    monkeypatch.setattr(terminal, "open_jail", lambda root, mode: (jail, ""))
    # the verifier jail is opened with the shell jail; this test is about the shell's restart
    monkeypatch.setattr(terminal, "open_verifier_jail", lambda shell, private, tests: shell)
    try:
        out = env.sh("echo hi")
        assert env.sandbox == "podman"
        assert "the jail could not start" in out and "hi" not in out.replace("echo hi", "")
        assert env.steps[-1].exit_class == 3
    finally:
        env.close()


def _podman_that_cannot_start(tmp_path: Path, monkeypatch, ready: str) -> None:
    """podman and the image "exist" (the probe passes) but the container reports ``ready``."""
    import adk.skilltasks.jail as jail_mod

    script = tmp_path / "fake_podman.py"
    script.write_text(_FAKE_PODMAN, encoding="utf-8")
    monkeypatch.setenv("FAKE_JAIL_READY", ready)
    monkeypatch.setattr(
        jail_mod, "probe_podman", lambda build=True: ([sys.executable, str(script)], "")
    )


@pytest.mark.parametrize(
    "ready, why",
    [
        ('{"ready": 1, "uid": 1000, "w": false, "nd": true}', "cannot write the workspace"),
        ('{"ready": 1, "uid": 0, "w": true, "nd": true}', "runs as root"),
    ],
)
def test_auto_mode_falls_back_to_the_policy_when_the_jail_cannot_start(
    tmp_path, monkeypatch, caplog, ready, why
):
    # podman on PATH, image present, but uid 1000 cannot write the workspace (e.g. rootless
    # sub-uid mapping): the run must be POLICY-ONLY and say so, not "podman" with a dead sh
    _podman_that_cannot_start(tmp_path, monkeypatch, ready)
    env = _env(tmp_path / "t", jail="auto")
    try:
        with caplog.at_level("WARNING"):
            assert env.jail_status() == POLICY_ONLY
        assert env.sandbox == POLICY_ONLY and env._jail is None
        assert why in env.jail_why
        assert POLICY_ONLY.upper() in caplog.text  # loud, never silent
        out = env.sh("echo hi")
        assert "the jail could not start" not in out
        if env._shell is not None:  # the policy shell really runs the command
            assert "hi" in out.replace("echo hi", "") and env.steps[-1].exit_class == 0
    finally:
        env.close()


def test_a_required_jail_that_cannot_start_is_refused_before_any_command(tmp_path, monkeypatch):
    from adk.skilltasks.task import TaskError

    _podman_that_cannot_start(
        tmp_path, monkeypatch, '{"ready": 1, "uid": 1000, "w": false, "nd": true}'
    )
    env = _env(tmp_path / "t", jail="podman")
    try:
        with pytest.raises(TaskError, match="required .* cannot write the workspace"):
            env.jail_status()  # what run.py calls before the first model call: exit 2
        assert env.sandbox == "unprobed" and env._jail is None
    finally:
        env.close()


def test_open_jail_returns_a_started_jail(tmp_path, monkeypatch):
    from adk.skilltasks.jail import open_jail

    _podman_that_cannot_start(
        tmp_path, monkeypatch, '{"ready": 1, "uid": 1000, "w": true, "nd": true}'
    )
    work = tmp_path / "work"
    work.mkdir()
    jail, why = open_jail(work, "auto")
    try:
        assert jail is not None and why == "" and jail.alive() and jail.starts == 1
        assert jail.run("x", timeout=20)[1] == "ran x" and jail.starts == 1
    finally:
        if jail is not None:
            jail.close()


# A process the model leaves running shares the workspace with the HOST harness, which
# checks a path and then opens it by name (read/write/patch, the try_change rollback): a
# symlink swapped in between is followed on the host, outside the jail. So nothing may
# outlive its command in the SHELL jail either.
def test_the_shell_jail_kills_what_a_command_left_behind(tmp_path, monkeypatch):
    env, requests = _exec_env(tmp_path, monkeypatch, jail="podman")
    try:
        env.sh("python d.py >/dev/null 2>&1 &")
        env.sh("echo again")
        sent = [r for r in requests() if r["jail"] == "shell"]
        assert [r["cmd"] for r in sent] == ["python d.py >/dev/null 2>&1 &", "echo again"]
        assert all(r["reap"] == 1 for r in sent), sent
        assert env._jail is not None and env._jail.reap
    finally:
        env.close()


def test_the_command_server_reaps_after_every_command_and_only_as_pid_1():
    """The reap itself, read from the server source: SIGKILL to every process (-1), only
    when the server is the container's pid 1, after the normal return AND on a timeout."""
    from adk.skilltasks.jail import _SERVER

    assert "os.kill(-1, signal.SIGKILL)" in _SERVER
    assert 'req.get("k") and os.getpid() == 1' in _SERVER
    body = _SERVER.split("for line in sys.stdin:", 1)[1]
    assert body.count("reap(req)") == 2
    assert body.rindex("reap(req)") < body.index('sys.stdout.write(json.dumps({"id"')
    # sealed BEFORE the first request is read, and the reply echoes the request's nonce
    head = _SERVER.split("for line in sys.stdin:", 1)[0]
    assert "libc.prctl(4, z, z, z, z)" in head and '"nd": seal()' in head
    assert '"n": req.get("n")' in body


@needs_podman
def test_a_background_process_does_not_outlive_its_command_in_the_real_jail(tmp_path):
    env = _env(tmp_path, jail="podman")
    try:
        env.write("d.py", "import time\ntime.sleep(300)\n")
        first = env.sh("python d.py >/dev/null 2>&1 & sleep 1; pgrep -f 'd[.]py' || echo NONE")
        assert "NONE" not in first, first  # alive WITHIN the command that started it
        after = env.sh("pgrep -f 'd[.]py' || echo NONE")
        assert "NONE" in after, after  # killed when that command returned
    finally:
        env.close()


_FAKE_PODMAN_HUNG = r"""
import os, sys, time
if "rm" in sys.argv[1:2]:
    with open(os.environ["FAKE_RM_LOG"], "a", encoding="utf-8") as fh:
        fh.write(" ".join(sys.argv[1:]) + "\n")
    time.sleep(1.0)  # a slow store: close() must still WAIT for the removal
    with open(os.environ["FAKE_RM_LOG"], "a", encoding="utf-8") as fh:
        fh.write("removed\n")
    sys.exit(0)
sys.stdout.write('{"ready": 1, "uid": 1000, "w": true, "nd": true}\n')
sys.stdout.flush()
time.sleep(600)  # never answers, and ignores its stdin closing
"""


def test_a_jail_that_stopped_answering_is_removed_before_the_harness_goes_on(tmp_path, monkeypatch):
    import adk.skilltasks.jail as jail_mod

    script = tmp_path / "fake_podman_hung.py"
    script.write_text(_FAKE_PODMAN_HUNG, encoding="utf-8")
    log = tmp_path / "rm.log"
    monkeypatch.setenv("FAKE_RM_LOG", str(log))
    monkeypatch.setattr(jail_mod, "CLOSE_WAIT_S", 0.5)
    work = tmp_path / "work"
    work.mkdir()
    jail = jail_mod.Jail(work, [sys.executable, str(script)], reap=True)
    jail.start()
    name = jail.name
    jail.close()  # what run() does when the server stops answering
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines == ["rm -f " + name, "removed"], lines  # waited for, not fired and forgotten
    assert not jail.alive()


# ----------------------------------------------------------------- the verifier is jailed too
# tests/test.py is frozen task code, but it EXECUTES what the model left in the workspace
# (corpus: checker-exit-contract runs the rewritten checker, commit-only-mine runs git on a
# repository whose .git/config the model controls). In a jailed run that must not be a host
# process. This fake ``podman run`` answers for both containers and logs every request; it
# executes nothing, so anything that RAN on the host ran outside the jail.
_FAKE_PODMAN_BOTH = r"""
import base64, json, os, sys
verifier = any(a.endswith(":/private:rw") for a in sys.argv)
sys.stdout.write(os.environ["FAKE_VJAIL_READY" if verifier else "FAKE_JAIL_READY"] + "\n")
sys.stdout.flush()
for line in sys.stdin:
    req = json.loads(line)
    cmd = base64.b64decode(req["c"]).decode()
    with open(os.environ["FAKE_JAIL_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"jail": "verifier" if verifier else "shell", "cmd": cmd,
                             "cwd": req.get("d"), "reap": req.get("k"),
                             "nonce": req.get("n")}) + "\n")
    out = os.environ.get("FAKE_VERIFY_OUT", "") if verifier else "ran " + cmd
    sys.stdout.write(json.dumps({"id": req["id"], "n": req["n"], "rc": 0, "to": False,
                                 "o": base64.b64encode(out.encode()).decode(), "e": ""}) + "\n")
    sys.stdout.flush()
"""

# A verifier that runs a file from the workspace, as the corpus verifiers do.
_EXEC_TEST = """import json, subprocess, sys
from pathlib import Path
ws, priv = Path(sys.argv[1]), Path(sys.argv[2])
ran = False
if (ws / "hook.py").is_file():
    ran = subprocess.run([sys.executable, str(ws / "hook.py")], cwd=str(priv)).returncode == 0
print(json.dumps({"tests": {"wrote_out": ran}}))
"""
_READY_OK = '{"ready": 1, "uid": 1000, "w": true, "nd": true}'


def _hook(marker: Path) -> str:
    """Model-written code: leaves a marker wherever it really runs, and plants a file in
    its cwd (the verifier runs it in private/)."""
    return (
        "from pathlib import Path\n"
        "Path(%r).write_text('executed', encoding='utf-8')\n"
        "Path('planted').write_text('x', encoding='utf-8')\n" % str(marker)
    )


def _exec_env(tmp_path: Path, monkeypatch, *, jail: str, vready: str = _READY_OK, out: str = ""):
    import json

    import adk.skilltasks.jail as jail_mod

    script = tmp_path / "fake_podman_both.py"
    script.write_text(_FAKE_PODMAN_BOTH, encoding="utf-8")
    log = tmp_path / "jail.log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setenv("FAKE_JAIL_READY", _READY_OK)
    monkeypatch.setenv("FAKE_VJAIL_READY", vready)
    monkeypatch.setenv("FAKE_JAIL_LOG", str(log))
    monkeypatch.setenv("FAKE_VERIFY_OUT", out)
    monkeypatch.setattr(
        jail_mod, "probe_podman", lambda build=True: ([sys.executable, str(script)], "")
    )
    env = SkillTaskTerminalEnv(load_task(_make_task(tmp_path, "t", test=_EXEC_TEST)), jail=jail)

    def requests():
        return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()]

    return env, requests


def test_the_verifier_runs_in_the_jail_and_never_on_the_host(tmp_path, monkeypatch):
    marker = tmp_path / "ran-on-the-host"
    env, requests = _exec_env(
        tmp_path, monkeypatch, jail="podman", out='{"tests": {"wrote_out": true}}'
    )
    try:
        env.write("hook.py", _hook(marker))
        env.run_tests()
        assert env.sandbox == "podman" and env.verifier_sandbox == "podman"
        assert not marker.exists(), "the verifier executed model-written code on the host"
        assert env.tests == {"wrote_out": True}  # graded from what the JAILED verifier printed
        assert env.final().reward == 1.0 and not marker.exists()  # final() takes the same path
        env.submit()
        assert env.won and not marker.exists()
        sent = [r for r in requests() if r["jail"] == "verifier"]
        assert len(sent) == 3 and all(r["cmd"] == "python3 test.py /work /private" for r in sent)
        # in the tests directory, and nothing the verifier started outlives the run
        assert all(r["cwd"] == "/tests" and r["reap"] == 1 for r in sent)
        # the verifier's mounts exist ONLY in its own container: the shell jail keeps one
        assert env._jail is not None and env._vjail is not None
        argv = env._vjail.argv()
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
        assert [m.rsplit(":", 2)[1:] for m in mounts] == [
            ["/work", "rw"], ["/private", "rw"], ["/tests", "ro"],
        ]  # fmt: skip
        for flag in ("--network=none", "--read-only", "--cap-drop=ALL"):
            assert flag in argv, flag
        assert argv[argv.index("--user") + 1] == "1000:1000"
        assert "HOME=/tmp" in argv  # not the workspace: a planted ~/.gitconfig is not read
        assert sum(1 for a in env._jail.argv() if a == "-v") == 1
    finally:
        env.close()
    assert env._vjail is not None and not env._vjail.alive()


def test_in_policy_only_mode_the_same_verifier_runs_on_the_host_and_says_so(tmp_path, monkeypatch):
    """The control for the test above (same task, same hook, no jail): the hook DOES run on
    the host, so the marker is a real witness -- and the run is marked as such. What the
    hook planted in private/ while the verifier ran it is gone before the next verification."""
    marker = tmp_path / "ran-on-the-host"
    env, requests = _exec_env(tmp_path, monkeypatch, jail="off")
    try:
        env.write("hook.py", _hook(marker))
        env.run_tests()
        assert env.sandbox == POLICY_ONLY and env.verifier_sandbox == "host"
        assert marker.exists() and env.tests == {"wrote_out": True}
        assert requests() == []
        assert not (env.ws.private / "planted").exists()
        assert env.final().reward == 1.0 and not env.invalid  # not mistaken for tampering
        assert not (env.ws.private / "planted").exists()
    finally:
        env.close()


def test_a_required_jail_without_a_verifier_jail_is_refused(tmp_path, monkeypatch):
    from adk.skilltasks.task import TaskError

    marker = tmp_path / "ran-on-the-host"
    env, requests = _exec_env(
        tmp_path,
        monkeypatch,
        jail="podman",
        vready='{"ready": 1, "uid": 1000, "w": false, "nd": true}',
    )
    try:
        env.write("hook.py", _hook(marker))
        with pytest.raises(TaskError, match="required .* the verifier cannot run in one"):
            env.final()  # the first thing that needs the sandbox: refused, nothing verified
        assert not marker.exists()
        assert env._jail is None and env._vjail is None  # the shell jail is not left running
    finally:
        env.close()


def test_auto_mode_is_policy_only_when_the_verifier_jail_cannot_start(
    tmp_path, monkeypatch, caplog
):
    # a shell jail alone is not a jailed run: the report must not say "podman"
    env, requests = _exec_env(
        tmp_path, monkeypatch, jail="auto", vready='{"ready": 1, "uid": 0, "w": true, "nd": true}'
    )
    try:
        with caplog.at_level("WARNING"):
            assert env.jail_status() == POLICY_ONLY
        assert env.verifier_sandbox == "host" and env._jail is None and env._vjail is None
        assert "the verifier jail could not start" in env.jail_why
        assert POLICY_ONLY.upper() in caplog.text
    finally:
        env.close()


def test_a_jailed_verifier_that_does_not_answer_is_an_error_not_a_host_run(tmp_path, monkeypatch):
    from adk.skilltasks.task import TaskError

    marker = tmp_path / "ran-on-the-host"
    env, requests = _exec_env(tmp_path, monkeypatch, jail="podman", out="no json here")
    try:
        env.write("hook.py", _hook(marker))
        with pytest.raises(TaskError, match="printed no"):
            env.final()
        assert [r["jail"] for r in requests()] == ["verifier"] and not marker.exists()
    finally:
        env.close()


# A verifier that reports whether the workspace code it ran was confined.
_CONFINED_TEST = """import json, subprocess, sys
from pathlib import Path
ws = Path(sys.argv[1])
p = subprocess.run([sys.executable, str(ws / "hook.py")], cwd=str(ws),
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
try:
    open(str(Path(__file__).parent / "planted"), "w").close()
    tests_ro = False
except OSError:
    tests_ro = True
ok = tests_ro and "NO-NETWORK" in p.stdout and "WROTE" not in p.stdout
print(p.stdout + p.stderr)
print(json.dumps({"tests": {"wrote_out": ok}}))
"""


@needs_podman
def test_code_the_verifier_executes_is_confined_by_the_real_jail(tmp_path):
    """Real podman: the hook the verifier runs has no network, cannot write outside the
    mounts, and the frozen tests are read-only even to the verifier."""
    env = SkillTaskTerminalEnv(
        load_task(_make_task(tmp_path, "t", test=_CONFINED_TEST)), jail="podman"
    )
    try:
        env.write("hook.py", NET_PROBE + ESCAPE.replace('"..", "private", "pwned"', '"..", "x"'))
        res = env.final()
        assert env.verifier_sandbox == "podman"
        assert res.tests == {"wrote_out": True}, res.stdout_tail
        assert not (env.ws.run / "sibling.txt").exists() and not (env.ws.run / "x").exists()
        assert not (env.task.root / "tests" / "planted").exists()
    finally:
        env.close()


# ----------------------------------------------------------------- the reply channel
# "The command returned" is what lets the host harness touch the workspace, and the command
# runs as the same uid as the command server: a program the model wrote can try to print
# the reply itself (/proc/1/fd/1 is the server's stdout) while it keeps running.
_FAKE_PODMAN_FORGER = r"""
import base64, json, os, sys, time
if "rm" in sys.argv[1:2]:
    with open(os.environ["FAKE_RM_LOG"], "a", encoding="utf-8") as fh:
        fh.write(" ".join(sys.argv[1:]) + "\n")
    sys.exit(0)
sys.stdout.write('{"ready": 1, "uid": 1000, "w": true, "nd": true}\n')
sys.stdout.flush()
for line in sys.stdin:
    req = json.loads(line)
    # what the jailed command prints into the channel: every id it can guess, no nonce
    # (or a guessed one) -- while the real command is still running
    for n in range(1, 50):
        forged = {"id": n, "rc": 0, "to": False, "o": "", "e": ""}
        if os.environ.get("FAKE_FORGE_NONCE"):
            forged["n"] = os.environ["FAKE_FORGE_NONCE"]
        sys.stdout.write(json.dumps(forged) + "\n")
    sys.stdout.flush()
    time.sleep(600)  # the command is still alive; the real reply never comes
"""


@pytest.mark.parametrize("guess", ["", "0" * 32, "None"])
def test_a_reply_forged_from_inside_the_jail_is_not_the_end_of_the_command(
    tmp_path, monkeypatch, guess
):
    import time

    import adk.skilltasks.jail as jail_mod

    script = tmp_path / "fake_podman_forger.py"
    script.write_text(_FAKE_PODMAN_FORGER, encoding="utf-8")
    log = tmp_path / "rm.log"
    monkeypatch.setenv("FAKE_RM_LOG", str(log))
    monkeypatch.setenv("FAKE_FORGE_NONCE", guess)
    monkeypatch.setattr(jail_mod, "CLOSE_WAIT_S", 60.0)  # a forgery must not wait this out
    work = tmp_path / "work"
    work.mkdir()
    jail = jail_mod.Jail(work, [sys.executable, str(script)], reap=True)
    jail.start()
    name = jail.name
    t0 = time.monotonic()
    rc, out, err, _to = jail.run("python3 x.py", timeout=20)
    assert rc == 127 and out == "" and "forged" in err, (rc, out, err)
    # the jail was REMOVED (and that was waited for) before run() returned: nothing the
    # command left is alive when the harness next touches the workspace
    assert log.read_text(encoding="utf-8").splitlines() == ["rm -f " + name]
    assert not jail.alive() and time.monotonic() - t0 < 30


def test_every_request_carries_a_fresh_unguessable_nonce(tmp_path, monkeypatch):
    env, requests = _exec_env(tmp_path, monkeypatch, jail="podman")
    try:
        env.sh("echo one")
        env.sh("echo two")
        seen = [r["nonce"] for r in requests() if r["jail"] == "shell"]
    finally:
        env.close()
    assert len(seen) == 2 and seen[0] != seen[1]
    assert all(len(n) == 32 and int(n, 16) >= 0 for n in seen)  # 128 random bits each


def test_a_jail_whose_server_is_not_sealed_is_refused(tmp_path, monkeypatch):
    from adk.skilltasks.jail import JailUnavailableError

    for ready in ('{"ready": 1, "uid": 1000, "w": true}',
                  '{"ready": 1, "uid": 1000, "w": true, "nd": false}'):  # fmt: skip
        (tmp_path / "work").mkdir(exist_ok=True)
        script = tmp_path / "fake_podman.py"
        script.write_text(_FAKE_PODMAN, encoding="utf-8")
        monkeypatch.setenv("FAKE_JAIL_READY", ready)
        from adk.skilltasks.jail import Jail

        jail = Jail(tmp_path / "work", [sys.executable, str(script)])
        with pytest.raises(JailUnavailableError, match="could not seal itself"):
            jail.start()
        assert not jail.alive()


def _can_run_the_real_server() -> bool:
    return (
        sys.platform.startswith("linux")
        and hasattr(os, "getuid")
        and os.getuid() != 0  # root holds CAP_SYS_PTRACE: the kernel check does not apply
        and shutil.which("bash") is not None
    )


@pytest.mark.skipif(not _can_run_the_real_server(), reason="needs Linux, non-root, bash")
def test_the_real_command_server_cannot_be_written_or_read_by_its_own_command(tmp_path):
    """The REAL server on a real kernel (no container needed: the check is per process).
    A command of the same uid tries exactly what the forgery needs -- open the server's
    stdout, read its memory -- and the kernel refuses both; the true reply still comes."""
    import base64
    import json
    import subprocess

    from adk.skilltasks.jail import _SERVER

    srv = subprocess.Popen(
        [sys.executable, "-u", "-c", _SERVER],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=str(tmp_path),
        env=dict(os.environ, ADK_JAIL_RW=str(tmp_path)),
    )  # fmt: skip
    try:
        assert srv.stdin is not None and srv.stdout is not None
        ready = json.loads(srv.stdout.readline())
        assert ready["ready"] == 1 and ready["nd"] is True, ready
        probe = (
            "import json\n"
            "for what, mode in (('fd/1', 'w'), ('mem', 'rb'), ('fd/0', 'r')):\n"
            "    try:\n"
            "        fh = open('/proc/%d/' + what, mode)\n"
            "        if what == 'fd/1':\n"
            "            fh.write(json.dumps({'id': 1, 'rc': 0, 'o': '', 'e': ''}) + chr(10))\n"
            "        fh.close()\n"
            "        print('OPENED', what)\n"
            "    except OSError as exc:\n"
            "        print('REFUSED', what, type(exc).__name__)\n" % srv.pid
        )
        (tmp_path / "x.py").write_text(probe, encoding="utf-8")
        cmd = "%s x.py" % sys.executable
        req = {"id": 1, "n": "secret-nonce", "t": 30, "d": str(tmp_path),
               "c": base64.b64encode(cmd.encode()).decode()}  # fmt: skip
        srv.stdin.write(json.dumps(req) + "\n")
        srv.stdin.flush()
        rep = json.loads(srv.stdout.readline())  # the FIRST line is the server's own
        out = base64.b64decode(rep["o"]).decode()
        assert rep["n"] == "secret-nonce" and rep["rc"] == 0, rep
        assert "OPENED" not in out and out.count("REFUSED") == 3, out
        assert "secret-nonce" not in out
    finally:
        if srv.stdin is not None:
            srv.stdin.close()
        srv.wait(timeout=30)


# ----------------------------------------------------------------- hardening the hand-over
# The harness is ROOT and re-owns a tree the model wrote before every command. These pin
# what keeps that safe and bounded, and what the model is (not) told when it fails.
_ODD_UID = 0x40000000 + 1000  # the jail user of the first id block: nobody's account
needs_posix = pytest.mark.skipif(
    os.name == "nt", reason="the ownership hand-over is POSIX only (Windows has no chown)"
)


def _owner(path) -> int:
    return os.lstat(str(path)).st_uid


@needs_rootful
def test_the_hand_over_reowns_the_tree_and_nothing_outside_or_shared(tmp_path):
    """Everything IN the workspace goes to the jail uid -- links and special files
    themselves, never their targets -- and a file with a second hard link is left alone
    (its other name may live outside the workspace)."""
    import adk.skilltasks.jail as jail_mod

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("s", encoding="utf-8")
    (outside / "shared").write_text("h", encoding="utf-8")
    work = tmp_path / "work"
    (work / "sub" / "deep").mkdir(parents=True)
    (work / "a.txt").write_text("a", encoding="utf-8")
    (work / "sub" / "deep" / "b.txt").write_text("b", encoding="utf-8")
    os.symlink(str(outside / "secret"), str(work / "lnk"))
    os.symlink(str(outside), str(work / "dlnk"))
    os.link(str(outside / "shared"), str(work / "hard"))
    os.mkfifo(str(work / "fifo"))

    jail_mod._give_to(work, _ODD_UID)

    mine = ("", "sub", "sub/deep", "a.txt", "sub/deep/b.txt", "lnk", "dlnk", "fifo")
    assert {rel: _owner(work / rel) for rel in mine} == {rel: _ODD_UID for rel in mine}
    assert _owner(work / "hard") == 0  # two names for one inode: not the workspace's alone
    assert [_owner(p) for p in (outside, outside / "secret", outside / "shared")] == [0, 0, 0]


def _can_mount_privately() -> bool:
    """Root, with ``unshare`` and ``mount``, and a tmpfs really mounts in a private mount
    namespace here (it may not in a container without CAP_SYS_ADMIN)."""
    import subprocess
    import tempfile

    if not _root_on_posix() or not shutil.which("unshare") or not shutil.which("mount"):
        return False
    spot = tempfile.mkdtemp(prefix="adk-mnt-probe-")
    try:
        return (
            subprocess.run(
                [
                    "unshare",
                    "-m",
                    "--propagation",
                    "private",
                    "mount",
                    "-t",
                    "tmpfs",
                    "tmpfs",
                    spot,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        shutil.rmtree(spot, ignore_errors=True)


@pytest.mark.skipif(
    not _can_mount_privately(),
    reason="needs root and a tmpfs mounted in a private mount namespace (unshare -m)",
)
def test_the_hand_over_stops_at_a_mount_point(tmp_path):
    """Another filesystem mounted inside the workspace is not the workspace's own. The
    mount lives in a private mount namespace: it never exists on the host."""
    import subprocess

    import adk.skilltasks.jail as jail_mod

    work = tmp_path / "work"
    (work / "mnt").mkdir(parents=True)
    (work / "plain").write_text("x", encoding="utf-8")
    code = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "import adk.skilltasks.jail as j\n"
        "w = sys.argv[1]\n"
        "open(w + '/mnt/inner', 'w').close()\n"
        "j._give_to(Path(w), %d)\n"
        "print(*(os.lstat(w + p).st_uid for p in ('/plain', '/mnt', '/mnt/inner')))\n" % _ODD_UID
    )
    shell = 'mount -t tmpfs tmpfs "$1/mnt" && exec "$2" -c "$3" "$1"'
    proc = subprocess.run(
        ["unshare", "-m", "--propagation", "private", "sh", "-c", shell, "sh",
         str(work), sys.executable, code],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        cwd=str(Path(jail_mod.__file__).resolve().parents[2]),
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == [str(_ODD_UID), "0", "0"], proc.stdout
    assert not (work / "mnt" / "inner").exists()  # the mount never existed out here


@needs_rootful
def test_a_tree_that_changes_under_the_hand_over_is_never_followed_out(tmp_path, monkeypatch):
    """The walk resolves nothing twice. A directory swapped for a link between listing it
    and entering it is not entered; a file swapped for a hard link to an outside inode
    between its stat and its chown is judged again on the descriptor and left alone."""
    import adk.skilltasks.jail as jail_mod

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("s", encoding="utf-8")
    (outside / "shared").write_text("h", encoding="utf-8")
    work = tmp_path / "work"
    (work / "sub").mkdir(parents=True)
    (work / "sub" / "inner.txt").write_text("i", encoding="utf-8")
    (work / "a.txt").write_text("a", encoding="utf-8")
    real_open, swapped = os.open, []

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None and path == "sub" and "sub" not in swapped:
            swapped.append("sub")  # the model's process wins the race, every time
            os.rename(str(work / "sub"), str(work / "sub.real"))
            os.symlink(str(outside), str(work / "sub"))
        if dir_fd is not None and path == "a.txt" and "a.txt" not in swapped:
            swapped.append("a.txt")
            os.unlink(str(work / "a.txt"))
            os.link(str(outside / "shared"), str(work / "a.txt"))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(jail_mod.os, "open", racing_open)
    jail_mod._give_to(work, _ODD_UID)
    monkeypatch.undo()

    assert sorted(swapped) == ["a.txt", "sub"]  # both races really happened
    assert [_owner(p) for p in (outside, outside / "secret", outside / "shared")] == [0, 0, 0]
    assert _owner(work) == _ODD_UID


@needs_posix
def test_a_workspace_past_the_bound_ends_the_hand_over_cleanly(tmp_path, monkeypatch):
    """Too deep or too large is a caught JailBrokenError -- never a RecursionError (os.walk
    and os.fwalk recurse on Python 3.10/3.11), never an unbounded walk before each command
    -- and no directory descriptor is left open."""
    import adk.skilltasks.jail as jail_mod

    me = os.getuid()  # nothing to chown: the bound is about the walk, so any uid can run it
    fds = "/proc/self/fd"
    before = len(os.listdir(fds)) if os.path.isdir(fds) else None

    deep = tmp_path / "deep"
    os.makedirs(str(deep) + "/d" * 300)
    with pytest.raises(jail_mod.JailBrokenError, match="directories deep"):
        jail_mod._give_to(deep, me)

    wide = tmp_path / "wide"
    wide.mkdir()
    for n in range(50):
        (wide / ("f%d" % n)).write_text("", encoding="utf-8")
    jail_mod._give_to(wide, me)  # within the bound: fine
    monkeypatch.setattr(jail_mod, "MAX_OWN_ENTRIES", 20)
    with pytest.raises(jail_mod.JailBrokenError, match="more than 20 entries") as exc:
        jail_mod._give_to(wide, me)
    assert str(tmp_path) not in str(exc.value)  # this text reaches the model

    if before is not None:
        assert len(os.listdir(fds)) == before


def _mapped(monkeypatch, give_to=None) -> int:
    """Make the jails the env opens MAPPED ones (as under rootful podman) on any host."""
    import adk.skilltasks.jail as jail_mod

    base = 0x40000000 + 3 * jail_mod.IDMAP_SIZE
    monkeypatch.setattr(jail_mod, "rootful_here", lambda: True)
    monkeypatch.setattr(jail_mod, "pick_idmap_base", lambda: base)
    monkeypatch.setattr(jail_mod, "_give_to", give_to or (lambda root, uid: None))
    return base


def test_a_workspace_that_cannot_be_handed_over_ends_the_episode_loudly(
    tmp_path, monkeypatch, caplog
):
    import adk.skilltasks.jail as jail_mod
    from adk.skilltasks.task import TaskError

    state = {"fail": False, "calls": 0}

    def give_to(root, uid):
        state["calls"] += 1
        if state["fail"]:
            raise jail_mod.JailBrokenError("the workspace holds more than 5 entries; too large")

    _mapped(monkeypatch, give_to)
    env, _requests = _exec_env(tmp_path, monkeypatch, jail="podman")
    try:
        env.sh("echo one")
        assert state["calls"] >= 2 and not env.invalid
        state["fail"] = True
        with caplog.at_level("ERROR"):
            out = env.sh("echo two")
        assert out.startswith("ERROR") and "more than 5 entries" in out
        assert "the jail failed" in env.invalid and env.over  # over, and never scored
        assert env._jail is not None and env._jail.broken and not env._jail.alive()
        assert "cannot go on" in caplog.text  # loud on the host too
        calls = state["calls"]
        env.sh("echo three")
        assert state["calls"] == calls  # the workspace is not walked again
        with pytest.raises(TaskError, match="invalid"):
            env.final()
    finally:
        env.close()


# ``podman run`` that forges a reply and stays alive, and whose container CANNOT be removed:
# ``rm -f`` fails, and ``container exists`` answers FAKE_EXISTS_RC (0 = still there).
_FAKE_PODMAN_UNREMOVABLE = r"""
import json, os, sys, time
if sys.argv[1:2] == ["rm"]:
    with open(os.environ["FAKE_RM_LOG"], "a", encoding="utf-8") as fh:
        fh.write(" ".join(sys.argv[1:]) + "\n")
    sys.exit(1)
if sys.argv[1:3] == ["container", "exists"]:
    sys.exit(int(os.environ["FAKE_EXISTS_RC"]))
sys.stdout.write('{"ready": 1, "uid": 1000, "w": true, "nd": true}\n')
sys.stdout.flush()
for line in sys.stdin:
    req = json.loads(line)
    sys.stdout.write(json.dumps({"id": req["id"], "rc": 0, "to": False, "o": "", "e": ""}) + "\n")
    sys.stdout.flush()
    time.sleep(600)
"""


@pytest.mark.parametrize("exists_rc, fatal", [(0, True), (125, True), (1, False)])
def test_a_jail_that_cannot_be_removed_is_fatal_for_the_episode(
    tmp_path, monkeypatch, caplog, exists_rc, fatal
):
    """``podman rm -f`` failed. Unless podman then says the container does not exist, a
    command may still be running in the workspace: no restart, no further root chown by
    name, the episode is over. (It used to be one log line, and the next command went on.)"""
    import adk.skilltasks.jail as jail_mod

    script = tmp_path / "fake_podman_unremovable.py"
    script.write_text(_FAKE_PODMAN_UNREMOVABLE, encoding="utf-8")
    log = tmp_path / "rm.log"
    monkeypatch.setenv("FAKE_RM_LOG", str(log))
    monkeypatch.setenv("FAKE_EXISTS_RC", str(exists_rc))
    handed = []
    monkeypatch.setattr(jail_mod, "_give_to", lambda root, uid: handed.append(uid))
    work = tmp_path / "work"
    work.mkdir()
    jail = jail_mod.Jail(work, [sys.executable, str(script)], reap=True, idmap_base=0x40000000)
    jail.start()
    name = jail.name
    try:
        if not fatal:  # rm failed because it was already gone: an ordinary forgery
            rc, _out, err, _to = jail.run("python3 x.py", timeout=20)
            assert rc == 127 and "forged" in err and not jail.broken
            return
        with caplog.at_level("ERROR"), pytest.raises(jail_mod.JailBrokenError) as exc:
            jail.run("python3 x.py", timeout=20)
        assert "could not be removed" in str(exc.value) and jail.broken
        assert name in caplog.text and name not in str(exc.value)
        assert log.read_text(encoding="utf-8").splitlines() == ["rm -f " + name]
        calls = len(handed)
        for _ in range(2):  # no restart and no chown, ever again
            with pytest.raises(jail_mod.JailBrokenError):
                jail.run("echo next", timeout=20)
        with pytest.raises(jail_mod.JailBrokenError):
            jail.start()
        assert len(handed) == calls and jail.starts == 1 and not jail.alive()
    finally:
        jail.close()


def test_an_unremovable_jail_invalidates_the_episode_and_its_workspace_is_left_alone(
    tmp_path, monkeypatch
):
    from adk.skilltasks.task import TaskError

    _mapped(monkeypatch)
    env, _requests = _exec_env(tmp_path, monkeypatch, jail="podman")
    try:
        env.sh("echo one")
        assert env._jail is not None and not env.invalid
        env._jail.broken = "the jail container could not be removed"  # what close() sets
        restored = []
        monkeypatch.setattr(env._private_snap, "restore", lambda root: restored.append(root) or [])
        out = env.sh("echo two")
        assert out.startswith("ERROR") and "could not be removed" in out
        assert "the jail failed" in env.invalid and env.over
        assert env._jail_broken()
        try:  # the verifier's restore would write private/ by name: it must be skipped
            env._run_verifier()
        except TaskError as exc:
            assert "could not be restored" not in str(exc)
        assert restored == []
    finally:
        env.close()


def test_reserved_id_ranges_come_from_subid_files_and_live_user_namespaces(tmp_path):
    from adk.skilltasks.jail import _reserved_id_ranges

    subuid = tmp_path / "subuid"
    subuid.write_text("alice:100000:65536\n# a comment\nnot-a-range\n", encoding="utf-8")
    subgid = tmp_path / "subgid"
    subgid.write_text("alice:200000:1000\n", encoding="utf-8")
    proc = tmp_path / "proc"
    for pid, uid_map, gid_map in (
        ("1", "         0          0 4294967295\n", "         0          0 4294967295\n"),
        ("77", "         0 1073741824      65536\n", "0 5000 10\n1000 7000 1\n"),
        ("78", "         0 1073741824      65536\n", "garbage\n"),
    ):
        (proc / pid).mkdir(parents=True)
        (proc / pid / "uid_map").write_text(uid_map, encoding="ascii")
        (proc / pid / "gid_map").write_text(gid_map, encoding="ascii")
    (proc / "self").mkdir()  # not a pid
    (proc / "99").mkdir()  # a process that ended: no map to read
    got = _reserved_id_ranges((str(subuid), str(subgid), str(tmp_path / "absent")), str(proc))
    assert sorted(set(got)) == [
        (5000, 10),
        (7000, 1),
        (100000, 65536),
        (200000, 1000),
        (1073741824, 65536),
    ]  # ... and not the initial namespace's identity map


@needs_posix
def test_the_id_block_avoids_reserved_ranges(monkeypatch):
    """Not a block /etc/subuid or /etc/subgid delegates, nor one a running container maps
    (only "no account at block + 1000" was checked)."""
    import adk.skilltasks.jail as jail_mod

    floor, size = jail_mod._IDMAP_FLOOR, jail_mod.IDMAP_SIZE
    draws = iter(range(1000))
    monkeypatch.setattr(jail_mod.secrets, "randbelow", lambda n: next(draws))
    # blocks 0..2 delegated, block 3 touched by ten ids of a live user namespace
    monkeypatch.setattr(
        jail_mod, "_reserved_id_ranges", lambda: [(floor, 3 * size), (floor + 4 * size - 10, 10)]
    )
    assert jail_mod.pick_idmap_base() == floor + 4 * size
    monkeypatch.setattr(jail_mod, "_reserved_id_ranges", lambda: [(0, 2**32)])
    with pytest.raises(jail_mod.JailUnavailableError, match="no free host id block"):
        jail_mod.pick_idmap_base()


_SEAL_PROBE = """import ctypes, os
print("uid", os.getuid(), "map", open("/proc/self/uid_map").read().split())
for what, mode in (("fd/1", "w"), ("fd/0", "r"), ("mem", "rb"), ("environ", "rb")):
    try:
        open("/proc/1/" + what, mode).close()
        print("OPENED", what)
    except OSError as exc:
        print("REFUSED", what, exc.errno)
try:
    os.listdir("/proc/1/fd")
    print("OPENED fd-listing")
except OSError as exc:
    print("REFUSED fd-listing", exc.errno)
libc = ctypes.CDLL(None, use_errno=True)
print("ATTACHED" if libc.ptrace(16, 1, 0, 0) == 0 else "REFUSED ptrace")
"""


@needs_podman
@needs_rootful
def test_the_mapped_jail_seals_its_server_against_its_own_commands(tmp_path, monkeypatch):
    """Real rootful podman, the jail in its own user namespace: the server reports itself
    sealed, and a command running as uid 1000 INSIDE cannot open the server's reply
    channel, stdin, memory or environment, nor ptrace it. (The seal test on the bare
    server needs a non-root harness, so it never ran in this configuration.)"""
    import adk.skilltasks.jail as jail_mod

    readies, real_next = [], jail_mod.Jail._next

    def spy(self, timeout):
        rep = real_next(self, timeout)
        if isinstance(rep, dict) and "ready" in rep:
            readies.append(rep)
        return rep

    monkeypatch.setattr(jail_mod.Jail, "_next", spy)
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    (work / "probe.py").write_text(_SEAL_PROBE, encoding="utf-8")
    jail, why = jail_mod.open_jail(work, "podman")
    assert jail is not None and why == ""
    try:
        assert jail.idmap_base is not None  # the mapped configuration
        assert readies and readies[0]["nd"] is True and readies[0]["uid"] == 1000, readies
        rc, out, err, _to = jail.run("python3 probe.py")
        assert rc == 0, (out, err)
        first = out.splitlines()[0]
        assert first == "uid 1000 map ['0', '%d', '%d']" % (jail.idmap_base, jail_mod.IDMAP_SIZE)
        assert "OPENED" not in out and "ATTACHED" not in out, out
        assert out.count("REFUSED") == 6, out
        assert jail.alive() and jail.run("echo still-mine")[1] == "still-mine\n"
    finally:
        jail.close()


@pytest.mark.parametrize("mode", ["auto", "podman"])
def test_the_model_is_never_told_host_ids_or_paths_when_the_jail_is_refused(
    tmp_path, monkeypatch, caplog, mode
):
    """What a refused jail says to the MODEL (a tool error) and to the run report
    (``sandbox_why``) carries no host uid, id block, mode or host path. The host log does."""
    from adk.skilltasks.task import TaskError

    base = _mapped(monkeypatch)
    _podman_that_cannot_start(
        tmp_path, monkeypatch, '{"ready": 1, "uid": 1000, "w": false, "nd": true}'
    )
    env = _env(tmp_path / "t", jail=mode)
    try:
        with caplog.at_level("WARNING"):
            if mode == "podman":
                with pytest.raises(TaskError) as exc:
                    env.jail_status()
                told = str(exc.value)
            else:
                assert env.jail_status() == POLICY_ONLY
                told = env.jail_why + "\n" + env.sh("echo hi")
        assert "cannot write the workspace" in told
        secrets_ = [str(env.root), str(tmp_path), str(base), str(base + 1000), "host uid", "mode 0"]
        for secret in secrets_:
            assert secret not in told, (secret, told)
        assert str(env.root) in caplog.text and "host uid %d" % (base + 1000) in caplog.text
    finally:
        env.close()
