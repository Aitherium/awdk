"""The browser IDE's save + git surface on the harness daemon.

``POST /fs/write``, ``GET /git/status`` and ``GET /git/diff`` through the real
``create_app`` under a TestClient, with the browse root pinned to a tmp dir by
``AITHER_HARNESS_BROWSE_ROOTS``. git arms run a real ``git`` against a throwaway
repo and skip when git is absent.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TOKEN = "root-bearer-for-tests-only"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("AITHER_HARNESS_BROWSE_ROOTS", str(root))
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    monkeypatch.setenv("AITHER_HARNESS_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setenv("AITHER_STEER_DISPATCH_STATUS", str(tmp_path / "dispatch.json"))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", TOKEN)
    registry_path = tmp_path / "harness_tokens.json"
    monkeypatch.setenv("AITHER_HARNESS_PRINCIPALS", str(registry_path))

    import adk.harnesses.daemon as daemon
    from adk.harnesses import rooms as rooms_mod
    from adk.harnesses.manager import SessionManager

    monkeypatch.setattr(daemon, "PRINCIPALS_PATH", registry_path)
    monkeypatch.setattr(rooms_mod, "_registry", None)
    app = daemon.create_app(manager=SessionManager(root=tmp_path / "sessions"), token=TOKEN)
    client = TestClient(app)
    client.headers = {"Authorization": f"Bearer {TOKEN}"}
    return {"client": client, "root": root.resolve(), "outside": outside.resolve()}


# ── /fs/read gains sha256 ───────────────────────────────────────────────────────


def test_read_returns_sha256_of_raw_bytes(env):
    f = env["root"] / "a.txt"
    f.write_bytes(b"one\r\ntwo\n")
    body = env["client"].get("/fs/read", params={"path": str(f)}).json()
    assert body["sha256"] == _sha(b"one\r\ntwo\n")
    assert body["content"] == "one\r\ntwo\n"


# ── /fs/write ───────────────────────────────────────────────────────────────────


def test_write_happy_path_preserves_bytes_and_returns_sha(env):
    client, f = env["client"], env["root"] / "a.txt"
    f.write_bytes(b"old\n")
    resp = client.post("/fs/write", json={"path": str(f), "content": "new\r\nline\n",
                                          "expected_sha256": _sha(b"old\n")})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert f.read_bytes() == b"new\r\nline\n"
    assert body == {"path": str(f), "size": 10, "sha256": _sha(b"new\r\nline\n")}
    # The read after the write agrees with the write's hash.
    assert client.get("/fs/read", params={"path": str(f)}).json()["sha256"] == body["sha256"]
    # Atomic: no temp file left behind.
    assert sorted(p.name for p in env["root"].iterdir()) == ["a.txt"]


def test_write_conflict_is_409_with_current_sha(env):
    f = env["root"] / "a.txt"
    f.write_bytes(b"theirs\n")
    resp = env["client"].post("/fs/write", json={"path": str(f), "content": "mine",
                                                 "expected_sha256": _sha(b"what I read\n")})
    assert resp.status_code == 409
    assert "changed on disk since you opened it" in resp.json()["detail"]
    assert _sha(b"theirs\n") in resp.json()["detail"]
    assert f.read_bytes() == b"theirs\n"


def test_write_outside_root_is_403(env):
    target = env["outside"] / "x.txt"
    resp = env["client"].post("/fs/write", json={"path": str(target), "content": "x",
                                                 "create": True})
    assert resp.status_code == 403
    assert not target.exists()


def _can_symlink() -> bool:
    probe = Path(tempfile.mkdtemp())
    try:
        os.symlink(probe, probe / "link", target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False
    finally:
        shutil.rmtree(probe, ignore_errors=True)


@pytest.mark.skipif(not _can_symlink(), reason="cannot create symlinks here")
def test_write_through_symlink_escape_is_refused(env):
    link = env["root"] / "escape"
    os.symlink(env["outside"], link, target_is_directory=True)
    resp = env["client"].post("/fs/write", json={"path": str(link / "x.txt"), "content": "x",
                                                 "create": True})
    assert resp.status_code == 403
    assert not (env["outside"] / "x.txt").exists()


def test_write_into_dot_git_is_refused(env):
    (env["root"] / ".git").mkdir()
    resp = env["client"].post("/fs/write", json={"path": str(env["root"] / ".git" / "config"),
                                                 "content": "x", "create": True})
    assert resp.status_code == 403


def test_write_missing_without_create_is_404_and_create_makes_it(env):
    f = env["root"] / "new.txt"
    resp = env["client"].post("/fs/write", json={"path": str(f), "content": "hi"})
    assert resp.status_code == 404
    assert not f.exists()
    resp = env["client"].post("/fs/write", json={"path": str(f), "content": "hi", "create": True})
    assert resp.status_code == 200
    assert f.read_bytes() == b"hi"


def test_write_parent_must_exist(env):
    f = env["root"] / "nope" / "new.txt"
    resp = env["client"].post("/fs/write", json={"path": str(f), "content": "hi", "create": True})
    assert resp.status_code == 403


def test_write_to_directory_is_403(env):
    (env["root"] / "d").mkdir()
    resp = env["client"].post("/fs/write", json={"path": str(env["root"] / "d"), "content": "x"})
    assert resp.status_code == 403


def test_write_size_cap_is_413(env, monkeypatch):
    from adk.harnesses import fs

    monkeypatch.setattr(fs, "MAX_WRITE_BYTES", 8)
    f = env["root"] / "a.txt"
    f.write_bytes(b"x")
    resp = env["client"].post("/fs/write", json={"path": str(f), "content": "0123456789"})
    assert resp.status_code == 413
    assert f.read_bytes() == b"x"


def test_write_requires_auth(env):
    resp = TestClient(env["client"].app).post(
        "/fs/write", json={"path": str(env["root"] / "a"), "content": "x", "create": True})
    assert resp.status_code in (401, 403)
    assert not (env["root"] / "a").exists()


# ── /git/status and /git/diff ───────────────────────────────────────────────────

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.invalid",
                    "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
                   check=True, capture_output=True)


@pytest.fixture()
def repo(env):
    r = env["root"]
    _git(r, "init", "-q", "-b", "main")
    (r / "tracked.txt").write_bytes(b"one\n")
    (r / "moved.txt").write_bytes(b"move me\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    (r / "tracked.txt").write_bytes(b"one\ntwo\n")
    (r / "untracked.txt").write_bytes(b"fresh\nno eol")
    _git(r, "mv", "moved.txt", "renamed.txt")
    return r


@needs_git
def test_git_status_skips_untracked_by_default(env, repo):
    """The untracked walk is the slow half of status on a big repo: opt-in only."""
    body = env["client"].get("/git/status", params={"path": str(repo)}).json()
    assert body["available"] is True and body["untracked"] is False
    rels = {e["rel"] for e in body["entries"]}
    assert "tracked.txt" in rels and "untracked.txt" not in rels


@needs_git
def test_git_status_is_scoped_to_the_opened_folder(env, repo):
    """Opening repo/sub reports repo/sub, not the rest of the parent repository."""
    sub = repo / "sub"
    sub.mkdir()
    (sub / "a.txt").write_bytes(b"a"+bytes([10]))
    _git(repo, "add", "sub/a.txt")
    _git(repo, "commit", "-q", "-m", "sub")
    (sub / "a.txt").write_bytes(b"b"+bytes([10]))
    body = env["client"].get("/git/status", params={"path": str(sub)}).json()
    assert [e["rel"] for e in body["entries"]] == ["sub/a.txt"]


@needs_git
def test_git_status_reports_modified_untracked_and_renamed(env, repo):
    body = env["client"].get("/git/status", params={"path": str(repo), "untracked": 1}).json()
    assert body["available"] is True, body
    assert Path(body["root"]) == repo
    assert body["branch"] == "main"
    by_rel = {e["rel"]: e for e in body["entries"]}
    assert by_rel["tracked.txt"]["status"] == " M"
    assert by_rel["tracked.txt"]["worktree"] == "M"
    assert Path(by_rel["tracked.txt"]["path"]) == repo / "tracked.txt"
    assert by_rel["untracked.txt"]["status"] == "??"
    assert by_rel["renamed.txt"]["index"] == "R"
    assert by_rel["renamed.txt"]["orig_rel"] == "moved.txt"
    assert "moved.txt" not in by_rel
    assert body["truncated"] is False


@needs_git
def test_git_status_filters_entries_outside_the_root(env, tmp_path, monkeypatch):
    repo_top = env["root"]
    sub = repo_top / "sub"
    sub.mkdir()
    _git(repo_top, "init", "-q", "-b", "main")
    (repo_top / "secret.txt").write_bytes(b"x")
    (sub / "mine.txt").write_bytes(b"y")
    monkeypatch.setenv("AITHER_HARNESS_BROWSE_ROOTS", str(sub))
    body = env["client"].get("/git/status", params={"path": str(sub), "untracked": 1}).json()
    assert body["available"] is True
    paths = [Path(e["path"]) for e in body["entries"]]
    assert paths and all(p.is_relative_to(sub.resolve()) for p in paths)
    assert not any(p.name == "secret.txt" for p in paths)


@needs_git
def test_git_diff_modified_and_untracked(env, repo):
    client = env["client"]
    body = client.get("/git/diff", params={"path": str(repo / "tracked.txt")}).json()
    assert body["available"] is True
    assert "+two" in body["diff"] and body["truncated"] is False

    body = client.get("/git/diff", params={"path": str(repo / "untracked.txt")}).json()
    assert body["available"] is True
    assert "new file mode" in body["diff"]
    assert "+fresh\n+no eol\n\\ No newline at end of file" in body["diff"]

    staged = client.get("/git/diff", params={"path": str(repo / "renamed.txt"), "staged": 1}).json()
    assert staged["available"] is True and "renamed.txt" in staged["diff"]


def _tmp_is_inside_a_repo() -> bool:
    if shutil.which("git") is None:
        return True
    proc = subprocess.run(["git", "-C", tempfile.gettempdir(), "rev-parse", "--show-toplevel"],
                          capture_output=True)
    return proc.returncode == 0


@pytest.mark.skipif(_tmp_is_inside_a_repo(), reason="no git, or tmp sits inside a repo here")
def test_git_not_a_repo_is_available_false(env):
    f = env["root"] / "plain.txt"
    f.write_bytes(b"x")
    status = env["client"].get("/git/status", params={"path": str(env["root"])})
    assert status.status_code == 200
    assert status.json()["available"] is False
    assert status.json()["reason"]
    diff = env["client"].get("/git/diff", params={"path": str(f)}).json()
    assert diff["available"] is False and diff["diff"] == ""


def test_git_outside_root_is_403(env):
    assert env["client"].get("/git/status", params={"path": str(env["outside"])}).status_code == 403
    assert env["client"].get("/git/diff", params={"path": str(env["outside"])}).status_code == 403


def test_git_missing_is_reported(env, monkeypatch):
    from adk.harnesses import git_ops

    monkeypatch.setattr(git_ops.shutil, "which", lambda _name: None)
    body = env["client"].get("/git/status", params={"path": str(env["root"])}).json()
    assert body == {"available": False, "reason": "git not installed", "root": "", "branch": "",
                    "ahead": 0, "behind": 0, "entries": [], "truncated": False, "untracked": False}
