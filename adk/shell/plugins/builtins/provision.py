"""
Company Provisioning Plugin for AitherShell (``/provision``)
=============================================================

One ``company.yaml`` -> one ``adk up`` plan per agent. A composer, not a
parallel system: every flag it emits is a real flag of the ``adk up`` parser
(``adk/cli.py``).

Flag mapping (pinned by a test that parses every emitted command with the
real ``adk`` parser, so a drift here fails CI instead of a customer's run):

    agent.identity       -> --identity
    agent.brain_pack     -> --brain-pack    (file or dir; adk exits 2 if it does not
                                             resolve. Omitted: adk uses ./brain_pack.yaml)
    agent.name           -> --name          (default <company>-<identity>)
    agent.port           -> --port
    agent.reach tunnel   -> --reach tunnel
    agent.reach mesh     -> --reach mesh
    agent.reach offline  -> --offline       (``--reach`` accepts ONLY tunnel|mesh;
                                             ``--reach offline`` is an argparse error)
    agent.model          -> --provider      (cloud providers only; local/gateway
                                             emit nothing, adk's own ladder applies)
    agent.approve        -> --approve       (string or list, comma-joined)
    company.portal       -> --portal        (not for offline agents)
    company.cloud_mode   -> AITHER_CLOUD_MODE (env, not a flag)
    company.tenant       -> NOTHING is sent: an agent registers under the tenant of
                            the portal account that signs in, never a tenant named in
                            a file. It is validated as a slug and printed so the
                            operator signs in to the right account.
    company.memory/gateway -> informational (platform planes every agent gets); ``off``
                            is REFUSED, since no adk flag removes either
    offline agent        -> also AITHER_OFFLINE=1

Usage:
    /provision [plan] [path]     -- validate company.yaml and print the plan
                                    (default ./company.yaml)
    /provision validate [path]   -- schema check only

The shell never executes the plan; ``provision_company.py --apply`` does.

Aliases: /company
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand

REACH_CHOICES = ("tunnel", "mesh", "offline")
CLOUD_MODES = ("local_first", "cloud_first", "cloud_only")
KNOWN_PROVIDERS = ("local", "gateway", "deepseek", "openai", "anthropic")
#: providers that emit no --provider flag: adk's own local/gateway ladder applies.
_LADDER_PROVIDERS = ("local", "gateway")
#: a company is a handful of agents; anything near this is abuse, not a config.
MAX_CONFIG_BYTES = 256_000
MAX_AGENTS = 64
_TENANT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
#: YAML 1.1 reads a bare on/off as a bool, so both spellings are accepted.
_PLANE_ON = ("on", "true", "yes")
_PLANE_OFF = ("off", "false", "no")


class ConfigRejectedError(ValueError):
    """company.yaml refused before any node is expanded (size, aliases)."""


def _no_alias_loader() -> type:
    """A SafeLoader that refuses YAML aliases (a ~1 KB alias bomb expands exponentially)."""
    import yaml
    from yaml.composer import ComposerError
    from yaml.events import AliasEvent

    class _NoAliasSafeLoader(yaml.SafeLoader):
        def compose_node(self, parent, index):  # type: ignore[override]
            if self.check_event(AliasEvent):
                event = self.peek_event()
                raise ComposerError(
                    None, None, "YAML aliases are not allowed in company.yaml",
                    event.start_mark)
            return super().compose_node(parent, index)

    return _NoAliasSafeLoader


def parse_config_text(text: str) -> Any:
    """Parse company.yaml text: size-capped, aliases refused.

    Raises ``ConfigRejectedError`` (size/alias) or ``yaml.YAMLError`` (syntax).
    """
    import yaml

    text = text or ""
    if len(text.encode("utf-8", "replace")) > MAX_CONFIG_BYTES:
        raise ConfigRejectedError(f"company.yaml larger than {MAX_CONFIG_BYTES // 1000} KB")
    try:
        return yaml.load(text, Loader=_no_alias_loader()) or {}  # noqa: S506 -- SafeLoader subclass
    except yaml.composer.ComposerError as exc:
        if "aliases are not allowed" in str(exc):
            raise ConfigRejectedError(
                "YAML aliases/anchors are not allowed in company.yaml") from exc
        raise


def load_config(path: Path) -> "tuple[Any, str]":
    """(parsed config, error). error is '' on success."""
    import yaml

    if not path.is_file():
        return None, f"{path} not found"
    try:
        return parse_config_text(path.read_text(encoding="utf-8")), ""
    except ConfigRejectedError as exc:
        return None, f"{path} rejected: {exc}"
    except yaml.YAMLError as exc:
        return None, f"{path} is not valid YAML: {exc}"


def _short(value: Any) -> str:
    """Render a caller value for an error message without expanding it."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        r = repr(value)
        return r if len(r) <= 80 else r[:77] + "..."
    return f"<{type(value).__name__}>"


def _approve_value(raw: Any) -> str:
    if isinstance(raw, (list, tuple)):
        return ",".join(str(x).strip() for x in raw if str(x).strip())
    return str(raw or "").strip()


def validate(cfg: Any) -> List[str]:
    """Return schema violations (empty == valid)."""
    if not isinstance(cfg, dict):
        return ["company.yaml must be a mapping"]
    company = cfg.get("company")
    if not isinstance(company, dict):
        return ["top-level 'company' block missing or not a mapping"]
    errors: List[str] = []

    for key in ("name", "portal", "tenant"):
        val = company.get(key)
        if val is not None and not isinstance(val, str):
            errors.append(f"company.{key} must be a string, got {_short(val)}")
    tenant = company.get("tenant")
    if isinstance(tenant, str) and not _TENANT_RE.match(tenant):
        errors.append(f"company.tenant {_short(tenant)} is not a slug (a-z, 0-9, '-')")
    cloud_mode = company.get("cloud_mode", "local_first")
    if not isinstance(cloud_mode, str) or cloud_mode not in CLOUD_MODES:
        errors.append(f"company.cloud_mode {_short(cloud_mode)} not in {list(CLOUD_MODES)}")
    for plane in ("memory", "gateway"):
        val = company.get(plane, True)
        norm = val.strip().lower() if isinstance(val, str) else val
        if norm is False or (isinstance(norm, str) and norm in _PLANE_OFF):
            errors.append(
                f"company.{plane}: off is not supported -- every agent gets the {plane} "
                "plane and no adk up flag removes it; drop the key or set it to on")
        elif not (norm is True or (isinstance(norm, str) and norm in _PLANE_ON)):
            errors.append(f"company.{plane} {_short(val)} must be on")

    agents = company.get("agents")
    if not isinstance(agents, list) or not agents:
        errors.append("company.agents must be a non-empty list")
        agents = []
    elif len(agents) > MAX_AGENTS:
        errors.append(f"company.agents has {len(agents)} entries (max {MAX_AGENTS})")
        agents = []

    seen_ports: Dict[int, int] = {}
    seen_names: Dict[str, int] = {}
    for i, agent in enumerate(agents):
        if not isinstance(agent, dict):
            errors.append(f"company.agents[{i}] is not a mapping")
            continue
        identity = agent.get("identity")
        if not isinstance(identity, str) or not identity.strip():
            errors.append(f"company.agents[{i}].identity is required and must be a string")
        brain_pack = agent.get("brain_pack")
        if brain_pack is not None and (not isinstance(brain_pack, str) or not brain_pack.strip()):
            errors.append(
                f"company.agents[{i}].brain_pack must be a non-empty path string, "
                f"got {_short(brain_pack)}")
        name = agent.get("name")
        if name is not None and not isinstance(name, str):
            errors.append(f"company.agents[{i}].name must be a string, got {_short(name)}")
        elif isinstance(name, str) and name.strip():
            if name in seen_names:
                errors.append(
                    f"company.agents[{i}].name {name!r} already used by agents[{seen_names[name]}]")
            else:
                seen_names[name] = i
        reach = agent.get("reach", "tunnel")
        if not isinstance(reach, str) or reach not in REACH_CHOICES:
            errors.append(
                f"company.agents[{i}].reach {_short(reach)} not in {list(REACH_CHOICES)}")
        model = agent.get("model", "gateway")
        if not isinstance(model, str) or model not in KNOWN_PROVIDERS:
            errors.append(
                f"company.agents[{i}].model {_short(model)} not in {list(KNOWN_PROVIDERS)}")
        approve = agent.get("approve")
        if approve is not None and not (
                isinstance(approve, str)
                or (isinstance(approve, list) and all(isinstance(x, str) for x in approve))):
            errors.append(f"company.agents[{i}].approve must be a string or a list of strings")
        port = agent.get("port", 8080)
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            errors.append(
                f"company.agents[{i}].port {_short(port)} must be an int in 1024..65535")
        elif port in seen_ports:
            errors.append(
                f"company.agents[{i}].port {port} already used by agents[{seen_ports[port]}]")
        else:
            seen_ports[port] = i
    return errors


def build_plan(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One plan entry per agent: the exact ``adk up`` argv plus its env. Pure.

    Call ``validate`` first; this assumes a valid config.
    """
    company = cfg["company"]
    cloud_mode = company.get("cloud_mode", "local_first")
    company_slug = "-".join(str(company.get("name") or "company").strip().lower().split())
    portal = str(company.get("portal") or "").strip()
    plans: List[Dict[str, Any]] = []
    for i, agent in enumerate(company.get("agents") or []):
        identity = str(agent["identity"]).strip()
        name = str(agent.get("name") or f"{company_slug}-{identity}")
        reach = agent.get("reach", "tunnel")
        cmd = ["adk", "up", "--identity", identity, "--name", name,
               "--port", str(agent.get("port", 8080))]
        brain_pack = str(agent.get("brain_pack") or "").strip()
        if brain_pack:
            cmd += ["--brain-pack", brain_pack]
        env = {"AITHER_CLOUD_MODE": cloud_mode}
        if reach == "offline":
            cmd.append("--offline")
            env["AITHER_OFFLINE"] = "1"
        else:
            cmd += ["--reach", reach]
            if portal:
                cmd += ["--portal", portal]
        model = agent.get("model", "gateway")
        if model and model not in _LADDER_PROVIDERS:
            cmd += ["--provider", model]
        approve = _approve_value(agent.get("approve"))
        if approve:
            cmd += ["--approve", approve]
        plans.append({
            "agent_index": i,
            "identity": identity,
            "name": name,
            "reach": reach,
            "cmd": cmd,
            "env": env,
            "cloud_mode": cloud_mode,
            "tenant": str(company.get("tenant") or ""),
        })
    return plans


def format_plan(plans: List[Dict[str, Any]], source: str = "company.yaml") -> str:
    lines = [f"# dry-run - {len(plans)} agent(s) from {source} (nothing executed)"]
    tenant = next((p.get("tenant") for p in plans if p.get("tenant")), "")
    if tenant:
        lines.append(f"# sign in (adk login) as an account of portal tenant {tenant!r}; "
                     "agents register under that account's tenant, not this file")
    for p in plans:
        lines.append(f"  [{p['identity']}] {p['name']}")
        lines.append("    env  " + " ".join(f"{k}={v}" for k, v in p["env"].items()))
        lines.append("    cmd  " + " ".join(shlex.quote(a) for a in p["cmd"]))
    return "\n".join(lines)


def plan_from_path(path: Path) -> "tuple[List[Dict[str, Any]], List[str]]":
    """(plans, errors) for a company.yaml on disk. Never raises on bad input."""
    cfg, err = load_config(path)
    if err:
        return [], [err]
    errors = validate(cfg)
    if errors:
        return [], errors
    return build_plan(cfg), []


class ProvisionPlugin(SlashCommand):
    name = "provision"
    description = "Validate a company.yaml and print its per-agent `adk up` plan"
    aliases = ["company"]

    def __init__(self) -> None:
        super().__init__(
            name="provision",
            description="Validate a company.yaml and print its per-agent `adk up` plan",
            aliases=["company"],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        sub = args[0].lower() if args else "plan"
        rest = args[1:]
        if sub not in ("plan", "validate", "help"):
            # `/provision path/to/company.yaml` == `/provision plan path/...`
            sub, rest = "plan", args
        if sub == "help":
            return __doc__
        path = Path(rest[0]) if rest else Path("company.yaml")
        plans, errors = plan_from_path(path)
        if errors:
            return f"{len(errors)} problem(s) in {path}:\n" + "\n".join(f"  - {e}" for e in errors)
        if sub == "validate":
            return f"{path}: valid ({len(plans)} agent(s))"
        return format_plan(plans, str(path)) + (
            "\n# run each cmd with --yes, or: python provision_company.py --apply")
