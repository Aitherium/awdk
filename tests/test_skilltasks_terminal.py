"""adk.skilltasks.terminal: the terminal-task Environment for adk.reasoning.solve.

* the adapter conforms (Environment protocol + every HOOK_ARGS arity, probed);
* every action goes through permits(): writing outside the workspace, reading through
  ``..``, shell paths outside it and network commands are DENIED and change nothing;
* the do-nothing and the reference policies score 0.0 and 1.0 through the adapter,
  as the acceptance gate says they must;
* a change hypothesis is verified by actually running the tests and rolled back; only a
  verified change may be applied (a context rule);
* the reasoning loop plays it end to end in sase (hypothesis booked, prediction scored,
  evidence table built) and plain mode, with a scripted model.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import List

import pytest
from adk.skilltasks import gate_task, load_task
from adk.skilltasks.gate import _make_task
from adk.skilltasks.terminal import (
    ACTION_NAMES,
    READ,
    TERMINAL_STRATEGIES,
    SkillTaskTerminalEnv,
    do_nothing_policy,
    reference_policy,
    run_policy,
    scripted_baseline_policy,
    shell_problems,
)

CORPUS = Path(__file__).resolve().parents[2] / "AitherOS" / "lib" / "training" / "skilltasks"


@pytest.fixture(autouse=True)
def _policy_mode(monkeypatch):
    # these tests pin the POLICY layer; the jail has its own (test_skilltasks_stall_and_jail)
    monkeypatch.setenv("ADK_SKILLTASK_JAIL", "off")


def _env(tmp_path: Path, **kw) -> SkillTaskTerminalEnv:
    return SkillTaskTerminalEnv(load_task(_make_task(tmp_path, "t")), **kw)


def test_adapter_conforms_to_the_environment_protocol_and_hook_args(tmp_path):
    from adk.reasoning.solve._types import HOOK_ARGS
    from adk.reasoning.solve.conformance import check_environment

    env = _env(tmp_path)
    try:
        assert check_environment(env, probe=True) == []
        for hook in (
            "primer",
            "render",
            "describe",
            "tools",
            "candidates",
            "state_key",
            "auto_action",
            "needs_xy",
            "significant_change",
            "handoff",
        ):
            assert hook in HOOK_ARGS and callable(getattr(env, hook))
        assert env.available_actions() == sorted(ACTION_NAMES)
        obs = env.observe()
        assert obs.state.shape[0] == 5 and str(obs.state.dtype) == "int8"
        assert set(env.tools(None)) >= {
            "sh",
            "read",
            "write",
            "patch",
            "run_tests",
            "try_change",
            "apply_change",
            "submit",
        }
    finally:
        env.close()


@pytest.mark.parametrize(
    "call",
    [
        lambda e: e.write("../escaped.txt", "x"),
        lambda e: e.write(str(Path(e.root).parent / "private" / "state.json"), "x"),
        lambda e: e.read("../private"),
        lambda e: e.patch("../x", "a", "b"),
        lambda e: e.sh("echo pwned > ../escaped.txt"),
        lambda e: e.sh("cat /etc/passwd"),
        lambda e: e.sh("ls ~"),
        lambda e: e.sh("cp seed.txt $HOME/x"),
        lambda e: e.sh("curl -s https://example.com"),
        lambda e: e.sh("git clone https://example.com/r.git"),
        lambda e: e.sh("pip install requests"),
        lambda e: e.sh("python -c \"import socket; socket.create_connection(('1.1.1.1', 53))\""),
        lambda e: e.write("net.py", "import urllib.request\nurllib.request.urlopen('http://x')\n"),
        lambda e: e.try_change(
            "sneak", [("write", "../escaped.txt", "x")], makes_pass=["wrote_out"]
        ),
    ],
)
def test_permits_blocks_escape_and_network(tmp_path, call):
    env = _env(tmp_path)
    try:
        outside = Path(env.root).parent / "escaped.txt"
        before = env._digests()
        out = call(env)
        assert out.startswith("DENIED by permits()"), out
        assert env.denials and env.last.exit_class == 3
        assert env._digests() == before and not outside.exists()
    finally:
        env.close()


def test_shell_policy_allows_ordinary_workspace_commands(tmp_path):
    root = tmp_path
    for cmd in (
        "git -C repo status --short",
        "ls -la ./sub",
        "sed -i 's/a/b/' f.txt",
        "python check.py --root dossiers 2>/dev/null",
        "git log --oneline HEAD~1..HEAD",
        "cat a/../b.txt",
    ):
        assert shell_problems(cmd, root) == [], cmd


def test_do_nothing_scores_zero_and_reference_scores_one_as_the_gate_says(tmp_path):
    task_dir = _make_task(tmp_path, "t")
    rep = gate_task(task_dir)
    assert rep.accepted and rep.oracle["reward"] == 1.0 and rep.nop["reward"] == 0.0
    for policy, want in (
        (do_nothing_policy, 0.0),
        (scripted_baseline_policy, 0.0),
        (reference_policy, 1.0),
    ):
        env = SkillTaskTerminalEnv(load_task(task_dir))
        try:
            assert run_policy(env, policy).reward == want, policy.__name__
            assert env.denials == []
        finally:
            env.close()


@pytest.mark.skipif(
    not CORPUS.is_dir(), reason="the AitherOS skill-task corpus is not in this checkout"
)
@pytest.mark.parametrize(
    "name", ["checker-exit-contract", "commit-only-mine", "dns-policy-inplace"]
)
@pytest.mark.skipif(shutil.which("git") is None, reason="commit-only-mine needs git")
def test_corpus_nop_zero_reference_one_through_the_adapter(name):
    task = load_task(CORPUS / name)
    for policy, want in ((do_nothing_policy, 0.0), (reference_policy, 1.0)):
        env = SkillTaskTerminalEnv(task)
        try:
            assert run_policy(env, policy).reward == want, (name, policy.__name__)
            assert env.denials == []
        finally:
            env.close()


def test_try_change_is_verified_by_the_tests_and_rolled_back(tmp_path):
    env = _env(tmp_path)
    try:
        ino = os.stat(env.root / "seed.txt").st_ino
        out = env.apply_change("fix")
        assert out.startswith("DENIED") and "not verified" in out  # nothing verified yet
        out = env.try_change("bad", [("write", "out.txt", "41\n")], makes_pass=["wrote_out"])
        assert "REFUTED" in out and not (env.root / "out.txt").exists()
        assert env.apply_change("bad").startswith("DENIED")
        out = env.try_change("fix", [("write", "out.txt", "42\n")], makes_pass=["wrote_out"])
        assert "VERIFIED" in out and not (env.root / "out.txt").exists()  # rolled back
        assert env.final().reward == 0.0
        assert "APPLIED" in env.apply_change("fix")
        assert env.final().reward == 1.0
        assert os.stat(env.root / "seed.txt").st_ino == ino
        assert "accepted" in env.submit() and env.done() and env.solved_by() == "policy"
    finally:
        env.close()


def test_writes_are_in_place_and_the_state_tracks_files_and_tests(tmp_path):
    env = _env(tmp_path)
    try:
        s0 = env.observe().state.copy()
        ino = os.stat(env.root / "seed.txt").st_ino
        env.read("seed.txt")
        assert not env.significant_change(s0, env.observe().state)  # a read changes nothing
        env.write("seed.txt", "seed v2\n")
        assert os.stat(env.root / "seed.txt").st_ino == ino
        assert env.significant_change(s0, env.observe().state)  # a file digest moved
        env.run_tests()
        s1 = env.observe().state
        assert env.significant_change(s0, s1) and s1[0][1] == 1  # tests now known
        assert env.patch("seed.txt", "nope", "x").startswith("ERROR")
    finally:
        env.close()


# ----------------------------------------------------------------- the loop, end to end
np = pytest.importorskip("numpy")

SASE_REPLY = """SITUATION: out.txt is missing; wrote_out fails.
ANALYSIS: none
SYNTHESIS:
```python
try_change("fix_out", [("write", "out.txt", "42\\n")], makes_pass=["wrote_out"], note="E1")
```
EXECUTION:
```python
apply_change("fix_out")
run_tests(expect_pass=["wrote_out", "kept_seed"])
submit()
```
"""

PLAIN_REPLY = """Write the file and submit.
```python
write("out.txt", "42\\n")
run_tests()
submit()
```
"""


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


@pytest.mark.parametrize("sase", [True, False])
def test_the_loop_solves_a_task_through_the_adapter(tmp_path, sase):
    from adk.reasoning.solve import Budget, LoopConfig, solve

    env = _env(tmp_path)
    model = _Scripted([SASE_REPLY if sase else PLAIN_REPLY])
    cfg = LoopConfig(
        budget=Budget(max_llm_calls=3, max_actions=20, turn_s=60.0),
        sase=sase,
        prism=sase,
        grounded=sase,
        planning=False,
        daydream=False,
    )
    try:
        res = asyncio.run(solve(env, model, config=cfg))
        assert env.final().reward == 1.0 and env.won
        assert res.won and res.finish_reason == "won" and res.llm_calls == 1
        msgs = model.calls[0]
        system, user = _content(msgs[0]), _content(msgs[-1])
        assert "TERMINAL TASK" in system and "act(a, x=None" not in system
        assert "RUBRIC Must-do" in user and "WORKSPACE FILES" in user
        if sase:
            hyp = {h.name: h for h in res.hypotheses}
            assert hyp["fix_out"].kind == "change" and hyp["fix_out"].status == "active"
            assert res.stats["predictions"]["made"] >= 2 and res.stats["predictions"]["hits"] >= 2
            assert "STRATEGY: test-first" in user  # PRISM rule_first, shown as the terminal arm
            assert env.solved_by() == "test-first"
        else:
            assert env.solved_by() == "plain"
    finally:
        env.close()


def test_strategy_map_covers_every_prism_arm():
    from adk.reasoning.solve._vendor.prism import BY_ID

    assert set(TERMINAL_STRATEGIES) == set(BY_ID)
    names = {s.name for s in TERMINAL_STRATEGIES.values()}
    assert {"test-first", "read-first", "minimal-patch", "scripted-baseline"} <= names


def test_evidence_table_is_command_to_effect(tmp_path):
    from adk.reasoning.solve._vendor.evidence import EvidenceTable
    from adk.reasoning.solve._vendor.memory import Episodic

    env = _env(tmp_path)
    try:
        hist = Episodic()
        for call in (
            lambda: env.run_tests(),
            lambda: env.write("out.txt", "42\n"),
            lambda: env.run_tests(),
            lambda: env.read("seed.txt"),
        ):
            b = env.observe().state.copy()
            call()
            k = len(env.steps) - 1
            hist.add(0, (env.steps[k].aid, k, -1), b, env.observe().state)
        table = EvidenceTable().build(
            hist, changed_fn=env.significant_change, family_fn=env.family, effects_fn=env.effects
        )
        text = table.render()
        assert "write:out.txt" in text and "file:out.txt" in text
        assert "test after write:out.txt" in text and "test:wrote_out moved (+1,+0)" in text
        assert any(  # a read's row carries the content hash it observed
            r.family.startswith("read:seed.txt@") and r.changed == 0 for r in table.rows.values()
        )
        assert env.steps[-1].aid == READ
    finally:
        env.close()


def test_scripted_arm_output_reaches_the_model_and_fences_are_closed(tmp_path):
    from adk.reasoning.solve import Budget, LoopConfig, solve

    env = _env(tmp_path)
    # an unterminated block (measured on bonsai2-27b): repaired, so the call still acts
    model = _Scripted(
        ["SITUATION: x\nANALYSIS: none\nSYNTHESIS: none\nEXECUTION:\n```python\nrun_tests()\n"]
    )
    cfg = LoopConfig(
        budget=Budget(max_llm_calls=2, max_actions=20, turn_s=60.0),
        sase=True,
        prism=True,
        grounded=True,
        planning=False,
        daydream=False,
    )
    try:
        asyncio.run(solve(env, model, config=cfg))
        assert env.fence_repairs >= 1
        assert any(
            s["family"].startswith("test") and s["strategy"] == "test-first" for s in env.trace()
        )
        env.render(env.observe(), None)
        env.write("extra.txt", "hello")
        env.render(env.observe(), None)
        env.loop = object()  # as if bound: a non-model source is the scripted baseline
        env.act(env.auto_action(), source="explore")  # the scripted baseline reads the new file
        env.loop = None
        assert env.trace()[-1]["strategy"] == "scripted-baseline"
        text = env.render(env.observe(), None)
        assert "ACTIONS TAKEN WITHOUT YOU" in text and "[read:extra.txt@" in text
        assert "] hello" in text  # the family carries the content hash it read
        assert "ACTIONS TAKEN WITHOUT YOU" not in env.render(env.observe(), None)  # shown once
    finally:
        env.close()


@pytest.mark.skipif(
    not CORPUS.is_dir(), reason="the AitherOS skill-task corpus is not in this checkout"
)
@pytest.mark.skipif(shutil.which("git") is None, reason="commit-only-mine needs git")
def test_a_tried_git_commit_never_survives_the_rollback():
    """Measured: git makes its objects read-only on Windows; the first rollback died on
    the unlink and a TRIED commit stayed in the repository (a free 1.0)."""
    env = SkillTaskTerminalEnv(load_task(CORPUS / "commit-only-mine"))
    try:
        head = env.sh("git -C repo rev-parse HEAD")
        cmd = "git -C repo commit -q -m 'docs(mine): add second note' -- mine/notes.md"
        out = env.try_change("commit", [("sh", cmd)], makes_pass=["one_commit_on_base"])
        assert "TRY commit" in out, out
        assert env.sh("git -C repo rev-parse HEAD") == head
        assert env.rollback_failures == 0
        assert env.final().reward == 0.0
    finally:
        env.close()


def test_strict_backend_queues_on_a_busy_pool_and_refuses_a_cross_model_reply(monkeypatch):
    from adk.core.backends import microscheduler as ms
    from adk.skilltasks import run as run_mod

    replies = [
        ms.SchedulerUnavailableError("returned 502 [error: Backend pool failed: timed out]", 502),
        ms.SchedulerUnavailableError("MicroScheduler at x unreachable: ConnectError"),
        {
            "model": "m1",
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "aither_route": {"served_by": "m1"},
        },
        {"model": "m2", "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]},
        ms.SchedulerUnavailableError("returned 500: boom", 500),
    ]

    def fake_request(self, method, path, **kw):
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(ms.MicroSchedulerBackend, "_request", fake_request)
    monkeypatch.setattr(run_mod.time, "sleep", lambda s: None)
    b = run_mod._strict_backend("https://127.0.0.1:1", "m1")
    assert b.chat([{"role": "user", "content": "x"}]).content == "hi"
    st = b.stats()
    assert st["queue_waits"] == 2 and st["llm_errors"] == 0 and st["served_checked"] == 1
    with pytest.raises(ms.CrossModelRouteError):  # a reply from another model never counts
        b.chat([{"role": "user", "content": "x"}])
    assert b.stats()["served_mismatch"] == 1
    with pytest.raises(ms.SchedulerUnavailableError):  # a real 5xx is not a queue
        b.chat([{"role": "user", "content": "x"}])


@pytest.mark.parametrize(
    "cmd",
    [
        "cat ${PWD%/*}/private/state.json",
        "ls $TMPDIR",
        "cat${IFS}/etc/passwd",
        "cat $(printf '\\57etc/passwd')",
        "ln -s .. up && cat up/private/x",
        'cd "$(dirname "$PWD")" && ls',
    ],
)
def test_indirect_path_escapes_are_denied(tmp_path, cmd):
    env = _env(tmp_path)
    try:
        assert env.sh(cmd).startswith("DENIED by permits()"), cmd
    finally:
        env.close()


def test_tampering_with_the_verifier_inputs_invalidates_the_episode(tmp_path):
    """The shell filter is policy, not a jail: a program run from the workspace can reach
    a sibling directory. The grading inputs are checked before every verification."""
    env = _env(tmp_path)
    try:
        (env.ws.private / "planted.txt").write_text("x", encoding="utf-8")  # as such a program
        out = env.run_tests()
        assert "invalid" in out and env.invalid and env.done()
        with pytest.raises(Exception, match="invalid"):
            env.final()
    finally:
        env.close()


def _can_symlink() -> bool:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        try:
            os.symlink(os.path.join(d, "a"), os.path.join(d, "b"))
        except (OSError, NotImplementedError):
            return False
    return True


@pytest.mark.skipif(not _can_symlink(), reason="this host cannot create symlinks")
def test_a_planted_symlink_does_not_survive_a_rollback(tmp_path):
    from adk.skilltasks.terminal import _Tree

    env = _env(tmp_path)
    target = env.root / "seed.txt"
    link = env.root / "planted"
    try:
        snap = _Tree.take(env.root)
        os.symlink(target, link)
        assert snap.restore(env.root) == [] and not os.path.lexists(link)
    finally:
        env.close()


# --- the harness never reads or walks through a link the model planted ------------------------
# Security review of #10876, follow-up 1: _digests() and _tree() run as the harness user (root
# under a rootful jail) after every step; following a planted link reads or walks host files.


@pytest.mark.skipif(not _can_symlink(), reason="this host cannot create symlinks")
def test_digests_and_tree_never_follow_a_planted_link(tmp_path):
    outside = tmp_path / "host-secret"
    (outside / "deep").mkdir(parents=True)
    (outside / "deep" / "key.pem").write_bytes(b"HOST-SECRET-BYTES")
    (outside / "file.txt").write_bytes(b"HOST-FILE-BYTES")
    env = _env(tmp_path / "w")
    try:
        os.symlink(outside, env.root / "dirlink")
        os.symlink(outside / "file.txt", env.root / "filelink")
        digests = env._digests()
        assert not any(b"HOST-" in v for v in digests.values())
        assert digests["filelink"].startswith(b"<link:")
        assert not any(k.startswith("dirlink/") for k in digests)
        tree = env._tree(env.root)
        assert "key.pem" not in tree and "dirlink (link)" in tree
    finally:
        env.close()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this host")
def test_a_fifo_in_the_workspace_never_blocks_the_harness(tmp_path):
    env = _env(tmp_path)
    try:
        import threading

        from adk.skilltasks.terminal import Step

        os.mkfifo(env.root / "pipe")
        assert "pipe" not in env._digests()  # never read as content
        assert "pipe (0 B)" in env._tree(env.root)
        step = Step(aid=0, args={"path": "pipe"})
        t = threading.Thread(target=env._do_read, args=(step,), daemon=True)
        t.start()
        t.join(5)
        if t.is_alive():  # unblock the reader so the test process can exit
            fd = os.open(env.root / "pipe", os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
        assert not t.is_alive(), "reading a FIFO blocked the harness"
        assert step.exit_class == 1 and "not a regular file" in step.output
    finally:
        env.close()


def test_a_huge_file_is_digested_not_held(tmp_path):
    from adk.skilltasks.terminal import _read_regular

    p = tmp_path / "big.bin"
    p.write_bytes(b"x" * 4096)
    assert _read_regular(p, cap=1024).startswith(b"<sha256:")
    assert _read_regular(p, cap=1 << 20) == b"x" * 4096


@pytest.mark.skipif(not _can_symlink(), reason="this host cannot create symlinks")
def test_a_read_file_swapped_for_a_link_is_not_followed_when_the_prompt_is_built(tmp_path):
    from adk.skilltasks.terminal import Step

    outside = tmp_path / "host-secret.txt"
    outside.write_text("HOST-SECRET\n", encoding="utf-8")
    env = _env(tmp_path / "w")
    try:
        name = sorted(p.name for p in env.root.iterdir() if p.is_file())[0]
        step = Step(aid=0, args={"path": name})
        env._do_read(step)
        assert step.exit_class == 0 and name in env.known
        reads: list = []
        real = Path.read_text

        def spy(self, *a, **k):
            reads.append(str(self))
            return real(self, *a, **k)

        os.unlink(env.root / name)
        os.symlink(outside, env.root / name)
        Path.read_text = spy
        try:
            lines = env._known_lines()
        finally:
            Path.read_text = real
        assert not any("host-secret" in r for r in reads)
        assert any(name in line and "DELETED" in line for line in lines)
        assert name not in env._unread()
    finally:
        env.close()


def test_an_unchanged_crlf_file_is_reported_unchanged(tmp_path):
    from adk.skilltasks.terminal import Step

    env = _env(tmp_path)
    try:
        (env.root / "crlf.txt").write_bytes(b"one\r\ntwo\r\n")
        step = Step(aid=0, args={"path": "crlf.txt"})
        env._do_read(step)
        assert step.exit_class == 0
        line = next(x for x in env._known_lines() if x.startswith("- crlf.txt"))
        assert "CHANGED" not in line and "DELETED" not in line
    finally:
        env.close()
