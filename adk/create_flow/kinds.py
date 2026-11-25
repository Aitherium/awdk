"""The six kinds, each bound to the provisioning path that ALREADY exists.

A kind never invents a way to create something. It names the existing route/tool,
previews what that path will produce, runs it, and verifies with the path's own
validator. ``executable = False`` kinds still plan and preview; approving them is
refused with the reason, so a "planned" kind can never be mistaken for a working one.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from .backends import Backend

logger = logging.getLogger("adk.create_flow")

PILLARS = ("intent", "context", "reasoning", "orchestration", "creation", "learning")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
MARKER = ".create-flow.json"


def slugify(text: str, *, limit: int = 32) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    s = s[:limit].strip("-")
    return s or "untitled"


@dataclass
class Ctx:
    plan: dict[str, Any]
    backend: Optional[Backend]
    pack_root: Optional[Path]
    docs: list[dict[str, str]]


class KindError(RuntimeError):
    """A step failed; the message is shown to the owner verbatim."""


def _need_backend(ctx: Ctx) -> Backend:
    if ctx.backend is None:
        raise KindError(
            "no platform connection: sign in (aither login) so this can be created as you"
        )
    return ctx.backend


def _check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


# -- workspace ---------------------------------------------------------------


class WorkspaceKind:
    key = "workspace"
    label = "Workspace"
    executable = True
    path = "POST /workspace (routers/workspace.py); same gates as the workspace switcher"
    undo_note = "Archives the workspace (PATCH status=archived). Nothing is deleted."

    async def existing(self, ctx: Ctx) -> list[str]:
        if ctx.backend is None:
            return []
        r = await ctx.backend.call("GET", "/workspace")
        if not r.ok or not isinstance(r.body, dict):
            ctx.plan.setdefault("unchecked", []).append("existing workspaces could not be listed")
            return []
        return [str(w.get("slug", "")) for w in r.body.get("workspaces", []) if isinstance(w, dict)]

    def draft(self, ctx: Ctx) -> dict[str, Any]:
        p = ctx.plan
        cfg = {"slug": p["slug"], "display_name": p["name"], "description": p["description"][:500]}
        return {
            "files": [],
            "config": cfg,
            "tools": ["POST /workspace", "GET /workspace/{id}"],
            "cost": {
                "usd": 0,
                "note": "Workspaces are free. Compute on Aitherium hardware is billed "
                "only when an agent runs there.",
            },
            "where": {
                "runtime": "genesis",
                "stores": "workspace store (tenant-scoped)",
                "durable_on_awnix": False,
            },
            "validators": [
                "slug rule ^[a-z0-9][a-z0-9-]{1,31}$",
                "no existing workspace with this slug",
                "GET /workspace/{id} shows it active",
            ],
            "steps": [
                "Create the workspace in your tenant",
                "Read it back and confirm it is active",
            ],
        }

    async def execute(self, ctx: Ctx) -> dict[str, Any]:
        b = _need_backend(ctx)
        cfg = ctx.plan["preview"]["config"]
        r = await b.call("POST", "/workspace", cfg)
        if not r.ok:
            raise KindError(f"workspace create refused: {r.detail()}")
        ws = (r.body or {}).get("workspace", {}) if isinstance(r.body, dict) else {}
        wid = ws.get("workspace_id") or ws.get("id") or ""
        if not wid:
            raise KindError("workspace create answered without an id")
        return {
            "workspace_id": wid,
            "slug": ws.get("slug", cfg["slug"]),
            "link": f"/workspace?workspace={wid}",
        }

    async def verify(self, ctx: Ctx) -> list[dict[str, Any]]:
        b = _need_backend(ctx)
        wid = ctx.plan["result"]["workspace_id"]
        r = await b.call("GET", f"/workspace/{quote(str(wid), safe='')}")
        ws = (r.body or {}).get("workspace", {}) if r.ok and isinstance(r.body, dict) else {}
        return [
            _check("read back", r.ok, r.detail() if not r.ok else ""),
            _check("active", ws.get("status") == "active", str(ws.get("status", ""))),
        ]

    async def undo(self, ctx: Ctx) -> dict[str, Any]:
        b = _need_backend(ctx)
        wid = (ctx.plan.get("result") or {}).get("workspace_id")
        if not wid:
            raise KindError("nothing was created, so there is nothing to archive")
        r = await b.call("PATCH", f"/workspace/{quote(str(wid), safe='')}", {"status": "archived"})
        if not r.ok:
            raise KindError(f"archive refused: {r.detail()}")
        ws = (r.body or {}).get("workspace", {}) if isinstance(r.body, dict) else {}
        return {"archived": True, "status": ws.get("status", "archived")}


# -- agent -------------------------------------------------------------------


class AgentKind:
    key = "agent"
    label = "Agent"
    executable = True
    path = "POST /agents/compose (routers/agent_compose.py); tenant-scoped identity + soul"
    undo_note = "Removes the agent's identity and soul files (DELETE /agents/compose/{name})."

    async def existing(self, ctx: Ctx) -> list[str]:
        if ctx.backend is None:
            return []
        r = await ctx.backend.call("GET", "/agents/compose")
        if not r.ok or not isinstance(r.body, dict):
            ctx.plan.setdefault("unchecked", []).append("existing agents could not be listed")
            return []
        return [str(a.get("name", "")) for a in r.body.get("agents", []) if isinstance(a, dict)]

    async def preflight(self, ctx: Ctx) -> list[str]:
        """A chosen workspace must be one this caller can already read (tenant-scoped)."""
        wid = ctx.plan.get("scope", {}).get("workspace_id", "")
        if not wid:
            return []
        if ctx.backend is None:
            return ["cannot check the workspace without a platform connection"]
        r = await ctx.backend.call("GET", f"/workspace/{quote(str(wid), safe='')}")
        if not r.ok:
            return [f"workspace {wid!r} is not one of yours"]
        return []

    def draft(self, ctx: Ctx) -> dict[str, Any]:
        p = ctx.plan
        opts = p.get("options", {})
        name = p["slug"]
        soul = (
            opts.get("soul_text")
            or f"# {p['name']}\n\n{p['description'].strip()}\n\n"
            "## How you work\n- Stay inside the workspace you were created in.\n"
            "- Say what you did and what you could not do.\n"
            "- Ask before anything irreversible, paid or public.\n"
        )
        cfg = {
            "name": name,
            "description": p["description"][:500],
            "soul_text": soul,
            "tools": list(opts.get("tools") or []),
            "effort_cap": int(opts.get("effort_cap") or 6),
            "workspace_id": p.get("scope", {}).get("workspace_id", ""),
            "visibility": "workspace",
            "deploy_target": "genesis",
            "created_via": "create_flow",
        }
        return {
            "files": [
                f"config/identities/tenants/<your tenant>/{name}.yaml",
                f"config/souls/tenants/<your tenant>/{name}.md",
            ],
            "config": cfg,
            "tools": ["POST /agents/compose", "GET /agents/compose/{name}"],
            "cost": {
                "usd": 0,
                "note": "Creating an agent is free. Its model calls are metered "
                "by your plan when it works.",
            },
            "where": {
                "runtime": "genesis",
                "stores": "tenant identity directory",
                "durable_on_awnix": False,
            },
            "validators": [
                "name rule ^[a-z0-9-]{1,64}$",
                "agent quota (ACTA)",
                "name not already yours or the platform's",
            ],
            "steps": [
                "Write the agent's identity and soul in your tenant",
                "Read it back and confirm its workspace",
            ],
        }

    async def execute(self, ctx: Ctx) -> dict[str, Any]:
        b = _need_backend(ctx)
        r = await b.call("POST", "/agents/compose", ctx.plan["preview"]["config"])
        if not r.ok:
            raise KindError(f"agent create refused: {r.detail()}")
        body = r.body if isinstance(r.body, dict) else {}
        name = body.get("name", ctx.plan["slug"])
        return {
            "agent": name,
            "workspace_id": ctx.plan["preview"]["config"]["workspace_id"],
            "link": f"/agents/{name}",
        }

    async def verify(self, ctx: Ctx) -> list[dict[str, Any]]:
        b = _need_backend(ctx)
        name = ctx.plan["result"]["agent"]
        r = await b.call("GET", f"/agents/compose/{quote(str(name), safe='')}")
        ident = (r.body or {}).get("identity", {}) if r.ok and isinstance(r.body, dict) else {}
        want = ctx.plan["preview"]["config"]["workspace_id"]
        checks = [_check("read back", r.ok, r.detail() if not r.ok else "")]
        if want:
            checks.append(
                _check(
                    "in the chosen workspace",
                    ident.get("workspace_id") == want,
                    str(ident.get("workspace_id", "")),
                )
            )
        return checks

    async def undo(self, ctx: Ctx) -> dict[str, Any]:
        b = _need_backend(ctx)
        name = (ctx.plan.get("result") or {}).get("agent")
        if not name:
            raise KindError("nothing was created, so there is nothing to remove")
        r = await b.call("DELETE", f"/agents/compose/{quote(str(name), safe='')}")
        if not r.ok:
            raise KindError(f"remove refused: {r.detail()}")
        return {"removed": name}


# -- file-backed packs (skill pack, tool pack) -------------------------------


def _yaml_scalar(text: str) -> str:
    """One line, double-quoted, so a ':' or '#' in a person's words cannot break the YAML."""
    return json.dumps(" ".join(str(text).split()), ensure_ascii=False)


def _under(root: Path, target: Path) -> Path:
    root_r, target_r = Path(root).resolve(), Path(target).resolve()
    if target_r != root_r and root_r not in target_r.parents:
        raise KindError("refusing to write outside the pack directory")
    return target_r


def _remove_marked_tree(root: Optional[Path], pack_dir: Path, plan_id: str) -> list[str]:
    """Delete exactly the files of a pack THIS plan made. No recursive delete: walk,
    unlink files, rmdir empties, and only when the marker names this plan."""
    if root is None:
        raise KindError("this runtime has no pack directory configured")
    try:
        info = json.loads((pack_dir / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise KindError("this folder was not made by this plan; leaving it alone")
    if info.get("plan_id") != plan_id:
        raise KindError("this folder belongs to a different plan; leaving it alone")
    _under(root, pack_dir)
    removed: list[str] = []
    for dirpath, dirnames, filenames in os.walk(pack_dir, topdown=False):
        for fn in filenames:
            fp = Path(dirpath) / fn
            fp.unlink()
            removed.append(str(fp.relative_to(pack_dir)).replace("\\", "/"))
        for dn in dirnames:
            try:
                (Path(dirpath) / dn).rmdir()
            except OSError as exc:  # not empty: somebody else's file, leave the dir
                logger.debug("left %s in place: %s", dn, exc)
    pack_dir.rmdir()
    return sorted(removed)


def _shown(ctx: Ctx, d: Path) -> str:
    """The absolute path only on the owner's own machine; on a shared server, the pack id."""
    return str(d) if ctx.plan.get("runtime") == "awdk" else d.name


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _list_dirs(root: Optional[Path]) -> list[str]:
    if root is None or not Path(root).is_dir():
        return []
    return sorted(p.name for p in Path(root).iterdir() if p.is_dir())


class SkillPackKind:
    key = "skill_pack"
    label = "Skill pack"
    executable = True
    path = "SKILL.md files under <packs>/<id>/skills/, validated by adk.harnesses.skills_local"
    undo_note = "Removes exactly the files this plan wrote."

    def _dir(self, ctx: Ctx) -> Path:
        if ctx.pack_root is None:
            raise KindError("this runtime has no pack directory configured")
        return _under(ctx.pack_root, Path(ctx.pack_root) / ctx.plan["slug"])

    async def existing(self, ctx: Ctx) -> list[str]:
        return _list_dirs(ctx.pack_root)

    def _skills(self, ctx: Ctx) -> list[dict[str, str]]:
        p = ctx.plan
        names = [
            slugify(n) for n in (p.get("options", {}).get("skills") or []) if str(n).strip()
        ] or [p["slug"]]
        ref = "\n\n".join(f"### {d['name']}\n{d['text'][:3000]}" for d in ctx.docs if d.get("text"))
        first = p["description"].strip().splitlines()[0][:180] if p["description"].strip() else ""
        out = []
        for n in names:
            title = n.replace("-", " ").title()
            body = (
                f"---\nname: {n}\ndescription: {_yaml_scalar(first or title)}\n---\n"
                f"# {title}\n\n{p['description'].strip()}\n\n## When to use\n"
                "When the ask matches the description above.\n\n"
                "## Steps\n1. Restate the goal in one line.\n"
                "2. Do the smallest reversible step first.\n"
                "3. Check the result against the goal and say what you verified.\n"
            )
            if ref:
                body += f"\n## Reference\n{ref}\n"
            out.append({"name": n, "path": f"skills/{n}/SKILL.md", "text": body})
        return out

    def draft(self, ctx: Ctx) -> dict[str, Any]:
        skills = self._skills(ctx)
        slug = ctx.plan["slug"]
        return {
            "files": [f"{slug}/pack.yaml"] + [f"{slug}/{s['path']}" for s in skills],
            "config": {
                "pack_id": slug,
                "kind": "skill-pack",
                "skills": [s["name"] for s in skills],
            },
            "contents": {f"{slug}/{s['path']}": s["text"] for s in skills},
            "tools": ["adk.harnesses.skills_local.discover"],
            "cost": {"usd": 0, "note": "Files only. Nothing runs until an agent loads the skill."},
            "where": {
                "runtime": ctx.plan["runtime"],
                "stores": "the pack directory of this runtime",
                "durable_on_awnix": False,
            },
            "validators": [
                "frontmatter name + description present",
                "skills_local.discover finds every declared skill",
            ],
            "steps": [
                "Write pack.yaml and each SKILL.md",
                "Discover the pack with the real skill loader",
            ],
        }

    async def execute(self, ctx: Ctx) -> dict[str, Any]:
        d = self._dir(ctx)
        if d.exists():
            raise KindError(f"a pack named {ctx.plan['slug']!r} already exists here")
        skills = self._skills(ctx)
        # The marker goes FIRST: if a write below fails, the same marker-gated remover that
        # undo uses clears the half-made folder, so nothing is left blocking the name.
        _write(
            d / MARKER,
            json.dumps(
                {"plan_id": ctx.plan["id"], "kind": "skill_pack", "created_at": time.time()}
            ),
        )
        try:
            _write(
                d / "pack.yaml",
                f"name: {ctx.plan['slug']}\nkind: skill-pack\ncreated_by: create_flow\n"
                f"plan_id: {ctx.plan['id']}\n"
                "skills:\n" + "".join(f"  - {s['name']}\n" for s in skills),
            )
            for s in skills:
                _write(_under(d, d / s["path"]), s["text"])
        except Exception:
            _remove_marked_tree(ctx.pack_root, d, ctx.plan["id"])
            raise
        return {
            "pack_id": ctx.plan["slug"],
            "path": _shown(ctx, d),
            "skills": [s["name"] for s in skills],
        }

    async def verify(self, ctx: Ctx) -> list[dict[str, Any]]:
        from adk.harnesses import skills_local

        d = self._dir(ctx)
        found = skills_local.discover(roots=[d])
        want = ctx.plan["result"]["skills"]
        checks = [
            _check(
                "the real skill loader finds every skill",
                all(w in found for w in want),
                ", ".join(sorted(found)),
            )
        ]
        for w in want:
            sk = found.get(w)
            checks.append(_check(f"{w}: has a description", bool(sk and sk.description), ""))
        return checks

    async def undo(self, ctx: Ctx) -> dict[str, Any]:
        return {"removed_files": _remove_marked_tree(ctx.pack_root, self._dir(ctx), ctx.plan["id"])}


class ToolPackKind:
    key = "tool_pack"
    label = "Tool pack"
    executable = True
    path = "adk.pack_author scaffold -> validate -> build (the `adk pack new|validate|build` verbs)"
    undo_note = "Removes exactly the files this plan wrote."

    def _dir(self, ctx: Ctx) -> Path:
        if ctx.pack_root is None:
            raise KindError("this runtime has no pack directory configured")
        return _under(ctx.pack_root, Path(ctx.pack_root) / ctx.plan["slug"])

    async def existing(self, ctx: Ctx) -> list[str]:
        return _list_dirs(ctx.pack_root)

    def draft(self, ctx: Ctx) -> dict[str, Any]:
        from adk import pack_author

        pid = ctx.plan["slug"]
        problems = []
        if not pack_author.ID_RE.match(pid):
            problems.append(f"pack id {pid!r} must match {pack_author.ID_RE.pattern}")
        if pid.split(".")[0].split("-")[0] in pack_author.RESERVED_PREFIXES:
            problems.append(f"{pid!r} uses a reserved first-party prefix")
        return {
            "files": [
                f"{pid}/{pack_author.MANIFEST}",
                f"{pid}/__init__.py",
                f"{pid}/tools.py",
                f"{pid}/tests/test_pack.py",
                f"{pid}/README.md",
                f"{pid}/dist/<id>-<version>.tar.gz",
            ],
            "config": {"pack_id": pid, "kind": "tool-pack"},
            "tools": [
                "adk.pack_author.scaffold",
                "adk.pack_author.validate",
                "adk.pack_author.build",
            ],
            "cost": {"usd": 0, "note": "Local files and one reproducible bundle."},
            "where": {
                "runtime": ctx.plan["runtime"],
                "stores": "the pack directory of this runtime",
                "durable_on_awnix": False,
            },
            "validators": [
                "pack id rule + reserved prefixes",
                "pack_author.validate (static, nothing imported)",
                "pack_author.build (reproducible sha256)",
            ],
            "steps": ["Scaffold a working pack", "Validate it", "Build the bundle"],
            "problems": problems,
        }

    async def execute(self, ctx: Ctx) -> dict[str, Any]:
        from adk import pack_author

        if ctx.pack_root is None:
            raise KindError("this runtime has no pack directory configured")
        Path(ctx.pack_root).mkdir(parents=True, exist_ok=True)
        try:
            d = pack_author.scaffold(ctx.plan["slug"], ctx.pack_root)
        except (ValueError, FileExistsError) as exc:
            raise KindError(str(exc))
        _write(
            d / MARKER,
            json.dumps({"plan_id": ctx.plan["id"], "kind": "tool_pack", "created_at": time.time()}),
        )
        try:
            rep = pack_author.validate(d)
            if not rep.ok:
                raise KindError("pack does not validate: " + "; ".join(map(str, rep.findings)))
            built = pack_author.build(d)
        except Exception:
            # Never leave a half-made folder that blocks the name and cannot be undone.
            _remove_marked_tree(ctx.pack_root, d, ctx.plan["id"])
            raise
        return {
            "pack_id": ctx.plan["slug"],
            "path": _shown(ctx, d),
            "bundle": built.tarball.name,
            "sha256": built.sha256,
            "files": built.files,
        }

    async def verify(self, ctx: Ctx) -> list[dict[str, Any]]:
        from adk import pack_author

        d = self._dir(ctx)
        rep = pack_author.validate(d)
        res = ctx.plan["result"]
        digest_file = d / "dist" / f"{res['bundle']}.sha256"
        written = digest_file.is_file() and res["sha256"] in digest_file.read_text(encoding="utf-8")
        return [
            _check("validate is clean", rep.ok, "; ".join(map(str, rep.findings))),
            _check("bundle + sha256 written", written, res["bundle"]),
        ]

    async def undo(self, ctx: Ctx) -> dict[str, Any]:
        return {"removed_files": _remove_marked_tree(ctx.pack_root, self._dir(ctx), ctx.plan["id"])}


# -- planned kinds -----------------------------------------------------------


class PlannedKind:
    executable = False

    def __init__(self, key: str, label: str, path: str, reason: str):
        self.key, self.label, self.path, self.reason = key, label, path, reason
        self.undo_note = "n/a until this kind is wired"

    async def existing(self, ctx: Ctx) -> list[str]:
        return []

    def draft(self, ctx: Ctx) -> dict[str, Any]:
        return {
            "files": [],
            "config": {"slug": ctx.plan["slug"]},
            "tools": [self.path],
            "cost": {"usd": None, "note": "not estimated yet"},
            "where": {"runtime": "genesis", "stores": "n/a", "durable_on_awnix": False},
            "validators": [],
            "steps": [self.reason],
        }

    async def execute(self, ctx: Ctx) -> dict[str, Any]:
        raise KindError(self.reason)

    async def verify(self, ctx: Ctx) -> list[dict[str, Any]]:
        return []

    async def undo(self, ctx: Ctx) -> dict[str, Any]:
        raise KindError(self.reason)


KINDS: dict[str, Any] = {
    "workspace": WorkspaceKind(),
    "agent": AgentKind(),
    "skill_pack": SkillPackKind(),
    "tool_pack": ToolPackKind(),
    "agent_pack": PlannedKind(
        "agent_pack",
        "Agent pack",
        "POST /v1/packs/author then POST /v1/agent/managed/deploy",
        "Agent packs are planned: authoring works through /v1/packs/author, "
        "but it has no teardown route yet, "
        "so Create will not make one it cannot undo.",
    ),
    "workflow": PlannedKind(
        "workflow",
        "Workflow",
        "expedition_create / expedition_plan / expedition_execute, or a routine in config/routines",
        "Workflows are planned: they run as expeditions or routines, "
        "whose cancel path needs the unified "
        "decision card first.",
    ),
}

# What a plain-words ask looks like, per kind. Order matters: most specific first.
_KIND_WORDS = (
    ("skill_pack", re.compile(r"\bskill ?packs?\b|\bskills?\b", re.I)),
    ("tool_pack", re.compile(r"\btool ?packs?\b|\bmcp (server|tools?)\b", re.I)),
    ("agent_pack", re.compile(r"\bagent ?packs?\b|\bbrain ?packs?\b|\bpersona pack\b", re.I)),
    ("workflow", re.compile(r"\bworkflows?\b|\broutines?\b|\bexpeditions?\b|\bpipelines?\b", re.I)),
    ("agent", re.compile(r"\bagents?\b|\bassistant\b|\bbot\b", re.I)),
    ("workspace", re.compile(r"\bworkspaces?\b|\bteam space\b|\bspace\b", re.I)),
)


def infer_kind(text: str) -> str:
    for key, rx in _KIND_WORDS:
        if rx.search(text or ""):
            return key
    return ""
