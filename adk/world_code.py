"""A TASK-CONDITIONED code world model: given a task, which files will it touch?

Primary API -- ``CodeWorld.predict_files(task_text, k)``
    The question awgraph, awpredict and Prospector are benchmarked on: a task (a commit
    message with the answer filenames stripped) -> the files that task needs. Three
    ranked lists are fused by reciprocal-rank fusion (RRF, ``score = sum_s w_s /
    (RRF_K + rank_s)``), and every returned file carries the sources that voted for it:

    ``LOCALIZER``  (prior) a ``Localizer`` -- by default Prospector's landmark map
                   (``map_localize``: question -> top-k landmark DIRECTORIES) -- names
                   where to look. A PRIOR over directories, never a file source: its
                   list is the graph's hits re-ordered by the rank of the top directory
                   they fall in, so a hit inside a localized directory gains a vote and
                   nothing the graph did not retrieve is injected. (The landmark's own
                   "files" are an alphabetical sample of the directory; ranking them
                   would inject noise.) Optional: imported GUARDED through the
                   protocol, never a hard import.
    ``AWGRAPH``    awgraph's own benchmarked retrieval: ``CodeGraph.hybrid_query(task,
                   max_results=chunk_budget)`` (the call its CLI ``query`` and its
                   README's cost table use), chunks -> files in first-appearance order.
    ``COCHANGE``   expansion from the top fused hits: partners that historically change
                   together with them (``CoChangeModel``, learned from git, updated
                   online with ``learn_commit``), weighted by the seed's rank.

    A source that is missing (no index, no git, no localizer) is left out of the fusion,
    named in ``FilePrediction.degraded`` and logged -- never silently zero.

Secondary APIs (kept; the file-seeded ones)
    ``CoChangeModel`` (tabular, learned from git) -- for every commit (merges and bulk
    commits touching more than ``max_files_per_commit`` files excluded), every pair of
    files changed together is counted; ``predict(file)`` ranks partners by
    P(partner | file changed).
    ``CodeWorld.impact(symbol_or_file)`` -- the files holding the callers and callees of
    a symbol (or of every symbol in a file), an edge through a name that resolves to k
    chunks counting 1/k. NOTE: seeding this from an arbitrary file is NOT awgraph's
    benchmarked use; the round-1 eval that did so (evals/git_world_eval.py, recorded in
    git_world_eval.round1_file_seeded.json) measured "callers of the alphabetically
    first file", not awgraph's task retrieval.
    ``CodeWorld.predict_cochange(file)`` -- COCHANGE when history knows the file,
    STRUCTURAL on a history miss, NONE otherwise.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import (Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple,
                    runtime_checkable)

logger = logging.getLogger("adk.world_code")

COCHANGE = "COCHANGE"
STRUCTURAL = "STRUCTURAL"
NONE = "NONE"
AWGRAPH = "AWGRAPH"
LOCALIZER = "LOCALIZER"

#: The RRF constant (Cormack et al. 2009's 60): damps the head so no single list's
#: first place decides the fusion alone.
RRF_K = 60.0
#: Pre-declared fusion weights (fixed before any task number was seen; see
#: evals/code_task_eval.py). Equal: no source is trusted over another a priori.
DEFAULT_WEIGHTS: Dict[str, float] = {AWGRAPH: 1.0, LOCALIZER: 1.0, COCHANGE: 1.0}


def _norm(path: str) -> str:
    p = (path or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def _git(root: str, *args: str, timeout: float = 120.0) -> str:
    out = subprocess.run(["git", "-C", root, *args], capture_output=True, timeout=timeout,
                         check=True)
    return out.stdout.decode("utf-8", errors="replace")


def git_toplevel(root: str) -> Optional[str]:
    """The repository root containing ``root``, or None when it is not in a git repo."""
    try:
        top = _git(root, "rev-parse", "--show-toplevel", timeout=30.0).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return str(Path(top).resolve()) if top else None


@dataclass
class CoChangeModel:
    """Pairwise co-change counts from git history. Tabular; see the module docstring."""

    max_commits: int = 2000
    max_files_per_commit: int = 40
    commits: int = 0
    skipped_bulk: int = 0
    touched: Counter = field(default_factory=Counter)
    pairs: Dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    root: Optional[str] = None

    def add_commit(self, files: List[str]) -> None:
        files = sorted({_norm(f) for f in files if f and f.strip()})
        if not files:
            return
        if len(files) > self.max_files_per_commit:
            self.skipped_bulk += 1
            return
        self.commits += 1
        for f in files:
            self.touched[f] += 1
            for g in files:
                if g != f:
                    self.pairs[f][g] += 1

    @classmethod
    def from_git(cls, root: str, *, max_commits: int = 2000,
                 max_files_per_commit: int = 40,
                 within: Optional[str] = None) -> "CoChangeModel":
        """Learn from ``git log`` at ``root``. Raises ValueError when it is not a repo.

        ``within`` (a repo-relative directory) confines the model to it: only commits
        touching it are read, and only paths under it are learned, so no partner
        outside it can ever be predicted. A commit is still judged bulk on ALL the
        files it touched, before the paths outside ``within`` are dropped.
        """
        top = git_toplevel(root)
        if top is None:
            raise ValueError(f"{root} is not inside a git repository")
        m = cls(max_commits=max_commits, max_files_per_commit=max_files_per_commit, root=top)
        inside = _norm(within or "").strip("/")
        if inside in (".", ""):
            inside = ""
        args = ["log", "--no-merges", "--no-renames", "--name-only",
                "--pretty=format:@@commit %H", f"-n{int(max_commits)}"]
        if inside:
            # --full-diff: list every file of a commit that touched `inside`, so
            # the bulk judgement below sees the whole commit.
            args += ["--full-diff", "--", inside]
        text = _git(top, *args)
        commits: List[List[str]] = []
        files: List[str] = []
        started = False
        for line in text.splitlines():
            if line.startswith("@@commit "):
                if started:
                    commits.append(files)
                files, started = [], True
            elif line.strip():
                files.append(line.strip())
        if started:
            commits.append(files)
        for fs in reversed(commits):          # oldest first: the order it happened
            if inside:
                whole = {_norm(f) for f in fs if f and f.strip()}
                if len(whole) > m.max_files_per_commit:
                    m.skipped_bulk += 1
                    continue
                fs = [f for f in whole if f == inside or f.startswith(inside + "/")]
            m.add_commit(fs)
        return m

    def predict(self, file: str, k: int = 10) -> List[Tuple[str, float, int]]:
        """Partners of ``file`` as ``(path, P(partner | file), together)``, best first."""
        f = _norm(file)
        n = self.touched.get(f, 0)
        if not n:
            return []
        ranked = sorted(self.pairs.get(f, {}).items(), key=lambda kv: (-kv[1], kv[0]))
        return [(g, c / n, c) for g, c in ranked[:k]]


@dataclass(frozen=True)
class Impact:
    file: str
    score: float
    callers: int
    callees: int


@dataclass(frozen=True)
class CoChangePrediction:
    """``source`` is COCHANGE | STRUCTURAL | NONE; ``files`` is [(path, score)], best first."""

    file: str
    source: str
    files: Tuple[Tuple[str, float], ...]
    support: int
    note: str = ""


# ============================================================================
# The localizer protocol (Prospector's landmark map, or anything shaped like it)
# ============================================================================

@runtime_checkable
class Localizer(Protocol):
    """question -> the directories to search first.

    ``localize(question, root, k)`` returns up to ``k`` dicts, best first:
    ``{"dir": <relative to root, "" = root itself>, "files": [<relative to root>...]}``.
    It must never raise for a normal miss (return ``[]``); ``name`` labels degradations.
    """

    name: str

    def localize(self, question: str, root: str, k: int) -> List[Dict[str, Any]]: ...


def _root_relative(root: str, rel: str) -> str:
    """A landmark ``rel`` as a path under ``root``. map_localize reports it relative to
    the root's PARENT (root ``.../lib/faculties`` -> rel ``faculties``), or relative to
    the root itself; whichever names an existing directory wins."""
    rel = rel.strip("/")
    if rel in ("", "."):
        return ""
    if os.path.isdir(os.path.join(root, rel)):
        return rel
    parent_based = os.path.join(os.path.dirname(os.path.abspath(root)), rel)
    if os.path.isdir(parent_based):
        r = _norm(os.path.relpath(parent_based, os.path.abspath(root)))
        return "" if r in (".", "") else r
    return rel


class ProspectorLocalizer:
    """AitherProspector's ``map_localize`` behind the Localizer protocol. Guarded.

    Imports ``lib.agents.packs.prospector.tools`` lazily (the AitherOS monorepo on
    ``sys.path``, or ``AITHER_PROSPECTOR_SRC`` naming the directory that contains
    ``lib/``). Unavailable -> ``available`` is False with the reason in ``error``,
    and ``localize`` returns []. ``build=True`` runs ``map_build(root)`` when no map
    exists for the root (it persists under ``AITHER_DATA_DIR``).
    """

    name = "prospector"

    def __init__(self, *, build: bool = False) -> None:
        self.build = bool(build)
        self._tools: Any = None
        self.error: Optional[str] = None
        self._built: set = set()

    @property
    def available(self) -> bool:
        return self._load() is not None

    def _load(self) -> Any:
        if self._tools is not None or self.error is not None:
            return self._tools
        src = os.environ.get("AITHER_PROSPECTOR_SRC", "").strip()
        if src:
            import sys
            if src not in sys.path:
                sys.path.append(src)
        try:
            from lib.agents.packs.prospector import tools  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001 -- optional plane
            self.error = f"{type(exc).__name__}: {exc}"
            return None
        self._tools = tools
        return tools

    def localize(self, question: str, root: str, k: int) -> List[Dict[str, Any]]:
        tools = self._load()
        if tools is None:
            return []
        doc = json.loads(tools.map_localize(question, root, int(k)))
        if not doc.get("ok") and self.build and root not in self._built:
            self._built.add(root)
            built = json.loads(tools.map_build(root))
            if not built.get("ok"):
                self.error = f"map_build: {built.get('error')}"
                return []
            doc = json.loads(tools.map_localize(question, root, int(k)))
        if not doc.get("ok"):
            self.error = str(doc.get("error") or "no landmark map")
            return []
        out = []
        for lm in doc.get("landmarks") or []:
            rel = _root_relative(root, _norm(str(lm.get("rel") or "")))
            files = []
            for f in lm.get("files") or []:
                f = _norm(str(f))
                files.append(f if "/" in f or not rel else f"{rel}/{f}")
            out.append({"dir": rel, "files": files})
        return out


# ============================================================================
# Task-conditioned prediction
# ============================================================================

@dataclass(frozen=True)
class RankedFile:
    """One predicted file: fused score, the sources that voted, and each source's rank."""

    path: str
    score: float
    votes: Tuple[str, ...]
    ranks: Tuple[Tuple[str, int], ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "score": self.score, "votes": list(self.votes),
                "ranks": dict(self.ranks)}


@dataclass(frozen=True)
class FilePrediction:
    """``files`` best first. ``sources`` = the lists that contributed; ``degraded`` = why
    any other source did not."""

    task: str
    files: Tuple[RankedFile, ...]
    sources: Tuple[str, ...]
    degraded: Tuple[str, ...] = ()
    chunks: int = 0
    localized_dirs: Tuple[str, ...] = ()

    @property
    def paths(self) -> List[str]:
        return [f.path for f in self.files]

    def to_dict(self) -> Dict[str, Any]:
        return {"task": self.task, "files": [f.to_dict() for f in self.files],
                "sources": list(self.sources), "degraded": list(self.degraded),
                "chunks": self.chunks, "localized_dirs": list(self.localized_dirs)}


def rrf(lists: Dict[str, Sequence[str]], weights: Optional[Dict[str, float]] = None,
        k: float = RRF_K) -> List[Tuple[str, float, Tuple[str, ...], Tuple[Tuple[str, int], ...]]]:
    """Reciprocal-rank fusion. ``(item, score, voters, ranks)`` best first; ties by name."""
    w = dict(DEFAULT_WEIGHTS)
    w.update(weights or {})
    score: Dict[str, float] = defaultdict(float)
    ranks: Dict[str, Dict[str, int]] = defaultdict(dict)
    for src, items in lists.items():
        seen = set()
        for r, it in enumerate(items, 1):
            if it in seen:
                continue
            seen.add(it)
            score[it] += w.get(src, 1.0) / (k + r)
            ranks[it][src] = r
    out = [(it, s, tuple(sorted(ranks[it])), tuple(sorted(ranks[it].items())))
           for it, s in score.items()]
    out.sort(key=lambda t: (-t[1], t[0]))
    return out


class _LoopRunner:
    """One private event loop for the graph's coroutines, usable from sync code even
    when the caller is itself inside a running loop (then it runs on a worker thread)."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()

    def run(self, factory: Callable[[], Any]) -> Any:
        def _go() -> Any:
            with self._lock:
                if self._loop is None or self._loop.is_closed():
                    self._loop = asyncio.new_event_loop()
                return self._loop.run_until_complete(factory())
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return _go()
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(1) as ex:
            return ex.submit(_go).result()


class CodeWorld:
    """Task-conditioned file prediction plus the file-seeded APIs, for one repository root.

    ``localizer``: a ``Localizer``, ``"auto"`` (ProspectorLocalizer when importable,
    else degraded) or None (off, recorded as degraded).
    """

    def __init__(self, root: str, *, graph: Any = None, build: bool = False,
                 cochange: Optional[CoChangeModel] = None, max_commits: int = 2000,
                 localizer: Any = "auto", weights: Optional[Dict[str, float]] = None) -> None:
        self.root = str(Path(root).resolve())
        self._graph = graph
        self._build = bool(build)
        self._cochange = cochange
        self._max_commits = int(max_commits)
        self._index: Optional[Dict[str, List[Any]]] = None
        self._callees: Optional[Dict[str, List[Any]]] = None
        self.telemetry: Dict[str, Any] = {"degraded": [], "counters": {}}
        top = git_toplevel(self.root)
        self.repo_root = top or self.root
        self.weights = dict(DEFAULT_WEIGHTS)
        self.weights.update(weights or {})
        if localizer == "auto":
            localizer = ProspectorLocalizer()
        self.localizer = localizer
        self._runner = _LoopRunner()

    def _degrade(self, reason: str) -> None:
        if reason not in self.telemetry["degraded"]:
            self.telemetry["degraded"].append(reason)
            logger.warning("[CODEWORLD] degraded: %s", reason)

    def _count(self, name: str) -> None:
        c = self.telemetry["counters"]
        c[name] = c.get(name, 0) + 1

    # -- history -------------------------------------------------------------
    @property
    def cochange(self) -> Optional[CoChangeModel]:
        if self._cochange is None:
            try:
                # Confined to `root`: a CodeWorld over one directory of a monorepo
                # must never predict a partner in a sibling project.
                self._cochange = CoChangeModel.from_git(self.root, max_commits=self._max_commits,
                                                        within=self._root_rel() or None)
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                self._degrade(f"git:{type(exc).__name__}: {exc}")
                self._cochange = CoChangeModel()
        return self._cochange

    def learn_commit(self, files: Sequence[str]) -> None:
        """Online update: a commit that just landed (repo-relative paths)."""
        cc = self.cochange
        if cc is not None:
            cc.add_commit(list(files))

    # -- graph ---------------------------------------------------------------
    def _open_graph(self) -> Any:
        if self._graph is not None:
            return self._graph
        try:
            from awgraph.cli import _open_graph  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001 -- optional plane
            self._degrade(f"awgraph:{type(exc).__name__}: {exc}")
            return None
        try:
            # hydrate=True: the query path needs the persisted vectors, else
            # hybrid_query is keyword-only and says nothing about it.
            graph, ok = self._runner.run(
                lambda: _open_graph(self.root, build=self._build, hydrate=True))
        except Exception as exc:  # noqa: BLE001
            self._degrade(f"awgraph:{type(exc).__name__}: {exc}")
            return None
        if not ok:
            self._degrade(f"awgraph:no-index for {self.root} (run `awgraph index`, "
                          f"or CodeWorld(build=True))")
            return None
        self._graph = graph
        return graph

    def _rel(self, path: str) -> str:
        p = str(path or "")
        try:
            p = os.path.relpath(p, self.repo_root)
        except ValueError:
            pass
        return _norm(p)

    def _root_rel(self) -> str:
        r = self._rel(self.root)
        return "" if r in (".", "") else r

    def _in_root(self, path: str) -> bool:
        """True for a repo-relative path under ``root`` (segment-wise, not a prefix)."""
        rr = self._root_rel()
        p = _norm(path)
        return not rr or p == rr or p.startswith(rr + "/")

    def semantic_coverage(self) -> Optional[float]:
        """Share of chunks carrying an embedding (None without a graph). 0.0 means
        hybrid_query is keyword-only -- awgraph's README warns that is silent."""
        graph = self._open_graph()
        chunks = list(getattr(graph, "chunks", {}).values()) if graph is not None else []
        if not chunks:
            return None
        return sum(1 for c in chunks if getattr(c, "embedding", None) is not None) / len(chunks)

    def retrieve_chunks(self, task_text: str, n: int) -> List[Any]:
        """awgraph's benchmarked retrieval: ``hybrid_query(task, max_results=n)``."""
        graph = self._open_graph()
        if graph is None:
            return []
        try:
            return list(self._runner.run(
                lambda: graph.hybrid_query(task_text, max_results=int(n))) or [])
        except Exception as exc:  # noqa: BLE001
            self._degrade(f"awgraph.hybrid_query:{type(exc).__name__}: {exc}")
            return []

    def _chunk_files(self, chunks: Sequence[Any]) -> List[str]:
        out: List[str] = []
        seen = set()
        for c in chunks:
            f = self._rel(str(getattr(c, "source_path", "") or ""))
            if f and f not in seen:
                seen.add(f)
                out.append(f)
        return out

    def _localize(self, task_text: str, k: int) -> Tuple[List[str], List[str]]:
        """(top dirs, their representative files), repo-relative. [] when off/missing."""
        loc = self.localizer
        if loc is None:
            self._degrade("localizer:off")
            return [], []
        name = str(getattr(loc, "name", type(loc).__name__))
        try:
            rows = loc.localize(task_text, self.root, int(k))
        except Exception as exc:  # noqa: BLE001 -- a localizer fault is a miss, reported
            self._degrade(f"localizer:{name}:{type(exc).__name__}: {exc}")
            return [], []
        if not rows:
            err = getattr(loc, "error", None)
            if err:
                self._degrade(f"localizer:{name}: {err}")
            self._count("localizer.empty")
            return [], []
        base = self._root_rel()
        dirs, files = [], []
        for r in rows:
            d = _norm(str(r.get("dir") or ""))
            dirs.append("/".join(p for p in (base, d) if p))
            for f in r.get("files") or []:
                files.append("/".join(p for p in (base, _norm(str(f))) if p))
        return dirs, files

    def predict_files(self, task_text: str, k: int = 25, *, chunk_budget: Optional[int] = None,
                      dirs: int = 5, seeds: int = 5, per_seed: int = 10,
                      file_filter: Optional[Callable[[str], bool]] = None) -> FilePrediction:
        """The files ``task_text`` will touch, best first (see the module docstring).

        ``chunk_budget``: awgraph's ``max_results`` (default ``k``) -- the same budget
        knob its README sweeps. ``dirs``: landmarks asked of the localizer. ``seeds`` /
        ``per_seed``: co-change expansion from the top fused hits. ``file_filter``
        restricts every list (e.g. to source files). Never raises.
        """
        task = (task_text or "").strip()
        if not task:
            return FilePrediction(task, (), (), ("task text is empty",))
        ok = file_filter or (lambda _p: True)
        lists: Dict[str, List[str]] = {}

        chunks = self.retrieve_chunks(task, int(chunk_budget or k))
        g_files = [f for f in self._chunk_files(chunks) if ok(f)]
        if g_files:
            lists[AWGRAPH] = g_files
        elif self._graph is not None:
            self._count("awgraph.empty")

        top_dirs, _l_files = self._localize(task, dirs)
        if top_dirs:
            # The prior: the graph's hits that fall in a top directory, in
            # directory-rank order (graph order within a directory).
            ranked_l: List[str] = []
            for d in top_dirs:
                for f in g_files:
                    if (not d or f.startswith(d + "/")) and f not in ranked_l:
                        ranked_l.append(f)
            if ranked_l:
                lists[LOCALIZER] = ranked_l

        first = rrf(lists, self.weights)
        cc = self.cochange
        if cc is not None and cc.commits and first:
            score: Dict[str, float] = defaultdict(float)
            for r, (seed, _s, _v, _rk) in enumerate(first[:max(1, int(seeds))]):
                for partner, p, _n in cc.predict(seed, int(per_seed)):
                    if ok(partner) and self._in_root(partner):
                        score[partner] += p / (r + 1.0)
            if score:
                lists[COCHANGE] = [f for f, _ in sorted(score.items(),
                                                         key=lambda kv: (-kv[1], kv[0]))]
        elif cc is not None and not cc.commits:
            self._degrade("cochange: no history learned (no git, or every commit was bulk)")

        fused = rrf(lists, self.weights)[:max(0, int(k))]
        files = tuple(RankedFile(p, round(s, 6), v, rk) for p, s, v, rk in fused)
        degraded = tuple(d for d in self.telemetry["degraded"]
                         if d.startswith(("awgraph", "localizer", "cochange", "git")))
        return FilePrediction(task, files, tuple(sorted(lists)), degraded, len(chunks),
                              tuple(top_dirs))

    # -- file-seeded (secondary) --------------------------------------------
    def _ensure_index(self) -> bool:
        if self._index is not None:
            return bool(self._index)
        graph = self._open_graph()
        index: Dict[str, List[Any]] = defaultdict(list)
        callees: Dict[str, List[Any]] = defaultdict(list)
        chunks = list(getattr(graph, "chunks", {}).values()) if graph is not None else []
        for c in chunks:
            name = str(getattr(c, "name", "") or "")
            if not name:
                continue
            index[name].append(c)
            if "." in name:
                index[name.split(".")[-1]].append(c)
            for caller in getattr(c, "called_by", []) or []:
                callees[caller].append(c)
        self._index, self._callees = dict(index), dict(callees)
        return bool(self._index)

    def locate(self, symbol: str) -> List[Tuple[str, int]]:
        """``[(relpath, line)]`` of the chunks named ``symbol`` (or with that short name)."""
        if not self._ensure_index():
            return []
        rows = {(self._rel(getattr(c, "source_path", "")), int(getattr(c, "start_line", 0)))
                for c in (self._index or {}).get(symbol, [])}
        return sorted(rows)

    def _targets(self, symbol_or_file: str) -> Tuple[List[Any], set]:
        want = _norm(symbol_or_file)
        by_file = [c for cs in (self._index or {}).values() for c in cs
                   if self._rel(getattr(c, "source_path", "")) == want]
        if by_file:
            uniq = {id(c): c for c in by_file}
            return list(uniq.values()), {want}
        found = (self._index or {}).get(symbol_or_file, [])
        return list(found), {self._rel(getattr(c, "source_path", "")) for c in found}

    def impact(self, symbol_or_file: str, k: int = 20) -> List[Impact]:
        """Files of the callers and callees of a symbol (or of every symbol in a file)."""
        if not self._ensure_index():
            return []
        targets, own = self._targets(symbol_or_file)
        score: Dict[str, float] = defaultdict(float)
        n_callers: Counter = Counter()
        n_callees: Counter = Counter()
        index, callees = self._index or {}, self._callees or {}
        for t in targets:
            for caller in getattr(t, "called_by", []) or []:
                hits = index.get(caller, [])
                for c in hits:
                    f = self._rel(getattr(c, "source_path", ""))
                    if f not in own:
                        score[f] += 1.0 / len(hits)
                        n_callers[f] += 1
            for c in callees.get(getattr(t, "name", ""), []):
                f = self._rel(getattr(c, "source_path", ""))
                if f not in own:
                    score[f] += 1.0 / max(1, len(index.get(getattr(c, "name", ""), [c])))
                    n_callees[f] += 1
        ranked = sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
        return [Impact(f, s, n_callers[f], n_callees[f]) for f, s in ranked]

    def predict_cochange(self, file: str, k: int = 10) -> CoChangePrediction:
        """History first (COCHANGE); the call graph on a history miss (STRUCTURAL); else NONE."""
        f = self._rel(file) if os.path.isabs(file) else _norm(file)
        cc = self.cochange
        # An injected model may span the whole repository: partners outside `root`
        # are dropped here too, not only when the model is learned from git.
        hist = [h for h in (cc.predict(f, k) if cc is not None else [])
                if self._in_root(h[0])]
        if hist:
            return CoChangePrediction(f, COCHANGE, tuple((g, p) for g, p, _ in hist),
                                      support=cc.touched.get(f, 0) if cc else 0)
        imp = self.impact(f, k)
        if imp:
            return CoChangePrediction(f, STRUCTURAL, tuple((i.file, i.score) for i in imp),
                                      support=len(imp), note="no co-change history for this file")
        return CoChangePrediction(f, NONE, (), 0,
                                  note="no co-change history and no call-graph edges")


__all__ = ["AWGRAPH", "COCHANGE", "CoChangeModel", "CoChangePrediction", "CodeWorld",
           "DEFAULT_WEIGHTS", "FilePrediction", "Impact", "LOCALIZER", "Localizer", "NONE",
           "ProspectorLocalizer", "RRF_K", "RankedFile", "STRUCTURAL", "git_toplevel", "rrf"]
