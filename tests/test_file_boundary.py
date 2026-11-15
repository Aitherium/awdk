"""A project agent's file tools stay inside the project folder (2026-11-12).

Measured before this change (adk 3.8.105, `adk init start-agent` then `adk run`
from start-agent/): asked to read ``<W>/canary.txt`` -- a file BESIDE the project
folder -- the agent's ``file_read`` returned its contents. The file tools did have
a root list, but on a desktop it held the signed-in user's whole home folder, and
containment was a string-prefix test.

On the pre-change module every "refused" test here reads or writes the canary
(home was a root), ``files.allowed_roots`` / ``files.full_machine`` did not exist,
and the prefix-sibling case passed the boundary.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from adk import builtin_tools as bt

CANARY = "canary-HOST-SECRET-9931"
# Read by name so this file also RUNS (and fails) against the pre-change module.
FULL_MACHINE_ENV = getattr(bt, "FULL_MACHINE_ENV", "AITHER_FILES_FULL_MACHINE")


def _can_symlink() -> bool:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        try:
            os.symlink(os.path.join(d, "a"), os.path.join(d, "b"))
        except (OSError, NotImplementedError):
            return False
    return True


needs_symlink = pytest.mark.skipif(not _can_symlink(), reason="cannot create symlinks here")
windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows path forms")


def _link_dir(link: Path, target: Path) -> None:
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(str(target), str(link), target_is_directory=True)


def _short_name(path: Path) -> str:
    import ctypes

    buf = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 1024)
    return buf.value


@pytest.fixture(autouse=True)
def _reset():
    def clear():
        bt._ALLOWED_ROOTS = None
        bt._FULL_MACHINE = None
        bt._FULL_MACHINE_ANNOUNCED = False

    clear()
    yield
    clear()


@pytest.fixture
def project(tmp_path, monkeypatch):
    """<W>/start-agent is the scaffold; <W>/canary.txt sits beside it."""
    monkeypatch.delenv("AITHER_ALLOWED_ROOTS", raising=False)
    monkeypatch.delenv(FULL_MACHINE_ENV, raising=False)
    # The measured case: on a desktop the home folder contained <W>.
    monkeypatch.setattr(bt, "_DEFAULT_ALLOWED_ROOTS", [str(tmp_path / "start-agent"), str(tmp_path)])
    proj = tmp_path / "start-agent"
    proj.mkdir()
    (proj / "agent.py").write_text("# scaffold\n", encoding="utf-8")
    (proj / "config.yaml").write_text("identity: start-agent\n", encoding="utf-8")
    (proj / "notes.txt").write_text("inside", encoding="utf-8")
    (tmp_path / "canary.txt").write_text(CANARY, encoding="utf-8")
    monkeypatch.chdir(proj)
    return proj


def _refused(raw: str) -> dict:
    assert CANARY not in raw
    out = json.loads(raw)
    assert "error" in out, out
    return out


# ── the scaffold keeps working inside its folder ───────────────────────────


def test_project_root_is_the_project_folder_only(project):
    assert [Path(r) for r in bt._get_allowed_roots()] == [project]


def test_scaffold_still_reads_writes_lists_searches_inside(project):
    assert bt.file_read("notes.txt") == "inside"
    assert bt.file_read(str(project / "notes.txt")) == "inside"
    assert json.loads(bt.file_write("out/a.txt", "x"))["success"] is True
    assert json.loads(bt.file_edit("out/a.txt", "x", "y"))["success"] is True
    assert (project / "out" / "a.txt").read_text(encoding="utf-8") == "y"
    names = {e["name"] for e in json.loads(bt.file_list("."))["entries"]}
    assert {"agent.py", "config.yaml", "notes.txt", "out"} <= names
    assert json.loads(bt.file_search(".", "**/*.txt", "inside"))["count"] == 1


# ── the measured gap, and its spellings ────────────────────────────────────


def test_canary_beside_the_project_is_refused_by_every_file_tool(project, tmp_path):
    target = str(tmp_path / "canary.txt")
    out = _refused(bt.file_read(target))
    # One line that names the root and how to add one.
    assert str(project) in out["error"]
    assert "files.allowed_roots" in out["error"] and "config.yaml" in out["error"]
    assert "\n" not in out["error"]
    _refused(bt.file_write(target, "overwritten"))
    _refused(bt.file_write(target, "appended", mode="append"))
    _refused(bt.file_edit(target, CANARY, "x"))
    _refused(bt.file_list(str(tmp_path)))
    _refused(bt.file_search(str(tmp_path), "*.txt", "canary"))
    assert (tmp_path / "canary.txt").read_text(encoding="utf-8") == CANARY


@pytest.mark.parametrize("path", [
    "../canary.txt",
    "out/../../canary.txt",
    "..\\canary.txt" if os.name == "nt" else "./../canary.txt",
])
def test_traversal_is_refused(project, path):
    _refused(bt.file_read(path))
    _refused(bt.file_write(path, "x"))


@pytest.mark.parametrize("path", [
    "/etc/passwd",
    "/proc/self/environ",
    "~/.ssh/id_rsa",
    os.path.expanduser("~/.aither/session-bearer"),
])
def test_absolute_paths_outside_are_refused(project, path):
    _refused(bt.file_read(path))


def test_sibling_folder_sharing_a_name_prefix_is_outside(project, tmp_path):
    evil = tmp_path / "start-agent-evil"
    evil.mkdir()
    (evil / "x.txt").write_text(CANARY, encoding="utf-8")
    _refused(bt.file_read(str(evil / "x.txt")))


def test_directory_link_escape_is_refused_and_not_walked(project, tmp_path):
    _link_dir(project / "escape", tmp_path)
    _refused(bt.file_read("escape/canary.txt"))
    _refused(bt.file_write("escape/planted.txt", "x"))
    assert not (tmp_path / "planted.txt").exists()
    assert CANARY not in bt.file_search(".", "**/*.txt", "canary")
    assert json.loads(bt.file_search(".", "**/canary.txt"))["count"] == 0
    assert "canary.txt" not in bt.file_list(".", "**/*")


@needs_symlink
def test_file_symlink_escape_is_refused(project, tmp_path):
    os.symlink(str(tmp_path / "canary.txt"), str(project / "link.txt"))
    _refused(bt.file_read("link.txt"))
    _refused(bt.file_write("link.txt", "overwritten"))
    assert CANARY not in bt.file_search(".", "*.txt", "canary")
    assert (tmp_path / "canary.txt").read_text(encoding="utf-8") == CANARY


def test_glob_pattern_cannot_climb_out(project):
    assert "canary.txt" not in bt.file_list(".", "../*")
    assert CANARY not in bt.file_search(".", "../*.txt", "canary")
    assert json.loads(bt.file_search(".", "../*.txt"))["count"] == 0


def test_nul_byte_is_refused(project):
    _refused(bt.file_read("notes.txt\x00.png"))


@windows_only
@pytest.mark.parametrize("form", ["unc", "unc_fwd", "extended", "device", "other_drive", "drive_relative"])
def test_windows_drive_and_unc_forms_are_refused(project, tmp_path, form):
    canary = tmp_path / "canary.txt"
    drive, rest = os.path.splitdrive(str(canary))
    other = next(f"{c}:" for c in "ZYXWVUTSRQ" if f"{c}:".lower() != drive.lower())
    path = {
        "unc": f"\\\\localhost\\{drive[0]}${rest}",
        "unc_fwd": f"//localhost/{drive[0]}${rest}".replace("\\", "/"),
        "extended": f"\\\\?\\{canary}",
        "device": "\\\\.\\PhysicalDrive0",
        "other_drive": f"{other}\\canary.txt",
        "drive_relative": f"{other}canary.txt",
    }[form]
    _refused(bt.file_read(path))
    if form in ("unc", "unc_fwd", "extended"):   # write-probe only spellings of the tmp canary
        _refused(bt.file_write(path, "x"))
        assert canary.read_text(encoding="utf-8") == CANARY


@windows_only
def test_case_and_short_names_resolve_before_the_check(project, tmp_path):
    # Same folder, different spelling: allowed.
    assert bt.file_read(str(project).upper() + "\\NOTES.TXT") == "inside"
    long_dir = project / "a long folder name"
    long_dir.mkdir()
    (long_dir / "f.txt").write_text("inside", encoding="utf-8")
    short = _short_name(long_dir)
    assert bt.file_read(short + "\\f.txt") == "inside"
    # An outside folder reached by its short name: refused.
    outside = tmp_path / "another long folder name"
    outside.mkdir()
    (outside / "c.txt").write_text(CANARY, encoding="utf-8")
    _refused(bt.file_read(_short_name(outside) + "\\c.txt"))
    _refused(bt.file_read(str(tmp_path).upper() + "\\CANARY.TXT"))


# ── the allow-list in the project config ───────────────────────────────────


def test_config_allow_list_adds_roots_relative_to_the_project(project, tmp_path):
    shared = tmp_path / "shared-data"
    shared.mkdir()
    (shared / "d.txt").write_text("shared", encoding="utf-8")
    _refused(bt.file_read(str(shared / "d.txt")))
    (project / "config.yaml").write_text(
        "identity: start-agent\nfiles:\n  allowed_roots:\n    - ../shared-data\n", encoding="utf-8")
    bt._ALLOWED_ROOTS = None
    assert bt.file_read(str(shared / "d.txt")) == "shared"
    assert bt.file_read("../shared-data/d.txt") == "shared"
    _refused(bt.file_read(str(tmp_path / "canary.txt")))      # still only what was listed


def test_env_allow_list_still_adds_roots(project, tmp_path, monkeypatch):
    shared = tmp_path / "env-shared"
    shared.mkdir()
    (shared / "d.txt").write_text("shared", encoding="utf-8")
    monkeypatch.setenv("AITHER_ALLOWED_ROOTS", str(shared))
    assert bt.file_read(str(shared / "d.txt")) == "shared"
    _refused(bt.file_read(str(tmp_path / "canary.txt")))


def test_unreadable_config_grants_nothing(project, tmp_path):
    (project / "config.yaml").write_text("files: [not, a, mapping\n", encoding="utf-8")
    assert [Path(r) for r in bt._get_allowed_roots()] == [project]
    _refused(bt.file_read(str(tmp_path / "canary.txt")))


# ── the explicit full-machine opt-out ──────────────────────────────────────


def test_full_machine_is_off_by_default(project, tmp_path, capsys):
    _refused(bt.file_read(str(tmp_path / "canary.txt")))
    assert "FILE BOUNDARY OFF" not in capsys.readouterr().err


@pytest.mark.parametrize("how", ["config", "env"])
def test_full_machine_opt_out_works_and_is_loud(project, tmp_path, monkeypatch, capsys, caplog, how):
    if how == "config":
        (project / "config.yaml").write_text("files:\n  full_machine: true\n", encoding="utf-8")
    else:
        monkeypatch.setenv(FULL_MACHINE_ENV, "1")
    with caplog.at_level("WARNING", logger="adk.builtin_tools"):
        assert bt.file_read(str(tmp_path / "canary.txt")) == CANARY
        assert bt.file_read(str(tmp_path / "canary.txt")) == CANARY
    err = capsys.readouterr().err
    assert err.count("FILE BOUNDARY OFF") == 1          # loud, once
    assert any("FILE BOUNDARY OFF" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("value", ["yes please", "full_machine", '"true"', "1"])
def test_only_a_real_true_in_config_opts_out(project, tmp_path, value):
    (project / "config.yaml").write_text(f"files:\n  full_machine: {value}\n", encoding="utf-8")
    _refused(bt.file_read(str(tmp_path / "canary.txt")))


def test_env_can_force_the_boundary_back_on(project, tmp_path, monkeypatch):
    (project / "config.yaml").write_text("files:\n  full_machine: true\n", encoding="utf-8")
    monkeypatch.setenv(FULL_MACHINE_ENV, "0")
    _refused(bt.file_read(str(tmp_path / "canary.txt")))


def test_full_machine_never_widens_a_pack_scope(project, tmp_path, monkeypatch):
    from adk import pack_scope

    monkeypatch.setenv(FULL_MACHINE_ENV, "1")
    monkeypatch.setattr(pack_scope, "pack_data_root", lambda pack_id: project / "packdata")
    (project / "packdata").mkdir()
    with pack_scope.pack_scope("demo"):
        _refused(bt.file_read(str(tmp_path / "canary.txt")))
        _refused(bt.file_read(str(project / "notes.txt")))


# ── not a project: the personal assistant's defaults are unchanged ─────────


def test_outside_a_project_the_default_roots_are_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("AITHER_ALLOWED_ROOTS", raising=False)
    monkeypatch.setattr(bt, "_DEFAULT_ALLOWED_ROOTS", [str(tmp_path), "HOME-SENTINEL"])
    monkeypatch.chdir(tmp_path)                       # no agent.py here
    monkeypatch.setattr(sys.modules["__main__"], "__file__", str(tmp_path / "not_agent.py"),
                        raising=False)
    assert bt._get_allowed_roots() == [str(tmp_path), "HOME-SENTINEL"]


def test_python_agent_py_from_another_folder_is_still_a_project(project, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(sys.modules["__main__"], "__file__", str(project / "agent.py"),
                        raising=False)
    assert [Path(r) for r in bt._get_allowed_roots()] == [project]
    _refused(bt.file_read(str(tmp_path / "canary.txt")))
