"""W4: structural prediction -- co-change history first, the awgraph call graph on a miss."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from adk.world_code import COCHANGE, NONE, STRUCTURAL, CoChangeModel, CodeWorld, git_toplevel

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

FILES = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def helper(x):\n    return x + 1\n",
    "pkg/app.py": "from pkg.util import helper\n\n\ndef run():\n    return helper(2)\n",
    "pkg/cli.py": "from pkg.app import run\n\n\ndef main():\n    run()\n",
}


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@example.invalid",
                    "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
                   check=True, capture_output=True)


def _commit(root: Path, files: dict, msg: str) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", msg)


@pytest.fixture(autouse=True, scope="module")
def _private_awgraph_cache(tmp_path_factory):
    """awgraph makes a per-root cache dir even on a read; keep it out of the user cache."""
    mp = pytest.MonkeyPatch()
    mp.setenv("AWGRAPH_CACHE_DIR", str(tmp_path_factory.mktemp("awgraph-cache")))
    yield
    mp.undo()


@pytest.fixture(scope="module")
def repo(tmp_path_factory, _private_awgraph_cache) -> Path:
    root = tmp_path_factory.mktemp("repo")
    _git(root, "init", "-q")
    _commit(root, FILES, "init")
    for i in (1, 2):
        _commit(root, {"pkg/util.py": FILES["pkg/util.py"] + f"# rev {i}\n",
                       "pkg/app.py": FILES["pkg/app.py"] + f"# rev {i}\n"}, f"couple {i}")
    _commit(root, {"pkg/cli.py": FILES["pkg/cli.py"] + "# cli\n", "README.md": "x\n"}, "cli")
    _commit(root, {f"bulk/f{i}.txt": "x\n" for i in range(5)}, "bulk")
    # never committed: history knows nothing about these two
    (root / "pkg" / "new.py").write_text(
        "from pkg.util import helper\n\n\ndef fresh():\n    return helper(9)\n", encoding="utf-8")
    (root / "pkg" / "lonely.py").write_text("def alone():\n    return 1\n", encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def world(repo: Path) -> CodeWorld:
    w = CodeWorld(str(repo), build=True, cochange=CoChangeModel.from_git(
        str(repo), max_files_per_commit=4))
    assert w.locate("helper"), w.telemetry
    return w


def test_cochange_is_learned_from_git_history(repo: Path):
    m = CoChangeModel.from_git(str(repo), max_files_per_commit=4)
    assert m.commits == 4 and m.skipped_bulk == 1        # the 5-file commit is noise
    top = m.predict("pkg/util.py")
    assert top[0] == ("pkg/app.py", 1.0, 3)              # changed together every time
    assert ("pkg/cli.py", pytest.approx(1 / 3), 1) in [(f, p, n) for f, p, n in top]
    assert m.predict("pkg/new.py") == []                 # no history -> nothing


def test_impact_is_the_union_of_caller_and_callee_files(world: CodeWorld):
    by_symbol = {i.file: i for i in world.impact("helper")}
    assert set(by_symbol) == {"pkg/app.py", "pkg/new.py"}
    assert all(i.callers == 1 and i.callees == 0 for i in by_symbol.values())
    by_file = [i.file for i in world.impact("pkg/app.py")]
    assert set(by_file) == {"pkg/util.py", "pkg/cli.py"}
    assert world.impact("pkg/lonely.py") == []


def test_predict_cochange_prefers_history_and_labels_the_source(world: CodeWorld):
    hist = world.predict_cochange("pkg/util.py")
    assert hist.source == COCHANGE and hist.files[0] == ("pkg/app.py", 1.0)
    graph = world.predict_cochange("pkg/new.py")
    assert graph.source == STRUCTURAL and [f for f, _ in graph.files] == ["pkg/util.py"]
    none = world.predict_cochange("pkg/lonely.py")
    assert none.source == NONE and none.files == ()


def test_absolute_paths_and_subdirectory_roots_work(repo: Path, world: CodeWorld):
    p = world.predict_cochange(str(repo / "pkg" / "util.py"))
    assert p.source == COCHANGE
    sub = CodeWorld(str(repo / "pkg"), graph=world._graph)
    assert sub.repo_root == git_toplevel(str(repo))
    assert sub.predict_cochange("pkg/util.py").source == COCHANGE


def test_a_directory_without_git_degrades_with_a_reason(tmp_path: Path):
    (tmp_path / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        CoChangeModel.from_git(str(tmp_path))
    w = CodeWorld(str(tmp_path))
    assert w.predict_cochange("m.py").source == NONE
    assert any(d.startswith("git:") for d in w.telemetry["degraded"])
    assert any(d.startswith("awgraph:") for d in w.telemetry["degraded"])  # no index built


# -- task-conditioned prediction (predict_files) ------------------------------------
class _FakeLocalizer:
    name = "fake"

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def localize(self, question, root, k):
        self.calls.append((question, root, k))
        return self.rows[:k]


class _BrokenLocalizer:
    name = "broken"

    def localize(self, question, root, k):
        raise RuntimeError("map exploded")


def test_rrf_sums_reciprocal_ranks_and_records_voters():
    from adk.world_code import rrf

    out = rrf({"A": ["x", "y"], "B": ["y", "z"]}, {"A": 1.0, "B": 1.0}, k=60)
    assert [o[0] for o in out] == ["y", "x", "z"]
    y = out[0]
    assert y[1] == pytest.approx(1 / 62 + 1 / 61) and y[2] == ("A", "B")
    assert dict(y[3]) == {"A": 2, "B": 1}


def test_predict_files_fuses_graph_localizer_and_cochange_with_votes(world: CodeWorld, repo):
    from adk.world_code import AWGRAPH, LOCALIZER

    loc = _FakeLocalizer([{"dir": "pkg", "files": ["pkg/cli.py"]}])
    w = CodeWorld(str(repo), graph=world._graph, cochange=world.cochange, localizer=loc)
    pred = w.predict_files("helper increments x by one", k=10, chunk_budget=1)
    by = {f.path: f for f in pred.files}
    assert set(pred.sources) == {AWGRAPH, LOCALIZER, COCHANGE}
    # the graph found the definer; it lies in the localized dir -> a localizer vote too
    assert by["pkg/util.py"].votes == (AWGRAPH, LOCALIZER)
    assert COCHANGE in by["pkg/app.py"].votes            # history: changes with util.py
    assert "pkg/cli.py" not in by or AWGRAPH in by["pkg/cli.py"].votes or \
        COCHANGE in by["pkg/cli.py"].votes               # a prior never injects a file
    assert loc.calls and loc.calls[0][1] == str(repo.resolve())
    scores = [f.score for f in pred.files]
    assert scores == sorted(scores, reverse=True)
    assert pred.degraded == ()
    elsewhere = CodeWorld(str(repo), graph=world._graph, cochange=world.cochange,
                          localizer=_FakeLocalizer([{"dir": "docs", "files": []}]))
    assert LOCALIZER not in elsewhere.predict_files("helper increments x by one", k=10,
                                                    chunk_budget=1).sources


def test_predict_files_degrades_honestly_without_a_localizer_or_history(world: CodeWorld,
                                                                         repo, tmp_path):
    w = CodeWorld(str(repo), graph=world._graph, cochange=CoChangeModel(), localizer=None)
    pred = w.predict_files("helper increments x by one", k=5)
    assert pred.files and all(f.votes == ("AWGRAPH",) for f in pred.files)
    assert "localizer:off" in pred.degraded
    assert any(d.startswith("cochange:") for d in pred.degraded)
    broken = CodeWorld(str(repo), graph=world._graph, cochange=world.cochange,
                       localizer=_BrokenLocalizer())
    pred = broken.predict_files("helper increments x by one", k=5)
    assert pred.files                                    # still answers from the graph
    assert any("localizer:broken:RuntimeError" in d for d in pred.degraded)


def test_predict_files_with_no_graph_names_why(tmp_path):
    (tmp_path / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    w = CodeWorld(str(tmp_path), localizer=None)
    pred = w.predict_files("return one", k=5)
    assert pred.files == ()
    assert any(d.startswith("awgraph:") for d in pred.degraded)


def test_predict_files_filter_and_online_learning(world: CodeWorld, repo):
    cc = CoChangeModel(max_files_per_commit=4)
    w = CodeWorld(str(repo), graph=world._graph, cochange=cc, localizer=None)
    before = w.predict_files("helper increments x by one", k=10, chunk_budget=1)
    assert before.paths == ["pkg/util.py"]               # one chunk: the definer
    w.learn_commit(["pkg/util.py", "pkg/lonely.py"])      # a commit lands
    after = w.predict_files("helper increments x by one", k=10, chunk_budget=1)
    votes = {f.path: f.votes for f in after.files}
    assert votes == {"pkg/util.py": ("AWGRAPH",), "pkg/lonely.py": ("COCHANGE",)}
    only_app = w.predict_files("helper increments x by one", k=10,
                               file_filter=lambda p: p.endswith("app.py"))
    assert only_app.paths in ([], ["pkg/app.py"])


def test_prospector_localizer_is_guarded(monkeypatch):
    from adk.world_code import Localizer, ProspectorLocalizer

    monkeypatch.setitem(__import__("sys").modules, "lib.agents.packs.prospector", None)
    loc = ProspectorLocalizer()
    assert isinstance(loc, Localizer)
    assert loc.localize("where is auth", ".", 3) == []
    assert loc.available is False and loc.error


def test_landmark_rel_is_normalised_under_the_root(tmp_path):
    from adk.world_code import _root_relative

    root = tmp_path / "lib" / "faculties"
    (root / "sub").mkdir(parents=True)
    assert _root_relative(str(root), "faculties") == ""          # parent-based, = root
    assert _root_relative(str(root), "faculties/sub") == "sub"
    assert _root_relative(str(root), "sub") == "sub"             # root-based
    assert _root_relative(str(root), ".") == ""
