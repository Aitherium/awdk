"""Eve agent importer — converts Eve agents to AitherADK packs.

Imports eve agents (from GitHub or local filesystem) and converts them to
installable AitherADK packs compatible with pack_discovery.

Maps Eve structure to AitherADK:
  eve instructions.md           → brain_pack.yaml system_prompt
  eve agent.ts (model config)   → agent.yaml
  eve skills/*.md               → pack/<name>/skills/*.md (copy, no transform)
  eve connections/*.ts          → MCP connection config
  eve schedules/                → cron.py
  eve subagents/                → a2a.py / fleet.yaml
  eve tools/*.ts                → tools/node/ (preserve verbatim)
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

__all__ = [
  "import_eve_agent",
  "fetch_eve_agent_manifest",
]

logger = logging.getLogger("adk.importers.eve")

# Current compiled manifest schema version we support
COMPILED_AGENT_MANIFEST_VERSION = 35


def fetch_eve_agent_manifest(agent_path: str) -> dict[str, Any]:
  """Fetch the compiled manifest from an eve agent.

  Args:
    agent_path: Local path or GitHub URL to eve agent directory

  Returns:
    Parsed compiled manifest dict

  Raises:
    ValueError: If manifest is invalid or version is unsupported
  """
  if agent_path.startswith("http"):
    # GitHub fetch — not implemented yet; would use gh api
    raise NotImplementedError("Remote GitHub fetching not yet implemented")

  manifest_path = Path(agent_path) / ".compiled-manifest.json"
  if not manifest_path.exists():
    raise FileNotFoundError(f"No compiled manifest found at {manifest_path}")

  with open(manifest_path, encoding="utf-8") as f:
    manifest = json.load(f)

  # Validate schema version
  if manifest.get("version") != COMPILED_AGENT_MANIFEST_VERSION:
    raise ValueError(
      f"Unsupported manifest version: {manifest.get('version')} "
      f"(expected {COMPILED_AGENT_MANIFEST_VERSION}). "
      "Eve agent may be from a newer or older version."
    )

  if manifest.get("kind") != "eve-agent-compiled-manifest":
    raise ValueError(f"Invalid manifest kind: {manifest.get('kind')}")

  return manifest


def _extract_system_prompt(manifest: dict[str, Any]) -> str:
  """Extract system prompt from compiled manifest."""
  instructions = manifest.get("instructions")
  if not instructions:
    return ""

  # Try to find the raw markdown content
  definition = instructions.get("definition")
  if isinstance(definition, str):
    return definition

  # If instructions points to a file, we can't extract it from the manifest alone
  # In that case, return a placeholder — the user should manually add it
  return "(See eve agent instructions.md in source directory)"


def _extract_model_config(manifest: dict[str, Any]) -> dict[str, Any]:
  """Extract model configuration from compiled manifest.

  `model` may be a plain string (authored) or a dict (compiled). Tolerate both.
  """
  config = manifest.get("config", {})
  model = config.get("model", {})

  if isinstance(model, str):
    model_id, model_routing = (model or "claude-opus-4"), {}
  elif isinstance(model, dict):
    model_id, model_routing = model.get("id", "claude-opus-4"), model.get("routing", {})
  else:
    model_id, model_routing = "claude-opus-4", {}

  return {
    "model_id": model_id,
    "model_routing": model_routing,
    "description": config.get("description", ""),
  }


def _extract_skills(manifest: dict[str, Any]) -> list[dict[str, Any]]:
  """Extract skill definitions from compiled manifest."""
  skills = []
  for skill in manifest.get("skills", []):
    skills.append({
      "name": skill.get("name", "unknown"),
      "description": skill.get("description", ""),
      "logicalPath": skill.get("logicalPath", ""),
    })
  return skills


def _extract_tools(manifest: dict[str, Any]) -> list[dict[str, Any]]:
  """Extract tool definitions from compiled manifest."""
  tools = []
  for tool in manifest.get("tools", []):
    tools.append({
      "name": tool.get("name", "unknown"),
      "description": tool.get("description", ""),
      "logicalPath": tool.get("logicalPath", ""),
      "inputSchema": tool.get("inputSchema", {}),
    })
  return tools


def import_eve_agent(
  agent_path: str,
  target_packs_dir: Optional[Path] = None,
  pack_id: Optional[str] = None,
) -> dict[str, Any]:
  """Import an Eve agent into AitherADK pack format.

  Creates a complete pack directory structure under target_packs_dir/<pack_id>/
  suitable for `adk pack install`.

  Args:
    agent_path: Local path to eve agent directory (must have .compiled-manifest.json)
    target_packs_dir: Target directory for packs (default: ~/.aitheros/packs)
    pack_id: Pack identifier (default: derived from manifest name)

  Returns:
    Result dict with keys:
      - pack_id: Assigned pack ID
      - pack_dir: Path to created pack directory
      - manifest: Parsed eve manifest
      - status: "created" or "updated"

  Raises:
    ValueError: If manifest is invalid
    FileNotFoundError: If manifest cannot be found
  """
  agent_path = Path(agent_path).resolve()

  # Fetch and validate the compiled manifest
  manifest = fetch_eve_agent_manifest(str(agent_path))

  # Determine pack ID
  if not pack_id:
    config = manifest.get("config", {})
    pack_id = config.get("name", "unnamed-eve-agent").lower().replace(" ", "-")
    pack_id = re.sub(r"[^a-z0-9-]", "", pack_id)
    if not pack_id:
      pack_id = "eve-agent"

  # Ensure pack_id has eve-import marker for discoverability
  if not pack_id.endswith("-eve-import"):
    pack_id = f"{pack_id}-eve-import"

  # Create target pack directory
  if target_packs_dir is None:
    target_packs_dir = Path.home() / ".aitheros" / "packs"

  target_packs_dir.mkdir(parents=True, exist_ok=True)
  pack_dir = target_packs_dir / pack_id
  existing = pack_dir.exists()

  pack_dir.mkdir(parents=True, exist_ok=True)

  # Create brain_pack.yaml
  brain_pack_content = _generate_brain_pack_yaml(manifest, agent_path)
  (pack_dir / "brain_pack.yaml").write_text(brain_pack_content, encoding="utf-8")

  # Create agent.yaml
  agent_yaml_content = _generate_agent_yaml(manifest)
  (pack_dir / "agent.yaml").write_text(agent_yaml_content, encoding="utf-8")

  # Copy skills
  skills_dir = pack_dir / "skills"
  skills_dir.mkdir(exist_ok=True)
  _copy_eve_skills(agent_path, skills_dir, manifest)

  # Create tools/node directory and copy TypeScript tools verbatim
  tools_node_dir = pack_dir / "tools" / "node"
  tools_node_dir.mkdir(parents=True, exist_ok=True)
  _copy_eve_tools(agent_path, tools_node_dir, manifest)

  # Create .toolpack.yaml marker (required by pack discovery)
  (pack_dir / ".toolpack.yaml").write_text(
    f"# Eve-imported agent pack\npack_id: {pack_id}\nsource: eve-import\n",
    encoding="utf-8"
  )

  logger.info("Imported eve agent to %s (pack_id: %s)", pack_dir, pack_id)

  return {
    "pack_id": pack_id,
    "pack_dir": str(pack_dir),
    "manifest": manifest,
    "status": "updated" if existing else "created",
  }


def _generate_brain_pack_yaml(manifest: dict[str, Any], agent_path: Path) -> str:
  """Generate brain_pack.yaml from compiled manifest."""
  config = manifest.get("config", {})
  name = config.get("name", "Unnamed Eve Agent")
  description = config.get("description", "")

  # Try to read instructions.md from the source
  instructions_file = agent_path / "instructions.md"
  if instructions_file.exists():
    system_prompt = instructions_file.read_text(encoding="utf-8")
  else:
    system_prompt = "(Eve agent imported — see source instructions.md)"

  # Extract tools available (for UI hint)
  tools = _extract_tools(manifest)
  tool_names = ", ".join(t["name"] for t in tools[:5])  # First 5 tools
  if len(tools) > 5:
    tool_names += f" (+{len(tools) - 5} more)"

  yaml_content = f"""# Eve Agent Import: {name}
# Converted from Eve source at {agent_path}
# Schema version: {COMPILED_AGENT_MANIFEST_VERSION}

app_name: "{name}"
company_name: "Aitherium"
identity: "{config.get('name', 'eve-agent').lower().replace(' ', '-')}"

system_prompt: |
  {system_prompt.strip()}

welcome_message: |
  {description or f"Eve-imported agent: {name}"}

ui_labels:
  title: "{name}"
  subtitle: "Eve-imported agent"
  chat_tab: "Chat"
  input_placeholder: "Ask me anything"
  send_button: "Send"

# Tools discovered in the Eve agent
sample_prompts: []

tools: {list(t["name"] for t in tools[:10])}

features:
  code_review: false
  memory: false
  document_generation: false

safety:
  mode: read_only
  destructive_tools: false
  content_moderation: standard
"""
  return yaml_content


def _generate_agent_yaml(manifest: dict[str, Any]) -> str:
  """Generate agent.yaml from compiled manifest."""
  config = manifest.get("config", {})
  model = config.get("model", {})

  # eve carries `model` as EITHER a plain string (authored form, e.g.
  # "anthropic/claude-sonnet-5") OR a structured object (compiled form with
  # id/routing). Tolerate both — assuming a dict crashes on real authored agents.
  if isinstance(model, str):
    model_id = model or "claude-opus-4"
    model_routing: dict[str, Any] = {}
  elif isinstance(model, dict):
    model_id = model.get("id", "claude-opus-4")
    model_routing = model.get("routing", {})
  else:
    model_id = "claude-opus-4"
    model_routing = {}

  capabilities = []
  if manifest.get("tools"):
    capabilities.append("tools")
  if manifest.get("skills"):
    capabilities.append("skills")
  if manifest.get("connections"):
    capabilities.append("mcp")

  yaml_content = f"""# Eve Agent: {config.get("name", "Unnamed")}
# Auto-generated from eve compiled manifest

name: eve-agent
model: {model_id}
description: "Eve-imported agent"
enabled: true
capabilities: {capabilities}
tier: free

# Model routing (from Eve manifest)
routing: {json.dumps(model_routing, indent=2)}
"""
  return yaml_content


def _resolve_eve_asset(agent_path: Path, rel_path: str) -> Optional[Path]:
  """Resolve a manifest asset path against eve's real on-disk layout.

  Manifest paths are relative to the agent root and typically carry the
  `agent/` prefix (e.g. "agent/skills/get-weather.md"). Older/looser layouts
  omit it. Try the path as-is, then under `agent/`, then the bare basename in
  both `agent_path/<kind>` and `agent_path/agent/<kind>`.
  """
  if not rel_path:
    return None
  candidates = [agent_path / rel_path, agent_path / "agent" / rel_path]
  for c in candidates:
    if c.is_file():
      return c
  return None


def _copy_eve_skills(
  agent_path: Path,
  target_dir: Path,
  manifest: dict[str, Any],
) -> None:
  """Copy skill .md files from an Eve agent, VERBATIM, into target_dir.

  Driven by the manifest's per-skill `path` (relative to the agent root), which
  is eve's real layout (`agent/skills/*.md`). A stub is written ONLY when the
  real file genuinely cannot be found — silently substituting a stub for real
  skill content makes an imported pack look complete while being inert.
  """
  target_dir.mkdir(parents=True, exist_ok=True)
  stubbed: list[str] = []

  for skill in manifest.get("skills", []):
    skill_name = skill.get("name", "unknown")
    src = _resolve_eve_asset(agent_path, skill.get("path", ""))
    # Fall back to a basename match under either layout.
    if src is None:
      for base in (agent_path / "skills", agent_path / "agent" / "skills"):
        cand = base / f"{skill_name}.md"
        if cand.is_file():
          src = cand
          break

    dest = target_dir / f"{skill_name}.md"
    if src is not None:
      dest.write_bytes(src.read_bytes())
      logger.debug("Copied skill (verbatim): %s", skill_name)
    else:
      dest.write_text(
        f"---\ndescription: {skill.get('description', '')}\n---\n\n"
        f"(Skill '{skill_name}' declared in the eve manifest but its source "
        f"markdown was not found at import time — add it manually.)\n",
        encoding="utf-8",
      )
      stubbed.append(skill_name)

  if stubbed:
    logger.warning(
      "eve import: %d skill(s) could not be located and were stubbed: %s",
      len(stubbed), ", ".join(stubbed),
    )


def _copy_eve_tools(
  agent_path: Path,
  target_dir: Path,
  manifest: dict[str, Any],
) -> None:
  """Copy Eve's TypeScript tools VERBATIM to tools/node/.

  These are NOT transpiled — they remain .ts files executed by the Node sidecar
  (aither-eve-runtime) over MCP. Driven by the manifest's per-tool `path`
  (eve's real layout is `agent/tools/*.ts`), with a glob fallback across both
  layouts. A tool declared in the manifest but not copied is a SILENTLY DROPPED
  capability, so we warn loudly on any miss.
  """
  target_dir.mkdir(parents=True, exist_ok=True)
  copied: set[str] = set()

  for tool in manifest.get("tools", []):
    src = _resolve_eve_asset(agent_path, tool.get("path", ""))
    if src is None:
      name = tool.get("name", "")
      for base in (agent_path / "tools", agent_path / "agent" / "tools"):
        cand = base / f"{name}.ts"
        if cand.is_file():
          src = cand
          break
    if src is not None:
      (target_dir / src.name).write_bytes(src.read_bytes())
      copied.add(src.name)
      logger.debug("Copied tool (verbatim): %s", src.name)
    else:
      logger.warning(
        "eve import: tool %r declared in manifest but its .ts source was not "
        "found — the Node sidecar will have nothing to run for it",
        tool.get("name", "<unnamed>"),
      )

  # Also sweep any loose .ts not referenced by the manifest, both layouts.
  for base in (agent_path / "tools", agent_path / "agent" / "tools"):
    if base.is_dir():
      for tool_file in base.glob("*.ts"):
        if tool_file.name not in copied:
          (target_dir / tool_file.name).write_bytes(tool_file.read_bytes())
          copied.add(tool_file.name)

  if not copied:
    logger.debug("No TypeScript tools found in eve agent")
