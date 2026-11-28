"""Declarative bot manifest (``bot.yaml``) schema, loader, and scaffolder.

Supports two first-class bot styles:
- ``gemini`` ("AitherGem"): ``system_instruction``/``instructions``,
  ``generation_config``, ``safety_settings``, ``grounding``, ``functionDeclarations``.
- ``openai`` ("AitherGPT"): ``instructions``, ``capabilities``
  (``code_interpreter``, ``file_search``, ``web_browsing``), OpenAPI 3.x ``actions``,
  and ``generation_config``.

Security invariant: ``bot.yaml`` and ``.env.example`` must NEVER store API keys
or live secrets. BYOK keys belong in ``~/.aither/provider_keys.json``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MANIFEST_FILENAME = "bot.yaml"

BOT_STYLES: tuple[str, ...] = ("gemini", "openai")
BOT_BACKENDS: tuple[str, ...] = (
    "auto",
    "aither",
    "gemini",
    "openai",
    "llamacpp",
    "vllm",
    "mock",
    "awzeal",
    "zero-libc",
)
BOT_TARGET_SURFACES: tuple[str, ...] = ("standard", "awnix", "awzeal")

ID_RE = re.compile(r"^[a-z][a-z0-9._-]{2,63}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

DEFAULT_MODELS: dict[str, str] = {
    "gemini": "gemini-2.0-flash",
    "openai": "gpt-4o-mini",
}

DEFAULT_BACKEND_MODELS: dict[str, str] = {
    "aither": "aither-orchestrator",
    "llamacpp": "local-model",
    "vllm": "local-model",
    "mock": "mock-bot-1",
    "awzeal": "bonsai2-27b",
    "zero-libc": "qwen3-0.6b",
}


class KnowledgeConfig(BaseModel):
    """Local knowledge base configuration for RAG / ``file_search``."""

    model_config = ConfigDict(extra="ignore")

    paths: list[str] = Field(default_factory=list)
    chunk_size: int = Field(default=800, ge=64, le=16000)
    top_k: int = Field(default=5, ge=1, le=50)


class ToolsConfig(BaseModel):
    """Tool pack, Python module, and MCP server declarations."""

    model_config = ConfigDict(extra="ignore")

    packs: list[str] = Field(default_factory=list)
    modules: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)


class ChannelsConfig(BaseModel):
    """Enabled delivery channels for the bot."""

    model_config = ConfigDict(extra="ignore")

    discord: bool = False
    web_ui: bool = True


class GenerationConfig(BaseModel):
    """Model sampling and output constraints."""

    model_config = ConfigDict(extra="ignore")

    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    response_mime_type: str | None = None
    response_schema: dict[str, Any] | None = None
    response_format: dict[str, Any] | str | None = None

    def effective_max_tokens(self, default: int = 2048) -> int:
        if self.max_output_tokens is not None:
            return self.max_output_tokens
        if self.max_tokens is not None:
            return self.max_tokens
        return default


class SafetySetting(BaseModel):
    """Gemini-style content safety threshold setting."""

    model_config = ConfigDict(extra="ignore")

    category: str
    threshold: str

    @field_validator("category", "threshold")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("SafetySetting category and threshold must be non-empty")
        return s


class GroundingConfig(BaseModel):
    """Gemini-style grounding configuration."""

    model_config = ConfigDict(extra="ignore")

    google_search_style_web: bool = False
    local_knowledge: bool = True


class CapabilitiesConfig(BaseModel):
    """OpenAI-style assistant capability flags."""

    model_config = ConfigDict(extra="ignore")

    code_interpreter: bool = False
    file_search: bool = True
    web_browsing: bool = False


class ActionSpec(BaseModel):
    """OpenAPI 3.x external action declaration."""

    model_config = ConfigDict(extra="ignore")

    name: str
    openapi_spec: str
    auth_type: Literal["none", "bearer", "api_key"] = "none"
    base_url: str = ""
    auth_env: str = ""

    @field_validator("name", "openapi_spec")
    @classmethod
    def _non_empty_action_field(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("ActionSpec name and openapi_spec must be non-empty")
        return s


class BotManifest(BaseModel):
    """Validated declarative bot configuration loaded from ``bot.yaml``."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    version: str = "0.1.0"
    style: Literal["gemini", "openai"]
    description: str = ""
    instructions: str
    model: str
    backend: Literal[
        "auto",
        "aither",
        "gemini",
        "openai",
        "llamacpp",
        "vllm",
        "mock",
        "awzeal",
        "zero-libc",
    ] = "auto"

    model_sha256: str | None = None
    shared_ro_mmap: bool = False
    target_surface: Literal["standard", "awnix", "awzeal"] = "standard"

    conversation_starters: list[str] = Field(default_factory=list)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    generation_config: GenerationConfig = Field(default_factory=GenerationConfig)
    safety_settings: list[SafetySetting] = Field(default_factory=list)
    grounding: GroundingConfig = Field(default_factory=GroundingConfig)
    capabilities: CapabilitiesConfig = Field(default_factory=CapabilitiesConfig)
    actions: list[ActionSpec] = Field(default_factory=list)

    @field_validator("model_sha256")
    @classmethod
    def _validate_model_sha256(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = str(v).strip().lower()
        if not s:
            return None
        if not SHA256_RE.match(s):
            raise ValueError(
                f"Invalid model_sha256 {v!r}: must be a 64-character hex SHA-256 digest"
            )
        return s

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        s = (v or "").strip()
        if not ID_RE.match(s):
            raise ValueError(
                f"Invalid bot id {v!r}: must match {ID_RE.pattern}"
            )
        return s

    @field_validator("version")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        s = (v or "").strip()
        if not SEMVER_RE.match(s):
            raise ValueError(f"Invalid version {v!r}: must be semver (e.g. 0.1.0)")
        return s

    @field_validator("name", "instructions", "model")
    @classmethod
    def _validate_non_empty_text(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("Field must be a non-empty string")
        return s

    @model_validator(mode="after")
    def _normalize_starters(self) -> BotManifest:
        self.conversation_starters = [
            str(item).strip()
            for item in self.conversation_starters
            if str(item).strip()
        ]
        return self

    def to_public_dict(self) -> dict[str, Any]:
        """Return public metadata safe for ``GET /bot/info`` (zero secrets)."""
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "style": self.style,
            "description": self.description,
            "model": self.model,
            "backend": self.backend,
            "conversation_starters": list(self.conversation_starters),
            "knowledge": {
                "paths": list(self.knowledge.paths),
                "chunk_size": self.knowledge.chunk_size,
                "top_k": self.knowledge.top_k,
            },
            "tools": {
                "packs": list(self.tools.packs),
                "modules": list(self.tools.modules),
                "mcp_servers": list(self.tools.mcp_servers),
            },
            "channels": self.channels.model_dump(),
            "generation_config": self.generation_config.model_dump(exclude_none=True),
            "safety_settings": [s.model_dump() for s in self.safety_settings],
            "grounding": self.grounding.model_dump(),
            "capabilities": self.capabilities.model_dump(),
            "actions": [
                {
                    "name": a.name,
                    "openapi_spec": a.openapi_spec,
                    "auth_type": a.auth_type,
                    "base_url": a.base_url,
                }
                for a in self.actions
            ],
        }
        if self.model_sha256 is not None:
            out["model_sha256"] = self.model_sha256
        if self.shared_ro_mmap:
            out["shared_ro_mmap"] = True
        if self.target_surface != "standard":
            out["target_surface"] = self.target_surface
        return out


def slugify_bot_id(name: str) -> str:
    """Convert a human bot name into a valid ``bot.yaml`` ``id``."""
    raw = (name or "").strip().lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", raw).strip("._-")
    if not slug or not slug[0].isalpha():
        slug = f"bot-{slug}" if slug else "aither-bot"
    slug = slug[:64].rstrip("._-")
    while len(slug) < 3:
        slug += "0"
    if not ID_RE.match(slug):
        return "custom-bot"
    return slug


def resolve_manifest_path(bot_dir_or_file: str | Path) -> Path:
    """Locate ``bot.yaml`` from either a directory or direct file path."""
    p = Path(bot_dir_or_file)
    if p.is_file():
        return p
    return p / MANIFEST_FILENAME


def load_bot_manifest(bot_dir: str | Path) -> BotManifest:
    """Load and validate ``bot.yaml`` from *bot_dir* (or a direct YAML path)."""
    manifest_path = resolve_manifest_path(bot_dir)
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Bot manifest not found: {manifest_path}")
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {manifest_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Manifest {manifest_path} must be a YAML mapping")
    try:
        return BotManifest.model_validate(raw)
    except Exception as exc:
        raise ValueError(f"Manifest validation failed for {manifest_path}: {exc}") from exc


def _default_model_for(style: str, backend: str) -> str:
    if backend in DEFAULT_BACKEND_MODELS:
        return DEFAULT_BACKEND_MODELS[backend]
    if backend == "gemini":
        return DEFAULT_MODELS["gemini"]
    if backend == "openai":
        return DEFAULT_MODELS["openai"]
    return DEFAULT_MODELS.get(style, "gpt-4o-mini")


def _render_bot_yaml(manifest: BotManifest) -> str:
    data: dict[str, Any] = {
        "id": manifest.id,
        "name": manifest.name,
        "version": manifest.version,
        "style": manifest.style,
        "description": manifest.description,
        "instructions": manifest.instructions,
        "model": manifest.model,
        "backend": manifest.backend,
    }
    if manifest.model_sha256 is not None:
        data["model_sha256"] = manifest.model_sha256
    if manifest.shared_ro_mmap:
        data["shared_ro_mmap"] = True
    if manifest.target_surface != "standard":
        data["target_surface"] = manifest.target_surface
    data.update(
        {
            "conversation_starters": manifest.conversation_starters,
            "knowledge": manifest.knowledge.model_dump(),
            "tools": manifest.tools.model_dump(),
            "channels": manifest.channels.model_dump(),
        }
    )
    if manifest.style == "gemini":
        data["generation_config"] = {
            "temperature": (
                manifest.generation_config.temperature
                if manifest.generation_config.temperature is not None
                else 0.4
            ),
            "top_p": (
                manifest.generation_config.top_p
                if manifest.generation_config.top_p is not None
                else 0.95
            ),
            "max_output_tokens": manifest.generation_config.max_output_tokens or 2048,
        }
        data["safety_settings"] = [
            s.model_dump() for s in manifest.safety_settings
        ] or [
            {
                "category": "HARM_CATEGORY_HATE_SPEECH",
                "threshold": "BLOCK_MEDIUM_AND_ABOVE",
            },
            {
                "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                "threshold": "BLOCK_MEDIUM_AND_ABOVE",
            },
        ]
        data["grounding"] = manifest.grounding.model_dump()
    else:
        data["capabilities"] = manifest.capabilities.model_dump()
        data["actions"] = [a.model_dump() for a in manifest.actions]
        data["generation_config"] = {
            "temperature": (
                manifest.generation_config.temperature
                if manifest.generation_config.temperature is not None
                else 0.4
            ),
            "top_p": (
                manifest.generation_config.top_p
                if manifest.generation_config.top_p is not None
                else 1.0
            ),
            "max_tokens": manifest.generation_config.max_tokens or 2048,
        }
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def scaffold_bot(
    name: str,
    style: str,
    target_dir: str | Path,
    *,
    backend: str = "auto",
    model: str | None = None,
    instructions: str | None = None,
    description: str = "",
    bot_id: str | None = None,
    overwrite: bool = False,
    model_sha256: str | None = None,
    shared_ro_mmap: bool = False,
    target_surface: str = "standard",
) -> Path:
    """Scaffold a complete, runnable declarative bot directory.

    Writes ``bot.yaml``, ``knowledge/README.md``, ``actions/.gitkeep``,
    ``tools.py``, ``.env.example`` (placeholders only), ``README.md``, and
    ``tests/test_bot.py``.
    """
    style_norm = (style or "").strip().lower()
    if style_norm not in BOT_STYLES:
        raise ValueError(f"Unsupported bot style {style!r}; expected one of {BOT_STYLES}")
    backend_norm = (backend or "auto").strip().lower()
    if backend_norm not in BOT_BACKENDS:
        raise ValueError(
            f"Unsupported backend {backend!r}; expected one of {BOT_BACKENDS}"
        )
    surface_norm = (target_surface or "standard").strip().lower()
    if surface_norm not in BOT_TARGET_SURFACES:
        raise ValueError(
            f"Unsupported target_surface {target_surface!r}; expected one of {BOT_TARGET_SURFACES}"
        )

    display_name = (name or "").strip()
    if not display_name:
        raise ValueError("Bot name must be non-empty")

    resolved_id = (bot_id or "").strip() or slugify_bot_id(display_name)
    if not ID_RE.match(resolved_id):
        raise ValueError(f"Invalid bot id {resolved_id!r}: must match {ID_RE.pattern}")

    dest = Path(target_dir)
    if dest.exists() and any(dest.iterdir()) and not overwrite:
        raise FileExistsError(f"Target directory already exists and is non-empty: {dest}")

    resolved_model = (model or "").strip() or _default_model_for(style_norm, backend_norm)
    resolved_instructions = (instructions or "").strip() or (
        f"You are {display_name}, a helpful local-first AitherOS "
        f"{'AitherGem' if style_norm == 'gemini' else 'AitherGPT'} assistant. "
        "Answer accurately using attached knowledge and tools when available."
    )
    resolved_desc = (description or "").strip() or (
        f"Self-hosted {style_norm}-style bot built with awdk."
    )

    manifest = BotManifest(
        id=resolved_id,
        name=display_name,
        version="0.1.0",
        style=style_norm,  # type: ignore[arg-type]
        description=resolved_desc,
        instructions=resolved_instructions,
        model=resolved_model,
        backend=backend_norm,  # type: ignore[arg-type]
        model_sha256=model_sha256,
        shared_ro_mmap=bool(shared_ro_mmap),
        target_surface=surface_norm,  # type: ignore[arg-type]
        conversation_starters=[
            f"What can {display_name} help me with?",
            "Summarize your local knowledge base.",
        ],
        knowledge=KnowledgeConfig(paths=["knowledge"], chunk_size=800, top_k=5),
        tools=ToolsConfig(packs=[], modules=["tools.py"], mcp_servers=[]),
        channels=ChannelsConfig(discord=False, web_ui=True),
    )

    dest.mkdir(parents=True, exist_ok=True)
    (dest / "knowledge").mkdir(parents=True, exist_ok=True)
    (dest / "actions").mkdir(parents=True, exist_ok=True)
    (dest / "tests").mkdir(parents=True, exist_ok=True)

    (dest / MANIFEST_FILENAME).write_text(
        _render_bot_yaml(manifest), encoding="utf-8", newline="\n"
    )

    (dest / "knowledge" / "README.md").write_text(
        f"# {display_name} Knowledge Base\n\n"
        "Place Markdown (`.md`), plain text (`.txt`), JSON (`.json`), or CSV (`.csv`) "
        "documents in this folder. They are indexed locally with BM25 for grounding.\n",
        encoding="utf-8",
        newline="\n",
    )

    (dest / "actions" / ".gitkeep").write_text(
        "# Place OpenAPI 3.x action specs (.yaml/.json) here.\n",
        encoding="utf-8",
        newline="\n",
    )

    (dest / "tools.py").write_text(
        '"""Custom tools for this bot. Never store secrets in this file."""\n\n'
        "from __future__ import annotations\n\n\n"
        "def get_bot_status() -> dict:\n"
        '    """Return basic bot readiness metadata."""\n'
        f'    return {{"bot_id": "{resolved_id}", "status": "ready"}}\n\n\n'
        "def register(registry) -> None:\n"
        '    """Register bot tools into the runtime ToolRegistry."""\n'
        "    registry.register(\n"
        "        get_bot_status,\n"
        '        name="get_bot_status",\n'
        '        description="Return basic bot readiness metadata.",\n'
        "        repeat_safe=True,\n"
        "    )\n",
        encoding="utf-8",
        newline="\n",
    )

    (dest / ".env.example").write_text(
        "# Environment variables template (placeholders only — never commit real keys)\n"
        "# Preferred BYOK store: ~/.aither/provider_keys.json\n"
        f"BOT_PORT=8095\n"
        f"AITHER_LLM_BACKEND={backend_norm}\n"
        "GEMINI_API_KEY=\n"
        "OPENAI_API_KEY=\n"
        "AITHER_API_KEY=\n"
        "DISCORD_BOT_TOKEN=\n",
        encoding="utf-8",
        newline="\n",
    )

    endpoint_hint = (
        "`POST /v1beta/models/" + resolved_model + ":generateContent`"
        if style_norm == "gemini"
        else "`POST /v1/chat/completions` and `/v1/assistants`"
    )
    (dest / "README.md").write_text(
        f"# {display_name} (`{resolved_id}`)\n\n"
        f"{resolved_desc}\n\n"
        f"- **Style**: `{style_norm}`\n"
        f"- **Model**: `{resolved_model}`\n"
        f"- **Backend**: `{backend_norm}`\n"
        f"- **Wire API**: {endpoint_hint}\n\n"
        "## Quickstart\n\n"
        "```bash\n"
        "adk bot validate .\n"
        "adk bot dev . --port 8095\n"
        'adk bot chat . -m "Hello!"\n'
        "adk bot deploy . --target local\n"
        "```\n",
        encoding="utf-8",
        newline="\n",
    )

    (dest / "tests" / "test_bot.py").write_text(
        '"""Starter validation and runtime smoke test for this bot."""\n\n'
        "from pathlib import Path\n\n"
        "from fastapi.testclient import TestClient\n\n"
        "from adk.bots.runtime import create_bot_app\n"
        "from adk.bots.validator import validate_bot\n\n"
        "BOT_DIR = Path(__file__).resolve().parent.parent\n\n\n"
        "def test_bot_manifest_is_valid() -> None:\n"
        "    report = validate_bot(BOT_DIR)\n"
        "    assert report.ok, [str(f) for f in report.findings]\n\n\n"
        "def test_bot_info_endpoint() -> None:\n"
        '    app = create_bot_app(BOT_DIR, backend="mock")\n'
        "    client = TestClient(app)\n"
        '    resp = client.get("/bot/info")\n'
        "    assert resp.status_code == 200\n"
        f'    assert resp.json()["id"] == "{resolved_id}"\n',
        encoding="utf-8",
        newline="\n",
    )

    return dest
