"""AitherZero pack — az_* agent tools for self-service infra provisioning.

The AitherZero surface is: a config.psd1 + bootstrap.ps1 that can provision bare-metal,
on-prem, cloud, or hybrid infrastructure by driving a library of numbered PowerShell
automation-scripts and playbooks. This pack lets an awdk agent MANAGE that surface:

  * Inventory   — az_inventory / az_describe_script read the script+playbook catalogue
                  (from the generated config-schema.json, itself AST-extracted from every
                  script's param() block — so private scripts are first-class once scanned).
  * Extend      — az_export_schema (re)generates the schema from whatever inventory you point
                  it at (public OR a customer's private automation-scripts), keeping the
                  editor and these tools in sync as new scripts land.
  * Configure   — az_generate_config emits a valid config.local.psd1 from section + per-script
                  overrides; az_validate_config runs the fail-closed traps before bootstrap.
  * Deploy      — az_plan_playbook resolves a playbook's parameters + sequence into a plan.
  * Author      — az_scaffold_script writes a new automation-script skeleton (param() block,
                  ValidateSet enums, comment-based help) that auto-extends config once scanned.

Design doctrine (same as the other packs):
  * Every tool returns a dict, never raises. Missing paths/tools => {"error", "fix"}.
  * Pure tools (inventory, describe, generate, scaffold-preview) have no side effects.
  * Tools that shell out use `pwsh -NoProfile` and time out; pwsh-missing fails soft.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger("aitherzero_pack")

_PWSH_TIMEOUT = 120.0
_TYPE_LABEL = {"s": "string", "e": "enum", "n": "number", "b": "bool", "a": "array"}


# ── root + path resolution ───────────────────────────────────────────────


def _az_root(explicit: Optional[str] = None) -> Optional[Path]:
    """Locate the AitherZero product root (holds config/ + library/).

    Order: explicit arg → AITHERZERO_ROOT env → walk up from CWD → known repo layout.
    Returns None if not found (callers fail soft with a fix hint).
    """
    cands: list[Path] = []
    if explicit:
        cands.append(Path(explicit))
    env = os.environ.get("AITHERZERO_ROOT")
    if env:
        cands.append(Path(env))
    here = Path(__file__).resolve()
    # This pack lives at <repo>/awdk/adk/toolpacks/aitherzero/tools.py.
    for parent in list(here.parents):
        cands.append(parent / ".PRODUCTS" / ".AITHERZERO")
    for start in (Path.cwd(), here):
        for parent in [start, *start.parents]:
            cands.append(parent)
    for c in cands:
        try:
            if (c / "config" / "config.psd1").exists() and (
                c / "library" / "automation-scripts"
            ).exists():
                return c
        except OSError:
            continue
    return None


def _root_or_error(explicit: Optional[str]) -> tuple[Optional[Path], Optional[dict]]:
    root = _az_root(explicit)
    if root is None:
        return None, {
            "error": "AitherZero root not found (need config/config.psd1 + "
            "library/automation-scripts).",
            "fix": "Pass root=<path to .AITHERZERO>, or set AITHERZERO_ROOT, or run from "
            "inside the AitherZero product tree.",
        }
    return root, None


def _schema_path(root: Path) -> Path:
    return root / "tools" / "config-editor" / "config-schema.json"


def _load_schema(root: Path) -> tuple[Optional[dict], Optional[dict]]:
    sp = _schema_path(root)
    if not sp.exists():
        return None, {
            "error": f"config-schema.json not found at {sp}.",
            "fix": "Run az_export_schema first (it AST-scans the automation-scripts and "
            "emits the schema the inventory tools read).",
        }
    try:
        return json.loads(sp.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as e:
        return None, {"error": f"schema unreadable: {type(e).__name__}: {e}",
                      "fix": "Regenerate it with az_export_schema."}


def _pwsh(args: list[str], cwd: Optional[Path] = None) -> tuple[Optional[str], Optional[dict]]:
    """Run pwsh; return (stdout, None) or (None, error_dict). Never raises."""
    try:
        r = subprocess.run(
            ["pwsh", "-NoProfile", *args],
            capture_output=True, text=True, timeout=_PWSH_TIMEOUT,
            cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError:
        return None, {"error": "pwsh (PowerShell 7) not found on PATH.",
                      "fix": "Install PowerShell 7+ (pwsh) — AitherZero standardises on it."}
    except subprocess.TimeoutExpired:
        return None, {"error": f"pwsh timed out after {_PWSH_TIMEOUT:.0f}s."}
    if r.returncode != 0:
        return None, {"error": f"pwsh failed (exit {r.returncode}).",
                      "detail": (r.stderr or "").strip()[:600]}
    return r.stdout, None


# ── psd1 emission (Python side, mirrors the editor) ──────────────────────


def _psv(v) -> str:
    if isinstance(v, bool):
        return "$true" if v else "$false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "@(" + ", ".join(_psv(x) for x in v) + ")"
    s = str(v)
    if s in ("$true", "$false") or re.fullmatch(r"-?\d+(\.\d+)?", s):
        return s  # already a literal the user typed
    return "'" + s.replace("'", "''") + "'"


def _pskey(k: str) -> str:
    """A psd1 hashtable key: bare if a valid identifier, else single-quoted.

    Keys like '00-bootstrap/Bootstrap-AitherOS' (slash, dash, leading digit) are NOT bare
    identifiers and must be quoted or PowerShell fails to parse the hashtable.
    """
    return k if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(k)) else _psv(str(k))


def _emit(d: dict, indent: int = 1) -> str:
    pad = "    " * indent
    lines = ["@{"]
    for k, val in d.items():
        if isinstance(val, dict):
            lines.append(f"{pad}{_pskey(k)} = " + _emit(val, indent + 1))
        else:
            lines.append(f"{pad}{_pskey(k)} = {_psv(val)}")
    lines.append("    " * (indent - 1) + "}")
    return "\n".join(lines)


# ── 1. INVENTORY ─────────────────────────────────────────────────────────


def az_inventory(category: Optional[str] = None, root: Optional[str] = None,
                 include_playbooks: bool = True) -> dict:
    """List the automation-script + playbook inventory (from config-schema.json).

    Pure read. With `category`, returns that category's scripts (id, name, synopsis,
    param count); without it, returns per-category counts + totals. Set include_playbooks
    to also list playbook names.
    """
    r, err = _root_or_error(root)
    if err:
        return err
    schema, err = _load_schema(r)
    if err:
        return err
    scripts = schema.get("scripts") or []
    cats: dict[str, list] = {}
    for s in scripts:
        cats.setdefault(s.get("category") or "misc", []).append(s)

    if category:
        rows = cats.get(category)
        if rows is None:
            return {"error": f"no category '{category}'.",
                    "available": sorted(cats.keys())}
        return {
            "category": category,
            "script_count": len(rows),
            "scripts": [
                {"id": s.get("id", ""), "name": s.get("name", ""),
                 "synopsis": s.get("synopsis", ""),
                 "params": len(s.get("params") or []),
                 "config_keys": s.get("configKeys") or []}
                for s in rows
            ],
        }

    out = {
        "root": str(r),
        "script_count": len(scripts),
        "param_count": sum(len(s.get("params") or []) for s in scripts),
        "categories": [
            {"category": c, "scripts": len(cats[c])} for c in sorted(cats)
        ],
    }
    if include_playbooks:
        pb = schema.get("playbooks") or []
        pb_dir = r / "library" / "playbooks"
        names = [p.get("name") for p in pb] if pb else [
            f.stem for f in pb_dir.glob("*.psd1")
        ] if pb_dir.exists() else []
        out["playbooks"] = sorted(n for n in names if n)
        out["playbook_count"] = len(out["playbooks"])
    return out


def az_describe_script(name: str, root: Optional[str] = None) -> dict:
    """Full configurable surface of one automation-script.

    Matches on exact name, then case-insensitive substring. Returns every parameter
    (name, type, enum options, default, mandatory, help), the config.psd1 keys it reads,
    and the file path — everything the editor shows for that script.
    """
    if not name:
        return {"error": "name is required."}
    r, err = _root_or_error(root)
    if err:
        return err
    schema, err = _load_schema(r)
    if err:
        return err
    scripts = schema.get("scripts") or []
    exact = [s for s in scripts if s.get("name") == name]
    matches = exact or [
        s for s in scripts if name.lower() in (s.get("name") or "").lower()
    ]
    if not matches:
        return {"error": f"no script matching '{name}'.",
                "hint": "Use az_inventory(category=...) to list script names."}
    if len(matches) > 1 and not exact:
        return {"error": f"'{name}' is ambiguous ({len(matches)} matches).",
                "matches": [f"{s.get('category')}/{s.get('name')}" for s in matches[:20]]}
    s = matches[0]
    return {
        "id": s.get("id", ""),
        "name": s.get("name", ""),
        "category": s.get("category", ""),
        "file": s.get("file", ""),
        "synopsis": s.get("synopsis", ""),
        "override_key": f"{s.get('category')}/{s.get('name')}",
        "params": [
            {"name": p.get("name"), "type": _TYPE_LABEL.get(p.get("type"), p.get("type")),
             "enum": p.get("enum") or [], "default": p.get("default"),
             "mandatory": bool(p.get("mandatory")), "help": p.get("help", "")}
            for p in (s.get("params") or [])
        ],
        "config_keys": s.get("configKeys") or [],
    }


# ── 2. EXTEND (regenerate schema from any inventory) ─────────────────────


def az_export_schema(script_root: Optional[str] = None, playbook_root: Optional[str] = None,
                     root: Optional[str] = None, out_file: Optional[str] = None) -> dict:
    """(Re)generate config-schema.json from an automation-script inventory.

    Shells out to Export-AitherConfigSchema.ps1, which AST-scans every *.ps1 param() block.
    Point script_root at your PRIVATE automation-scripts to fold them into the same schema
    the editor and az_inventory read — this is the extensibility seam.
    """
    r, err = _root_or_error(root)
    if err:
        return err
    gen = r / "tools" / "config-editor" / "Export-AitherConfigSchema.ps1"
    if not gen.exists():
        return {"error": f"generator not found at {gen}."}
    sroot = Path(script_root) if script_root else (r / "library" / "automation-scripts")
    proot = Path(playbook_root) if playbook_root else (r / "library" / "playbooks")
    out = Path(out_file) if out_file else _schema_path(r)
    args = ["-File", str(gen), "-ScriptRoot", str(sroot), "-OutFile", str(out)]
    if proot.exists():
        args += ["-PlaybookRoot", str(proot)]
    stdout, err = _pwsh(args, cwd=r)
    if err:
        return err
    schema, serr = _load_schema(r) if out == _schema_path(r) else (
        (json.loads(out.read_text(encoding="utf-8")), None) if out.exists() else (None, None)
    )
    n_scripts = len(schema.get("scripts") or []) if schema else None
    return {
        "ok": True,
        "out_file": str(out),
        "script_root": str(sroot),
        "scripts_with_config": n_scripts,
        "message": (stdout or "").strip().splitlines()[-1:] and (stdout or "").strip()
        .splitlines()[-1] or "schema written",
    }


# ── 3. CONFIGURE ─────────────────────────────────────────────────────────


def az_generate_config(sections: Optional[dict] = None, automation: Optional[dict] = None,
                       header: bool = True) -> dict:
    """Emit a valid config.local.psd1 from overrides.

    `sections`  — nested dict of config-section overrides, e.g.
                  {"Core": {"Environment": "Production"}, "AI": {"MultiModelMode": True}}.
    `automation`— per-script parameter overrides keyed "category/ScriptName", e.g.
                  {"00-bootstrap/Bootstrap-AitherOS": {"Profile": "core"}}.
                  These are written under AutomationDefaults; bootstrap.ps1 / playbooks
                  pass them to each script by name.
    Pure — returns the psd1 text; does not write to disk.
    """
    sections = sections or {}
    automation = automation or {}
    if not isinstance(sections, dict) or not isinstance(automation, dict):
        return {"error": "sections and automation must be objects (hashtables)."}
    body: dict = {}
    for k, v in sections.items():
        body[k] = v
    if automation:
        body["AutomationDefaults"] = {k: v for k, v in automation.items()}
    if not body:
        return {"psd1": "@{ }\n", "note": "no overrides given — empty config."}
    head = (
        "# config.local.psd1 — generated by AitherZero (az_generate_config)\n"
        "# Only your overrides. Merges over base at load "
        "(base < platform < local < env).\n" if header else ""
    )
    return {
        "psd1": head + _emit(body) + "\n",
        "sections": sorted(sections.keys()),
        "automation_overrides": sorted(automation.keys()),
    }


def az_validate_config(psd1_path: Optional[str] = None, psd1_text: Optional[str] = None,
                       root: Optional[str] = None) -> dict:
    """Run the fail-closed config traps before bootstrap.

    Parses the config (from psd1_path or inline psd1_text via pwsh Import-PowerShellDataFile),
    then checks: vLLM port collisions against services.yaml, GPU memory-fraction sum > 1.0,
    and mesh replica nodes missing a primary host. Returns {ok, errors, warnings}.
    """
    r, err = _root_or_error(root)
    if err:
        return err
    if not psd1_path and not psd1_text:
        return {"error": "pass psd1_path or psd1_text."}
    if psd1_path:
        p = Path(psd1_path)
        if not p.exists():
            return {"error": f"config not found: {p}"}
        cmd = ["-Command",
               f"Import-PowerShellDataFile -LiteralPath '{p}' | ConvertTo-Json -Depth 25"]
    else:
        # Write inline text to a temp file pwsh can parse.
        tmp = r / "tools" / "config-editor" / ".az_validate_tmp.psd1"
        try:
            tmp.write_text(psd1_text, encoding="utf-8")
        except OSError as e:
            return {"error": f"cannot stage temp config: {e}"}
        cmd = ["-Command",
               f"Import-PowerShellDataFile -LiteralPath '{tmp}' | ConvertTo-Json -Depth 25"]
    stdout, err = _pwsh(cmd, cwd=r)
    if psd1_text:
        try:
            (r / "tools" / "config-editor" / ".az_validate_tmp.psd1").unlink()
        except OSError:
            pass
    if err:
        return {"ok": False, "errors": [err.get("error", "parse failed")],
                "detail": err.get("detail", "")}
    try:
        cfg = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError as e:
        return {"ok": False, "errors": [f"config is not a valid @{{ }} block: {e}"]}

    errors: list[str] = []
    warnings: list[str] = []

    # (a) vLLM ports vs services.yaml owners
    svc_ports = _services_ports(r)
    ai = (cfg.get("AI") or {})
    vllm = ai.get("vLLM") or {}
    used: dict[int, str] = {}
    frac_sum = 0.0
    if isinstance(vllm, dict):
        for worker, wc in vllm.items():
            if not isinstance(wc, dict):
                continue
            port = wc.get("Port")
            if isinstance(port, int):
                owners = svc_ports.get(port, [])
                foreign = [o for o in owners if worker.lower() not in o.lower()]
                if foreign:
                    who = "', '".join(foreign)
                    plural = "services" if len(foreign) > 1 else "service"
                    errors.append(
                        f"AI.vLLM.{worker} Port {port} is owned by {plural} '{who}' in "
                        "services.yaml — get_service_url() will route callers there, not to "
                        "your worker." + (f" (services.yaml ALSO double-claims :{port} "
                        "across those owners — a drift in services.yaml itself.)"
                        if len(owners) > 1 else ""))
                if port in used:
                    errors.append(f"AI.vLLM.{worker} and {used[port]} both use port {port}.")
                used[port] = f"AI.vLLM.{worker}"
            gf = wc.get("GpuMemoryFraction") or wc.get("GpuMemoryUtilization")
            if isinstance(gf, (int, float)):
                frac_sum += float(gf)
    if frac_sum > 1.0:
        errors.append(f"vLLM GPU memory fractions sum to {frac_sum:.2f} (>1.0) — the "
                      "co-resident workers will not fit on one GPU.")

    # (b) mesh replica missing a primary host
    infra = cfg.get("Infrastructure") or {}
    nodes = infra.get("Nodes") or infra.get("MeshNodes") or []
    if isinstance(nodes, list):
        for nd in nodes:
            if isinstance(nd, dict) and str(nd.get("MeshRole", "")).lower() == "replica" \
                    and not (nd.get("MeshPrimaryHost") or "").strip():
                warnings.append(
                    f"Mesh node '{nd.get('Name', '?')}' is a replica but has no "
                    "MeshPrimaryHost — it cannot join the primary.")

    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "checked": ["vllm-port-collision", "gpu-fraction-sum", "mesh-replica-host"]}


def _services_ports(root: Path) -> dict[int, list[str]]:
    """port -> [service names] from AitherOS services.yaml (best-effort, empty on miss).

    Returns ALL owners per port (not just the first) so a port that services.yaml itself
    double-claims is reported honestly rather than arbitrarily attributed to one service.
    """
    # services.yaml lives in the AitherOS core tree, sibling to .PRODUCTS.
    for cand in (root.parent.parent / "AitherOS" / "config" / "services.yaml",
                 root.parents[2] / "AitherOS" / "config" / "services.yaml"):
        if cand.exists():
            try:
                import yaml
                data = yaml.safe_load(cand.read_text(encoding="utf-8")) or {}
            except (OSError, ImportError, ValueError):
                return {}
            out: dict[int, list[str]] = {}
            for name, c in (data.get("services") or {}).items():
                if not isinstance(c, dict):
                    continue
                # A disabled service isn't serving its port — don't report it as an owner
                # (it can't shadow a caller). Matches get_service_url() runtime behaviour.
                if c.get("enabled") is False:
                    continue
                if isinstance(c.get("port"), int):
                    out.setdefault(c["port"], []).append(name)
                # Compound services declare their real ports under sub_services (list/dict).
                subs = c.get("sub_services")
                if isinstance(subs, list):
                    for sub in subs:
                        if isinstance(sub, dict) and isinstance(sub.get("port"), int):
                            out.setdefault(sub["port"], []).append(
                                f"{name}/{sub.get('name', '?')}")
                elif isinstance(subs, dict):
                    for subname, subcfg in subs.items():
                        if isinstance(subcfg, dict) and isinstance(subcfg.get("port"), int):
                            out.setdefault(subcfg["port"], []).append(f"{name}/{subname}")
            return out
    return {}


# ── 4. DEPLOY (playbook plan) ────────────────────────────────────────────


def az_plan_playbook(name: str, root: Optional[str] = None) -> dict:
    """Resolve a playbook into a deployment plan (parameters + ordered steps).

    Reads library/playbooks/<name>.psd1 and returns its user-overridable Parameters,
    prerequisites, and the ordered Sequence (each step's name, command, condition, and
    continue-on-error). Read-only — does not execute anything.
    """
    if not name:
        return {"error": "playbook name is required."}
    r, err = _root_or_error(root)
    if err:
        return err
    stem = name[:-5] if name.endswith(".psd1") else name
    pf = r / "library" / "playbooks" / f"{stem}.psd1"
    if not pf.exists():
        avail = sorted(f.stem for f in (r / "library" / "playbooks").glob("*.psd1"))
        return {"error": f"playbook '{stem}' not found.", "available": avail[:60]}
    stdout, err = _pwsh(
        ["-Command",
         f"Import-PowerShellDataFile -LiteralPath '{pf}' | ConvertTo-Json -Depth 25"],
        cwd=r)
    if err:
        return err
    try:
        pb = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError as e:
        return {"error": f"playbook parse failed: {e}"}
    seq = pb.get("Sequence") or []
    if isinstance(seq, dict):
        seq = [seq]
    steps = []
    for i, st in enumerate(seq, 1):
        if not isinstance(st, dict):
            continue
        steps.append({
            "step": i,
            "name": st.get("Name", ""),
            "description": st.get("Description", ""),
            "command": st.get("Command", ""),
            "condition": st.get("Condition", ""),
            "continue_on_error": bool(st.get("ContinueOnError")),
        })
    return {
        "name": pb.get("Name", stem),
        "description": pb.get("Description", ""),
        "category": pb.get("Category", ""),
        "parameters": pb.get("Parameters") or {},
        "prerequisites": pb.get("Prerequisites") or [],
        "steps": steps,
        "step_count": len(steps),
    }


# ── 5. AUTHOR (scaffold a new automation-script) ─────────────────────────


def az_scaffold_script(name: str, category: str, synopsis: str = "",
                       params: Optional[list] = None, number: Optional[str] = None,
                       write: bool = False, root: Optional[str] = None) -> dict:
    """Generate a new automation-script skeleton that auto-extends config once scanned.

    Emits a #Requires-versioned .ps1 with comment-based help (.SYNOPSIS / .PARAMETER),
    a [CmdletBinding()] param() block (ValidateSet for enums, defaults, [Parameter(Mandatory)]),
    and the AitherZero numbering convention. Because the schema generator AST-scans param()
    blocks, the new script's parameters appear in the editor + az_inventory on the next
    az_export_schema — no schema hand-editing.

    `params` — list of {name, type(string|enum|number|bool), enum:[...], default, mandatory,
               help}. `write=True` writes the file into the category folder (returns path);
               if `number` is omitted it auto-picks the next free NNNN slot in that category.
    """
    if not name or not category:
        return {"error": "name and category are required (category e.g. '00-bootstrap')."}
    params = params or []
    pascal = re.sub(r"[^0-9A-Za-z]+", "_", name).strip("_")
    lines: list[str] = []
    param_help: list[str] = []
    for p in params:
        if not isinstance(p, dict) or not p.get("name"):
            return {"error": "each param needs at least a 'name'."}
        pn = p["name"]
        ptype = (p.get("type") or "string").lower()
        enum = p.get("enum") or []
        attrs: list[str] = []
        if p.get("mandatory"):
            attrs.append("        [Parameter(Mandatory)]")
        if ptype == "enum" and enum:
            opts = ", ".join(_psv(o) for o in enum)
            attrs.append(f"        [ValidateSet({opts})]")
        pstype = {"number": "[int]", "bool": "[switch]", "array": "[string[]]"}.get(
            ptype, "[string]")
        default = p.get("default")
        decl = f"        {pstype}${pn}"
        if ptype != "bool" and default is not None:
            decl += f" = {_psv(default)}"
        lines.append("\n".join([*attrs, decl]))
        if p.get("help"):
            param_help.append(f".PARAMETER {pn}\n  {p['help']}")
    param_block = ",\n".join(lines) if lines else ""
    help_params = ("\n\n" + "\n\n".join(param_help)) if param_help else ""
    script = f"""#Requires -Version 7.0
<#
.SYNOPSIS
  {synopsis or name}

.DESCRIPTION
  AitherZero automation-script. Declares its configurable surface via the param() block
  below; run Export-AitherConfigSchema.ps1 to fold it into config-schema.json so it shows
  up in the config editor and az_inventory.{help_params}
#>
[CmdletBinding()]
param(
{param_block}
)

$ErrorActionPreference = 'Stop'

# TODO: implement {pascal}. Read config with $Config.<Section>.<Key> if this script is
# config-driven; those reads are captured as configKeys by the schema generator.

Write-Host "{pascal}: not yet implemented" -ForegroundColor Yellow
"""
    num = number or "NNNN"
    result = {
        "category": category,
        "script": script,
        "param_count": len(params),
        "note": "Run az_export_schema after saving so the new params reach the editor.",
    }
    if write:
        r, err = _root_or_error(root)
        if err:
            return err
        target_dir = r / "library" / "automation-scripts" / category
        if not target_dir.exists():
            return {"error": f"category folder not found: {target_dir}",
                    "fix": "Create it or pass an existing category.", "script": script}
        if not number:
            num = _next_number(target_dir, category)
            result["auto_numbered"] = num
        elif not re.fullmatch(r"\d{3,4}", num):
            return {"error": f"number must be 3-4 digits (got '{num}').", "script": script}
        filename = f"{num}_{pascal}.ps1"
        target = target_dir / filename
        if target.exists():
            return {"error": f"{target} already exists — won't overwrite.",
                    "script": script}
        try:
            target.write_text(script, encoding="utf-8")
        except OSError as e:
            return {"error": f"write failed: {e}", "script": script}
        result["written"] = str(target)
    result["filename"] = f"{num}_{pascal}.ps1"
    return result


def _next_number(target_dir: Path, category: str) -> str:
    """First FREE 4-digit slot at/after the category's base band.

    Seeds from the category's leading digits (e.g. '01-infrastructure' → base 0100) and
    returns the first unused number from there — so a lone mis-filed outlier (e.g. a 9011
    script parked in the infra tree) doesn't push new scripts up to 9012. Always collision-
    safe: the returned slot is guaranteed not to be taken by any existing NNNN prefix.
    """
    taken = set()
    for f in target_dir.rglob("*.ps1"):  # recursive — categories nest scripts in subfolders
        m = re.match(r"(\d{3,4})", f.name)
        if m:
            taken.add(int(m.group(1)))
    cm = re.match(r"(\d{1,2})", category)
    n = int(cm.group(1)) * 100 if cm else 9000
    while n in taken:
        n += 1
    return f"{n:04d}"
