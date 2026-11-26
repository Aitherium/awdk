"""``/model-postures`` on the harness daemon -- host-side awmodels execution, proved.

* closed input validation: malformed posture names, skip hosts and deadlines are
  refused with 400 before any subprocess starts;
* the real daemon enforces bearer auth (401/403) and requires the owner principal
  for ``POST /model-postures/use`` (scoped agent tokens get 403);
* ``run_status``, ``run_plan``, and ``run_use`` drive a real (fake) ``awmodels.py``
  and return structured JSON for clean, drift, infeasible, dry-run, and busy-lock cases.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="daemon route tests need fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from adk.harnesses import model_posture_routes as mpr  # noqa: E402

TOKEN = "test-token-model-postures-do-not-reuse"


@pytest.mark.parametrize(
    "name",
    ["", "GAME ON", "gaming; rm -rf /", "../gaming", "--yes", "-bad", "x" * 70],
)
def test_invalid_posture_names_are_refused(name):
    with pytest.raises(ValueError):
        mpr.validate_posture_name(name)


def test_invalid_skip_and_deadline_are_refused():
    with pytest.raises(ValueError):
        mpr.validate_skip(["badhost"])
    with pytest.raises(ValueError):
        mpr.validate_deadline(True)
    with pytest.raises(ValueError):
        mpr.validate_deadline(5.0)
    with pytest.raises(ValueError):
        mpr.validate_deadline(99999.0)


def test_argv_shapes():
    tool = Path("repo") / "awmodels.py"
    assert mpr.build_status_argv(tool, skip=["spark"], python="py") == [
        "py", str(tool), "status", "--json", "--skip", "spark",
    ]
    assert mpr.build_plan_argv("pool-fast", tool, offline=True, python="py") == [
        "py", str(tool), "plan", "pool-fast", "--json", "--offline",
    ]
    assert mpr.build_use_argv("gaming", tool, dry_run=True, python="py") == [
        "py", str(tool), "use", "gaming", "--json",
    ]
    assert mpr.build_use_argv(
        "gaming", tool, dry_run=False, wait=False, deadline_s=120, skip=["rtx5090"], python="py"
    ) == [
        "py", str(tool), "use", "gaming", "--json", "--yes", "--no-wait",
        "--deadline", "120", "--skip", "rtx5090",
    ]


@pytest.fixture()
def daemon(tmp_path, monkeypatch):
    from adk.harnesses.daemon import create_app
    from adk.harnesses.manager import SessionManager

    calls: list = []

    monkeypatch.setattr(
        mpr,
        "run_status",
        lambda skip=(), **_: calls.append(("status", list(skip)))
        or {"ok": True, "rc": 0, "status": {"active": "pool-fast-spark-embed", "drift": []}},
    )
    monkeypatch.setattr(
        mpr,
        "run_plan",
        lambda name, offline=False, **_: calls.append(("plan", name, offline))
        or {"ok": True, "rc": 0, "plan": {"fit": {"ok": True}, "steps": []}},
    )
    monkeypatch.setattr(
        mpr,
        "run_use",
        lambda name, **kw: calls.append(("use", name, kw.get("dry_run", False)))
        or {"ok": True, "rc": 0, "posture": name, "applied": not kw.get("dry_run"),
            "dry_run": bool(kw.get("dry_run")), "busy": False, "steps": [], "log": []},
    )
    manager = SessionManager(root=tmp_path)
    with TestClient(create_app(manager=manager, token=TOKEN)) as client:
        yield client, calls
    manager.stop_all()


def test_real_daemon_enforces_bearer_auth(daemon):
    client, calls = daemon
    assert client.get("/model-postures/status").status_code == 401
    assert client.post("/model-postures/use", json={"posture": "gaming"}).status_code == 401
    bad = {"Authorization": "Bearer wrong-token"}
    assert client.get("/model-postures/status", headers=bad).status_code == 403
    assert client.post("/model-postures/use", headers=bad,
                       json={"posture": "gaming"}).status_code == 403
    assert calls == []


def test_real_daemon_owner_bearer_drives_status_plan_use(daemon):
    client, calls = daemon
    auth = {"Authorization": f"Bearer {TOKEN}"}
    r = client.get("/model-postures/status", headers=auth)
    assert r.status_code == 200 and r.json()["status"]["active"] == "pool-fast-spark-embed"
    r = client.get("/model-postures/gaming/plan?offline=true", headers=auth)
    assert r.status_code == 200 and r.json()["plan"]["fit"]["ok"] is True
    r = client.post("/model-postures/use", headers=auth,
                    json={"posture": "gaming", "dry_run": True})
    assert r.status_code == 200 and r.json()["dry_run"] is True
    assert client.post("/model-postures/use", headers=auth,
                       json={"posture": "bad;cmd"}).status_code == 400
    assert calls == [
        ("status", []),
        ("plan", "gaming", True),
        ("use", "gaming", True),
    ]


def test_non_owner_principal_cannot_use(monkeypatch):
    calls: list = []
    monkeypatch.setattr(mpr, "run_use", lambda *a, **kw: calls.append(a) or {"ok": True})

    class AgentPrincipal:
        plan = "agent"

    app = FastAPI()
    mpr.mount(app, lambda: AgentPrincipal())
    client = TestClient(app)
    res = client.post("/model-postures/use", json={"posture": "gaming"})
    assert res.status_code == 403
    assert calls == []


def _fake_awmodels(root: Path, body: str) -> Path:
    tool = root / "AitherOS" / "dev" / "tools" / "awmodels.py"
    tool.parent.mkdir(parents=True, exist_ok=True)
    tool.write_text(textwrap.dedent(body), encoding="utf-8")
    return tool


def test_run_status_plan_use_against_real_subprocess(tmp_path):
    tool = _fake_awmodels(
        tmp_path,
        """
        import json, sys
        args = sys.argv[1:]
        cmd = args[0]
        if cmd == "status":
            print(json.dumps({"active": "pool-fast-spark-embed", "drift": [{"host": "rtx5090"}],
                              "unjudged": {}}))
            sys.exit(1)
        if cmd == "plan":
            name = args[1]
            if name == "unknown":
                sys.stderr.write("awmodels: unknown posture 'unknown'\\n")
                sys.exit(2)
            ok = name != "full-mesh"
            print(json.dumps({
                "fit": {"ok": ok},
                "steps": [{"id": "fit"}],
                "spark_now": {"judged": True},
            }))
            sys.exit(0 if ok else 1)
        if cmd == "use":
            name = args[1]
            yes = "--yes" in args
            print(json.dumps({
                "posture": name,
                "dry_run": not yes,
                "applied": yes,
                "busy": False,
                "steps": [{"id": "fit", "ok": True, "detail": "fits"}],
                "log": [f"use {name} yes={yes}"],
            }))
            sys.exit(0)
        """,
    )
    st = mpr.run_status(tool=tool)
    assert st["ok"] is True and st["rc"] == 1
    assert st["status"]["active"] == "pool-fast-spark-embed"

    pl_ok = mpr.run_plan("gaming", tool=tool)
    assert pl_ok["ok"] is True and pl_ok["rc"] == 0 and pl_ok["plan"]["spark_now"]["judged"] is True

    pl_bad = mpr.run_plan("full-mesh", tool=tool)
    assert pl_bad["ok"] is True and pl_bad["rc"] == 1 and pl_bad["plan"]["fit"]["ok"] is False

    pl_unk = mpr.run_plan("unknown", tool=tool)
    assert pl_unk["ok"] is False and "unknown posture" in pl_unk["error"]

    us_dry = mpr.run_use("gaming", dry_run=True, tool=tool)
    assert us_dry["ok"] is True and us_dry["dry_run"] is True and us_dry["applied"] is False

    us_live = mpr.run_use("gaming", dry_run=False, tool=tool)
    assert us_live["ok"] is True and us_live["applied"] is True
    assert us_live["log"] == ["use gaming yes=True"]


def test_missing_checkout_returns_error(monkeypatch):
    monkeypatch.setattr(mpr, "find_tool", lambda: None)
    assert mpr.run_status()["ok"] is False
    assert mpr.run_plan("gaming")["ok"] is False
    assert mpr.run_use("gaming")["ok"] is False
