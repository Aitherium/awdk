"""Flow -- awflow's journaled workflows, exposed as agent tools.

WHAT THIS IS FOR

awflow is a deterministic workflow runtime: every ``agent()`` call a workflow
script makes is hashed and appended (fsynced) to a per-run journal, and a run
started again under the same run id replays the answered calls from that journal
instead of paying for them twice. Until this pack its only caller was
``awrun/_flow_runner.py``; an agent could not start, list or resume a workflow.

    flow_run(script_path, args=None, budget=None)  start a new run
    flow_list()                                     the runs on this machine
    flow_resume(run_id)                             continue a run from its journal

A workflow script is a Python file defining an async function (``main`` by
default) that uses awflow's primitives (``agent``, ``parallel``, ``pipeline``,
``phase``, ``log``). ``args`` become that function's keyword arguments.
flow_run executes the script in this process -- it is code execution, which is
why the pack is never exposed over A2A (the registry default) and is opt-in.

Every run stores ``flow_meta.json`` next to its ``journal.jsonl`` (script path,
function, args, budget) so ``flow_resume`` needs only the run id. The determinism
hash is awflow's, not ours: a resumed script that asks the same questions in the
same order gets the journaled answers back; a changed question is asked live.

THE RULE: every tool returns a dict with ``ok``. A missing awflow is
``ok: False`` naming the package (``pip install awdk[flow]``), never an empty
result an agent would read as "there were no runs".
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

PACK_ID = "flow"
META_FILE = "flow_meta.json"
JOURNAL_FILE = "journal.jsonl"
DEFAULT_BUDGET = 1_000_000

#: Replaces the live model call. Tests only -- nothing in the pack sets it, and a
#: tool caller cannot reach it (it is not a parameter).
_DISPATCHER: Any = None


def _missing(exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "error": f"awflow is not available: {exc}",
        "hint": ("Install it: pip install awdk[flow]  (distribution aitherium-awflow; "
                 "from a source checkout: pip install -e <the awflow package directory>)"),
    }


def journal_root() -> Path:
    """Where runs live: $AWFLOW_JOURNAL_ROOT, else /data/workflows when mounted
    (fleet containers), else ~/awflow -- the same fallback run_workflow uses."""
    env = os.environ.get("AWFLOW_JOURNAL_ROOT", "").strip()
    if env:
        return Path(env)
    fleet = Path("/data/workflows")
    if fleet.is_dir():
        return fleet
    return Path.home() / "awflow"


def _load_script(script_path: Path, function: str):
    spec = importlib.util.spec_from_file_location(
        f"_awflow_script_{uuid.uuid4().hex[:8]}", script_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(script_path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        if str(script_path.parent) in sys.path:
            sys.path.remove(str(script_path.parent))
    fn = getattr(module, function, None)
    if fn is None or not callable(fn):
        raise ValueError(f"{script_path} defines no callable {function!r}")
    return fn


def _read_journal(path: Path) -> list[dict[str, Any]]:
    """Valid records only; a torn or corrupt line is skipped, as awflow does."""
    out: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        return []
    return out


def _answered_calls(records: list[dict[str, Any]]) -> int:
    return sum(1 for r in records
               if r.get("type") == "AGENT_CALL" and r.get("response") is not None)


def _status(records: list[dict[str, Any]]) -> str:
    for rec in reversed(records):
        kind = rec.get("type")
        if kind == "WORKFLOW_END":
            return str(rec.get("status") or "completed")
        if kind == "ERROR":
            return "failed"
    return "incomplete" if records else "empty"


async def _execute(run_id: str, meta: dict[str, Any], *, resume: bool) -> dict[str, Any]:
    try:
        from awflow import run_workflow
    except Exception as exc:  # noqa: BLE001 - absent package is a refusal, not a crash
        return _missing(exc)

    root = journal_root()
    run_dir = root / run_id
    script_path = Path(meta["script_path"])
    try:
        fn = _load_script(script_path, meta.get("function") or "main")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "run_id": run_id, "error": f"cannot load script: {exc}"}

    kwargs = dict(meta.get("args") or {})

    async def workflow():
        return await fn(**kwargs)

    workflow.__name__ = getattr(fn, "__name__", "workflow")

    before = _read_journal(run_dir / JOURNAL_FILE)
    extra: dict[str, Any] = {}
    if _DISPATCHER is not None:
        extra["dispatcher"] = _DISPATCHER
    try:
        result = await run_workflow(
            workflow, journal_path=root, resume_from=run_id,
            budget_tokens=int(meta.get("budget") or DEFAULT_BUDGET), **extra)
        ok, error = True, None
    except Exception as exc:  # noqa: BLE001 - the workflow's failure is this run's result
        result, ok, error = None, False, f"{type(exc).__name__}: {exc}"
    after = _read_journal(run_dir / JOURNAL_FILE)

    out: dict[str, Any] = {
        "ok": ok,
        "run_id": run_id,
        "resumed": resume,
        "journal": str(run_dir / JOURNAL_FILE),
        # Answered calls already in the journal = what this start could replay;
        # live_calls = AGENT_CALL records this start appended (replays write none).
        "replayable_calls": _answered_calls(before),
        "live_calls": sum(1 for r in after[len(before):] if r.get("type") == "AGENT_CALL"),
        "status": _status(after),
    }
    if ok:
        try:
            json.dumps(result)
            out["result"] = result
        except (TypeError, ValueError):
            out["result"] = str(result)
    else:
        out["error"] = error
    return out


async def flow_run(script_path: str, args: Optional[dict] = None,
                   budget: Optional[int] = None) -> dict[str, Any]:
    """Run a journaled awflow workflow script; returns its run_id and result.

    script_path: path to a .py file defining an async ``main`` that uses awflow primitives
    args: keyword arguments passed to main (JSON object)
    budget: hard token budget for the run (default 1000000)
    """
    if importlib.util.find_spec("awflow") is None:
        return _missing(ModuleNotFoundError("No module named 'awflow'"))
    path = Path(str(script_path or "")).expanduser()
    if not str(script_path or "").strip() or not path.is_file():
        return {"ok": False, "error": f"script_path is not a file: {script_path!r}"}
    if args is not None and not isinstance(args, dict):
        return {"ok": False, "error": "args must be a JSON object (dict)"}
    if budget is not None:
        try:
            budget = int(budget)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"budget must be an integer, got {budget!r}"}
        if budget <= 0:
            return {"ok": False, "error": "budget must be positive"}
    try:
        json.dumps(args or {})
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": f"args must be JSON-serialisable: {exc}"}

    run_id = str(uuid.uuid4())
    meta = {
        "script_path": str(path.resolve()),
        "function": "main",
        "args": args or {},
        "budget": budget or DEFAULT_BUDGET,
    }
    run_dir = journal_root() / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / META_FILE).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"journal root not writable ({run_dir}): {exc}"}
    return await _execute(run_id, meta, resume=False)


def flow_list() -> dict[str, Any]:
    """List workflow runs in the journal root, newest first, with status and call counts."""
    if importlib.util.find_spec("awflow") is None:
        return _missing(ModuleNotFoundError("No module named 'awflow'"))
    root = journal_root()
    runs: list[dict[str, Any]] = []
    if root.is_dir():
        for run_dir in root.iterdir():
            jpath = run_dir / JOURNAL_FILE
            if not jpath.is_file():
                continue
            records = _read_journal(jpath)
            meta: dict[str, Any] = {}
            try:
                meta = json.loads((run_dir / META_FILE).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                # A run awrun or a script started has no meta; it lists, not resumes.
                logger.debug("flow_list: %s has no readable %s (%s)", run_dir.name, META_FILE, exc)
            start = next((r for r in records if r.get("type") == "WORKFLOW_START"), {})
            runs.append({
                "run_id": run_dir.name,
                "status": _status(records),
                "script": meta.get("script_path") or start.get("script_name"),
                "started": start.get("timestamp"),
                "agent_calls": sum(1 for r in records if r.get("type") == "AGENT_CALL"),
                "resumable": bool(meta.get("script_path")),
                "_mtime": jpath.stat().st_mtime,
            })
    runs.sort(key=lambda r: r.pop("_mtime"), reverse=True)
    return {"ok": True, "root": str(root), "count": len(runs), "runs": runs}


async def flow_resume(run_id: str) -> dict[str, Any]:
    """Resume a workflow run from its journal; answered agent calls replay, not re-run.

    run_id: the run id returned by flow_run or listed by flow_list
    """
    if importlib.util.find_spec("awflow") is None:
        return _missing(ModuleNotFoundError("No module named 'awflow'"))
    rid = str(run_id or "").strip()
    if not rid or rid != Path(rid).name or rid in (".", ".."):
        return {"ok": False, "error": f"invalid run_id: {run_id!r}"}
    run_dir = journal_root() / rid
    if not (run_dir / JOURNAL_FILE).is_file():
        return {"ok": False, "run_id": rid,
                "error": f"no journal for run {rid} under {run_dir.parent}"}
    try:
        meta = json.loads((run_dir / META_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "run_id": rid,
                "error": f"run {rid} has no {META_FILE}; it was not started by flow_run, "
                         "so its script is unknown"}
    if not isinstance(meta, dict) or not meta.get("script_path"):
        return {"ok": False, "run_id": rid, "error": f"{META_FILE} names no script_path"}
    return await _execute(rid, meta, resume=True)


_TOOLS = [
    (flow_run, "flow_run",
     "Run a journaled awflow workflow script (replayable, budgeted); returns run_id and result."),
    (flow_list, "flow_list", "List awflow workflow runs on this machine with status."),
    (flow_resume, "flow_resume",
     "Resume an awflow run from its journal; answered calls replay instead of re-running."),
]


def register(registry) -> int:
    """Register the flow tools. Registered even when awflow is absent: present and
    honestly refusing beats absent and silent."""
    n = 0
    for fn, name, desc in _TOOLS:
        try:
            registry.register(fn, name=name, description=desc)
            n += 1
        except TypeError:
            try:
                registry.register(fn)
                n += 1
            except Exception as exc:  # noqa: BLE001 - one bad tool must not block the pack
                logger.debug("flow pack: skip %s (%s)", name, exc)
        except Exception as exc:  # noqa: BLE001
            logger.debug("flow pack: skip %s (%s)", name, exc)
    logger.info("Flow pack registered %d tools", n)
    return n
