"""Declarative Gemini-style ("AitherGem") and OpenAI-style ("AitherGPT") bots."""

from __future__ import annotations

from adk.bots.actions import (
    ActionExecutor,
    OpenAPIAction,
    load_openapi_actions,
    validate_action_url,
)
from adk.bots.cli_entry import bot_main
from adk.bots.deploy import (
    BotDeployerBackend,
    DeployResult,
    build_awrun_job_spec,
    deploy_bot,
    derive_bot_egress_hosts,
    export_identity_yaml,
    verify_bot_bundle,
)
from adk.bots.knowledge import KnowledgeChunk, KnowledgeHit, KnowledgeIndex
from adk.bots.manifest import (
    BOT_BACKENDS,
    BOT_STYLES,
    BOT_TARGET_SURFACES,
    MANIFEST_FILENAME,
    ActionSpec,
    BotManifest,
    CapabilitiesConfig,
    ChannelsConfig,
    GenerationConfig,
    GroundingConfig,
    KnowledgeConfig,
    SafetySetting,
    ToolsConfig,
    load_bot_manifest,
    scaffold_bot,
    slugify_bot_id,
)
from adk.bots.onboard import (
    load_provider_keys,
    provider_keys_path,
    resolve_provider_key,
    run_bot_onboard,
    save_provider_key,
)
from adk.bots.runtime import (
    BackendUnavailableError,
    BotRuntime,
    MockBotBackend,
    TurnResult,
    create_bot_app,
    mount_bot_routes,
)
from adk.bots.ui import render_playground_html
from adk.bots.validator import BotFinding, BotValidationResult, validate_bot

__all__ = [
    "ActionExecutor",
    "ActionSpec",
    "BOT_BACKENDS",
    "BOT_STYLES",
    "BOT_TARGET_SURFACES",
    "BackendUnavailableError",
    "BotDeployerBackend",
    "BotFinding",
    "BotManifest",
    "BotRuntime",
    "BotValidationResult",
    "CapabilitiesConfig",
    "ChannelsConfig",
    "DeployResult",
    "GenerationConfig",
    "GroundingConfig",
    "KnowledgeChunk",
    "KnowledgeConfig",
    "KnowledgeHit",
    "KnowledgeIndex",
    "MANIFEST_FILENAME",
    "MockBotBackend",
    "OpenAPIAction",
    "SafetySetting",
    "ToolsConfig",
    "TurnResult",
    "bot_main",
    "build_awrun_job_spec",
    "create_bot_app",
    "deploy_bot",
    "derive_bot_egress_hosts",
    "export_identity_yaml",
    "load_bot_manifest",
    "load_openapi_actions",
    "load_provider_keys",
    "mount_bot_routes",
    "provider_keys_path",
    "render_playground_html",
    "resolve_provider_key",
    "run_bot_onboard",
    "save_provider_key",
    "scaffold_bot",
    "slugify_bot_id",
    "validate_action_url",
    "validate_bot",
    "verify_bot_bundle",
]
