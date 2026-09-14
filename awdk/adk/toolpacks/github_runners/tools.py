"""runners_* tool implementations.

Standalone by design: this pack ships in the wheel and must not import the
monorepo. Everything here is stdlib plus the `gh` CLI, which the operator
already has authenticated if they can see their own Actions.

The doctrine each tool encodes was measured on 2026-08-12, when a repo's CI sat
queued for a day:

  * three of four registrations were GHOSTS with no agent on any host;
  * the real fault was a `runs-on: ubuntu-latest` label mismatch, so more
    runners would not have helped;
  * the one live runner was on a drive being decommissioned.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("github_runners_pack")

_GH_TIMEOUT = 45


def _repo() -> str:
    """OWNER/NAME from env, else the git remote."""
    env = os.environ.get("AITHER_GITHUB_REPO", "").strip()
    if env:
        return env
    try:
        out = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=15,
        )
        url = (out.stdout or "").strip()
        if url:
            slug = url.rstrip("/").removesuffix(".git")
            parts = slug.replace(":", "/").split("/")
            if len(parts) >= 2:
                return f"{parts[-2]}/{parts[-1]}"
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("git remote unreadable during repo detection: %s", exc)
    return ""


def _gh(args: List[str]) -> Dict[str, Any]:
    """Run gh. Returns {ok, data|error} — never raises into the agent loop."""
    if not shutil.which("gh"):
        return {"ok": False, "error": "gh CLI not found on PATH"}
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=_GH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"gh timed out after {_GH_TIMEOUT}s"}
    except OSError as exc:
        return {"ok": False, "error": f"gh failed to execute: {exc}"}
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout or "").strip()[:400]}
    body = (proc.stdout or "").strip()
    if not body:
        return {"ok": True, "data": None}
    try:
        return {"ok": True, "data": json.loads(body)}
    except ValueError:
        return {"ok": True, "data": body}


def _local_installs() -> List[Dict[str, Any]]:
    """Runner directories on THIS host — the fact the GitHub API cannot give you."""
    found: List[Dict[str, Any]] = []
    if platform.system() == "Windows":
        roots = ["C:\\", "D:\\", "E:\\", str(Path.home())]
    else:
        roots = [str(Path.home()), "/opt", "/srv"]
    for root in roots:
        try:
            base = Path(root)
            if not base.is_dir():
                continue
            for child in base.iterdir():
                if child.is_dir() and child.name.startswith("actions-runner"):
                    found.append({
                        "path": str(child),
                        "configured": (child / ".runner").exists(),
                    })
        except (OSError, PermissionError) as exc:
            logger.debug("skipping unreadable root %s: %s", root, exc)
    return found


def runners_status() -> str:
    """Fleet truth: registrations vs live agents vs ghosts.

    A registration with no install directory and no service is a GHOST. It will
    never take a job, and it makes the fleet look larger than it is.
    """
    repo = _repo()
    if not repo:
        return json.dumps({"ok": False,
                           "error": "could not determine repo; set AITHER_GITHUB_REPO"})
    res = _gh(["api", f"repos/{repo}/actions/runners", "--jq", ".runners"])
    if not res["ok"]:
        return json.dumps({"ok": False, "repo": repo, "error": res["error"],
                           "note": "could not judge the fleet — DEAD, not empty"})
    registered = res["data"] or []
    online = [r for r in registered if r.get("status") == "online"]
    offline = [r for r in registered if r.get("status") != "online"]
    return json.dumps({
        "ok": True,
        "repo": repo,
        "registered": len(registered),
        "online": [{"name": r.get("name"), "busy": r.get("busy"),
                    "labels": [x.get("name") for x in (r.get("labels") or [])]}
                   for r in online],
        "offline": [r.get("name") for r in offline],
        "local_installs": _local_installs(),
        "ghost_note": ("An OFFLINE registration with no install directory on this host is "
                       "a ghost — it will never take a job. Check the other hosts before "
                       "deleting; the agent may simply live elsewhere."),
        "durability_note": ("A runner started with run.cmd dies on reboot; only an "
                            "installed service survives."),
    }, indent=2)


def runners_diagnose_queue(workflow: str = "") -> str:
    """Why is CI stuck? Checks the label mismatch BEFORE blaming capacity."""
    repo = _repo()
    if not repo:
        return json.dumps({"ok": False,
                           "error": "could not determine repo; set AITHER_GITHUB_REPO"})
    runners = _gh(["api", f"repos/{repo}/actions/runners", "--jq", ".runners"])
    if not runners["ok"]:
        return json.dumps({"ok": False, "error": runners["error"],
                           "note": "could not judge — DEAD, not healthy"})
    live = [r for r in (runners["data"] or []) if r.get("status") == "online"]
    idle = [r.get("name") for r in live if not r.get("busy")]
    labels = sorted({x.get("name") for r in live for x in (r.get("labels") or [])})

    args = ["run", "list", "--limit", "12", "--json",
            "status,conclusion,workflowName,createdAt"]
    if workflow:
        args += ["--workflow", workflow]
    runs = _gh(args)
    stuck = []
    if runs["ok"] and isinstance(runs["data"], list):
        stuck = [r for r in runs["data"]
                 if r.get("status") in ("queued", "pending", "waiting")]

    findings = []
    if stuck and idle:
        findings.append(
            f"{len(stuck)} run(s) queued while {len(idle)} runner(s) are ONLINE and IDLE. "
            "That is almost always a LABEL MISMATCH, not a capacity shortage. Read the "
            "workflow's `runs-on`: a job asking for ubuntu-latest will NEVER match a "
            "self-hosted runner."
        )
    if stuck and not live:
        findings.append(
            f"{len(stuck)} run(s) queued and NO runner online — the agents are down, or "
            "the jobs want GitHub-hosted capacity."
        )
    if not stuck:
        findings.append("No queued runs — nothing is stuck right now.")

    return json.dumps({
        "ok": True, "repo": repo,
        "queued": [{"workflow": r.get("workflowName"), "created": r.get("createdAt")}
                   for r in stuck],
        "online_idle_runners": idle,
        "available_labels": labels,
        "findings": findings,
    }, indent=2)


def runners_delete_ghost(name: str, confirm: bool = False) -> str:
    """Delete a runner REGISTRATION. Refuses online runners; needs confirm=True."""
    if not name:
        return json.dumps({"ok": False, "error": "name is required"})
    repo = _repo()
    if not repo:
        return json.dumps({"ok": False, "error": "could not determine repo"})
    got = _gh(["api", f"repos/{repo}/actions/runners", "--jq",
               f'.runners[] | select(.name=="{name}")'])
    if not got["ok"] or not got["data"]:
        return json.dumps({"ok": False, "error": f"no registration named {name!r}"})
    entry = got["data"] if isinstance(got["data"], dict) else {}
    status = entry.get("status")
    if status == "online":
        return json.dumps({
            "ok": False, "refused": True, "name": name,
            "error": ("refusing to delete an ONLINE runner — stop its agent first, or you "
                      "remove working capacity"),
        }, indent=2)
    if not confirm:
        return json.dumps({
            "ok": False, "refused": True, "name": name, "status": status,
            "error": "confirm=True required",
            "warning": ("Offline is not proof of a ghost — the host may just be powered "
                        "off. Check for an install directory on that machine first."),
        }, indent=2)
    res = _gh(["api", "-X", "DELETE", f"repos/{repo}/actions/runners/{entry.get('id')}"])
    return json.dumps({"ok": res["ok"], "deleted": name, "error": res.get("error")},
                      indent=2)


def runners_setup_playbook(count: int = 1, runtime_drive: str = "E:") -> str:
    """Exact commands to add N runners, plus the four traps that break the build.

    Instructions, not execution: provisioning installs a service, needs elevation
    and is long and interactive. A tool that half-completes it leaves you worse
    off than one working runner.
    """
    repo = _repo() or "<OWNER>/<REPO>"
    steps = []
    for i in range(2, 2 + max(1, count)):
        d = f"{runtime_drive}\\actions-runner-{i}"
        steps.append({
            "runner": f"runner-{i}",
            "dir": d,
            "commands": [
                f"New-Item -ItemType Directory {d} -Force",
                f"Copy-Item <EXISTING_RUNNER_DIR>\\* {d} -Recurse -Force",
                (f"Get-ChildItem {d} -Force | Where-Object Name -in '.runner',"
                 "'.credentials','.credentials_rsaparams','.path','.env',"
                 "'.runner_migrated','_diag','_work' | Remove-Item -Recurse -Force"),
                f"cd {d}",
                (f"$tok = gh api -X POST repos/{repo}/actions/runners/"
                 "registration-token --jq .token"),
                (f"./config.cmd --unattended --url https://github.com/{repo} "
                 f"--token $tok --name runner-{i} "
                 "--labels 'self-hosted,Windows,X64' --work '_work' --replace"),
                "# ELEVATED shell, so it survives reboot:",
                "./svc.cmd install ; ./svc.cmd start",
            ],
        })
    return json.dumps({
        "ok": True, "repo": repo, "steps": steps,
        "traps": [
            "Copy-Item -Exclude does NOT filter through -Recurse, so a copied package "
            "inherits the SOURCE runner's .runner/.credentials and config.cmd refuses "
            "with 'already configured'. Strip them AFTER copying.",
            "A package that has been through a version upgrade also carries "
            "`.runner_migrated`, which gives the IDENTICAL message after the obvious "
            "two are gone — it reads as 'my strip failed' when it did not.",
            "config.cmd registers unelevated; svc.cmd REQUIRES elevation. A runner "
            "started with run.cmd is real capacity today and gone after reboot.",
            "The registration token is short-lived and single-use. Mint one PER runner, "
            "inline, and never write it to a file — it authorises adding a machine.",
        ],
        "before_you_start": ("Run runners_diagnose_queue first. If jobs queue while a "
                             "runner idles, the fault is the workflow's `runs-on` and "
                             "more runners will not fix it."),
        "placement": (f"Install on {runtime_drive}. Do not put CI capacity on a disk "
                      "being decommissioned."),
    }, indent=2)


TOOLS = [
    runners_status,
    runners_diagnose_queue,
    runners_delete_ghost,
    runners_setup_playbook,
]
