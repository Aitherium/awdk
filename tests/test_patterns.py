"""Tests for adk.patterns: discovery, loading, running, importing, CLI."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest
from adk import patterns as pm

BUNDLED = Path(pm.__file__).parent / "packs" / "aither" / "patterns"
HOUSE = ["summarize", "extract_wisdom", "explain_code", "write_commit_message",
         "report_150w", "critique_plan"]


def _mk(root: Path, name: str, system: str, user: str | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "system.md").write_text(system, encoding="utf-8")
    if user is not None:
        (d / "user.md").write_text(user, encoding="utf-8")
    return d


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point Path.home() at a temp dir so ~/.aither/patterns is ours."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.delenv(pm.ENV_DIRS, raising=False)
    return h


# ── discovery ───────────────────────────────────────────────────────────────


def test_pattern_dirs_order_env_then_home_then_bundled(home: Path, tmp_path: Path, monkeypatch):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    user_dir = home / ".aither" / "patterns"
    user_dir.mkdir(parents=True)
    monkeypatch.setenv(pm.ENV_DIRS, os.pathsep.join([str(a), str(b)]))

    dirs = pm.pattern_dirs()
    assert dirs == [a, b, user_dir, BUNDLED]


def test_pattern_dirs_skips_missing(home: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv(pm.ENV_DIRS, str(tmp_path / "does-not-exist"))
    assert pm.pattern_dirs() == [BUNDLED]


def test_first_dir_wins_on_name_collision(home: Path, tmp_path: Path, monkeypatch):
    a = tmp_path / "a"
    b = tmp_path / "b"
    _mk(a, "summarize", "FROM-A " * 5)
    _mk(b, "summarize", "FROM-B " * 5)
    _mk(b, "only_b", "ONLY-B " * 5)
    monkeypatch.setenv(pm.ENV_DIRS, os.pathsep.join([str(a), str(b)]))

    got = {p.name: p for p in pm.list_patterns()}
    assert got["summarize"].system.startswith("FROM-A")
    assert got["summarize"].source == str(a)
    assert got["only_b"].source == str(b)
    # bundled names still present behind the overrides
    assert set(HOUSE) <= set(got)
    assert pm.get_pattern("summarize").system.startswith("FROM-A")


def test_user_md_loaded(home: Path, tmp_path: Path, monkeypatch):
    a = tmp_path / "a"
    _mk(a, "with_user", "SYS", user="PREFIX")
    monkeypatch.setenv(pm.ENV_DIRS, str(a))
    p = pm.get_pattern("with_user")
    assert p.user == "PREFIX"
    msgs = pm.build_messages(p, "BODY")
    assert msgs[0].role == "system" and msgs[0].content == "SYS"
    assert msgs[1].role == "user" and msgs[1].content == "PREFIX\n\nBODY"


def test_get_pattern_missing_names_searched_dirs(home: Path, tmp_path: Path, monkeypatch):
    a = tmp_path / "a"
    a.mkdir()
    monkeypatch.setenv(pm.ENV_DIRS, str(a))
    with pytest.raises(KeyError) as ei:
        pm.get_pattern("nope")
    msg = ei.value.args[0]
    assert "nope" in msg
    assert str(a) in msg
    assert str(BUNDLED) in msg


# ── running ─────────────────────────────────────────────────────────────────


class _FakeResponse:
    content = "CONTROL-4410"


class _FakeRouter:
    def __init__(self):
        self.calls: list[tuple] = []

    async def chat(self, messages, model=None, **kw):
        self.calls.append((messages, model))
        return _FakeResponse()


async def test_run_pattern_uses_router_and_returns_content(home: Path):
    router = _FakeRouter()
    out = await pm.run_pattern("summarize", "hello world", model="m-1", router=router)
    assert out == "CONTROL-4410"
    (messages, model), = router.calls
    assert model == "m-1"
    assert messages[0].role == "system"
    assert "IDENTITY AND PURPOSE" in messages[0].content
    assert messages[1].role == "user" and messages[1].content == "hello world"


async def test_run_pattern_unknown_raises(home: Path):
    with pytest.raises(KeyError):
        await pm.run_pattern("no_such_pattern", "x", router=_FakeRouter())


# ── importing ───────────────────────────────────────────────────────────────


def _fabric_clone(root: Path) -> Path:
    clone = root / "Fabric"
    pats = clone / "data" / "patterns"
    _mk(pats, "alpha", "ALPHA SYSTEM", user="ALPHA USER")
    (pats / "alpha" / "README.md").write_text("alpha readme", encoding="utf-8")
    (pats / "alpha" / "ignored.bin").write_bytes(b"\x00")
    _mk(pats, "beta", "BETA SYSTEM")
    (pats / "not_a_pattern").mkdir()
    return clone


def test_import_from_fabric_clone_writes_provenance(home: Path, tmp_path: Path):
    clone = _fabric_clone(tmp_path)
    done = pm.import_patterns(clone)
    assert done == ["alpha", "beta"]

    dest = home / ".aither" / "patterns"
    assert (dest / "alpha" / "system.md").read_text(encoding="utf-8") == "ALPHA SYSTEM"
    assert (dest / "alpha" / "user.md").read_text(encoding="utf-8") == "ALPHA USER"
    assert (dest / "alpha" / "README.md").exists()
    assert not (dest / "alpha" / "ignored.bin").exists()
    assert not (dest / "beta" / "user.md").exists()

    prov = (dest / "alpha" / "PROVENANCE.txt").read_text(encoding="utf-8")
    assert "source: " in prov and "alpha" in prov
    assert "system_sha256: " in prov
    assert "imported: " in prov
    import hashlib
    assert hashlib.sha256(b"ALPHA SYSTEM").hexdigest() in prov

    # imported patterns are now discoverable ahead of bundled ones
    assert pm.get_pattern("alpha").source == str(dest)


def test_import_refuses_overwrite_unless_asked(home: Path, tmp_path: Path):
    clone = _fabric_clone(tmp_path)
    pm.import_patterns(clone, names=["alpha"])
    (clone / "data" / "patterns" / "alpha" / "system.md").write_text("ALPHA V2", encoding="utf-8")

    with pytest.raises(FileExistsError):
        pm.import_patterns(clone, names=["alpha"])
    dest = home / ".aither" / "patterns"
    assert (dest / "alpha" / "system.md").read_text(encoding="utf-8") == "ALPHA SYSTEM"

    assert pm.import_patterns(clone, names=["alpha"], overwrite=True) == ["alpha"]
    assert (dest / "alpha" / "system.md").read_text(encoding="utf-8") == "ALPHA V2"


def test_import_explicit_dest_and_missing_name(home: Path, tmp_path: Path):
    clone = _fabric_clone(tmp_path)
    dest = tmp_path / "custom"
    assert pm.import_patterns(clone / "data" / "patterns", dest, names=["beta"]) == ["beta"]
    assert (dest / "beta" / "PROVENANCE.txt").exists()
    with pytest.raises(FileNotFoundError):
        pm.import_patterns(clone, dest, names=["gamma"])
    with pytest.raises(FileNotFoundError):
        pm.import_patterns(tmp_path / "nowhere", dest)


# ── bundled patterns ────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", HOUSE)
def test_bundled_pattern_loads(home: Path, name: str):
    p = pm.get_pattern(name)
    assert p.source == str(BUNDLED)
    assert len(p.system) > 200
    headings = ("# IDENTITY AND PURPOSE", "# STEPS", "# OUTPUT SECTIONS", "# OUTPUT INSTRUCTIONS")
    for heading in headings:
        assert heading in p.system, f"{name} lacks {heading}"


def test_bundled_readme_present():
    assert (BUNDLED / "README.md").is_file()


# ── CLI ─────────────────────────────────────────────────────────────────────


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    pm.add_patterns_parser(sub)
    return parser.parse_args(argv)


def test_cmd_patterns_list_prints_names(home: Path, capsys):
    rc = pm.cmd_patterns(_parse(["patterns", "list"]))
    assert rc == 0
    out = capsys.readouterr().out
    for name in HOUSE:
        assert name in out
    assert str(BUNDLED) in out


def test_cmd_patterns_show_and_missing(home: Path, capsys):
    assert pm.cmd_patterns(_parse(["patterns", "show", "summarize"])) == 0
    assert "IDENTITY AND PURPOSE" in capsys.readouterr().out
    assert pm.cmd_patterns(_parse(["patterns", "show", "nope"])) == 1
    assert "nope" in capsys.readouterr().err


def test_cmd_patterns_run_reads_file_and_stdin(home: Path, tmp_path: Path, monkeypatch, capsys):
    import io

    import adk.llm

    router = _FakeRouter()
    # run_pattern builds LLMRouter() lazily; hand it the fake instead.
    monkeypatch.setattr(adk.llm, "LLMRouter", lambda *a, **k: router)

    f = tmp_path / "in.txt"
    f.write_text("file body", encoding="utf-8")
    argv = ["patterns", "run", "summarize", "--in", str(f), "--model", "m"]
    assert pm.cmd_patterns(_parse(argv)) == 0
    assert "CONTROL-4410" in capsys.readouterr().out
    assert router.calls[-1][1] == "m"
    assert router.calls[-1][0][1].content == "file body"

    monkeypatch.setattr("sys.stdin", io.StringIO("stdin body"))
    assert pm.cmd_patterns(_parse(["patterns", "run", "summarize"])) == 0
    assert router.calls[-1][0][1].content == "stdin body"

    monkeypatch.setattr("sys.stdin", io.StringIO("   "))
    assert pm.cmd_patterns(_parse(["patterns", "run", "summarize"])) == 2


def test_cmd_patterns_import(home: Path, tmp_path: Path, capsys):
    clone = _fabric_clone(tmp_path)
    rc = pm.cmd_patterns(_parse(["patterns", "import", str(clone), "--names", "alpha"]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "imported alpha" in out and "1 pattern(s) imported" in out
    # second import without --overwrite is refused with exit 1
    assert pm.cmd_patterns(_parse(["patterns", "import", str(clone), "--names", "alpha"])) == 1
    assert "already exists" in capsys.readouterr().err
