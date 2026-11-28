"""Static zero-execution validator (``BOT001``–``BOT008``) for declarative bots.

Never imports or executes ``tools.py`` or OpenAPI action handlers. Secret matches
are reported strictly by ``relative_path:lineno`` — matched secret values are
never echoed in findings or logs.
"""

from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from adk.bots.actions import validate_action_url
from adk.bots.manifest import (
    BOT_BACKENDS,
    BOT_STYLES,
    BOT_TARGET_SURFACES,
    ID_RE,
    MANIFEST_FILENAME,
    SEMVER_RE,
    SHA256_RE,
)
from adk.pack_author import (
    EXCLUDE_DIRS,
    EXCLUDE_NAMES,
    EXCLUDE_SUFFIXES,
    SECRET_RES,
)

# Extend pack_author.SECRET_RES to cover Google Gemini keys, hyphenated OpenAI
# project/service-account keys, and hardcoded Discord bot tokens.
_BOT_EXTRA_SECRET_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"sk-(?:proj|svcacct)-[A-Za-z0-9_-]{20,}"),
    re.compile(r"DISCORD_BOT_TOKEN\s*=\s*['\"]?[A-Za-z0-9._-]{24,}['\"]?"),
)

ALL_SECRET_RES: tuple[re.Pattern[str], ...] = tuple(SECRET_RES) + _BOT_EXTRA_SECRET_RES


@dataclass
class BotFinding:
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code} {self.message}"


@dataclass
class BotValidationResult:
    bot_dir: Path
    manifest: dict[str, Any] = field(default_factory=dict)
    findings: list[BotFinding] = field(default_factory=list)
    judged: bool = True

    @property
    def ok(self) -> bool:
        return self.judged and not self.findings

    @property
    def exit_code(self) -> int:
        if not self.judged:
            return 2
        return 1 if self.findings else 0

    def add(self, code: str, message: str) -> None:
        self.findings.append(BotFinding(code, message))


def _resolve_within(bot_dir: Path, rel_path: str) -> tuple[Path | None, str]:
    """Resolve *rel_path* strictly inside *bot_dir*, rejecting traversal and symlinks."""
    raw_input = rel_path or ""
    if "\x00" in raw_input:
        return None, f"null byte is not allowed in path: {raw_input!r}"
    raw = raw_input.strip()
    if not raw:
        return None, "empty path"
    p = Path(raw)
    if p.is_absolute() or raw.startswith(("/", "\\")) or (len(raw) >= 2 and raw[1] == ":"):
        return None, f"absolute path is not allowed: {raw!r}"
    if ".." in p.parts:
        return None, f"path traversal ('..') is not allowed: {raw!r}"

    root_resolved = bot_dir.resolve()
    cur = bot_dir
    for part in p.parts:
        cur = cur / part
        if cur.is_symlink():
            return None, f"symlink component is not allowed: {raw!r}"
    try:
        resolved = cur.resolve()
    except OSError as exc:
        return None, f"cannot resolve {raw!r}: {exc}"
    if not resolved.is_relative_to(root_resolved):
        return None, f"path escapes bot directory: {raw!r}"
    return cur, ""


def iter_bot_files(bot_dir: Path) -> list[Path]:
    """Yield sorted relative file paths in *bot_dir*, excluding build/VCS/env artifacts."""
    out: list[Path] = []
    for root, dirs, files in os.walk(bot_dir):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS)
        for name in sorted(files):
            if name in EXCLUDE_NAMES or name.endswith(EXCLUDE_SUFFIXES):
                continue
            full = Path(root) / name
            out.append(full.relative_to(bot_dir))
    return out


def _validate_tools_and_actions(
    bot_dir: Path,
    manifest: dict[str, Any],
    result: BotValidationResult,
    *,
    allow_local_action_urls: bool,
) -> None:
    tools = manifest.get("tools")
    if tools is not None:
        if not isinstance(tools, dict):
            result.add("BOT005", "'tools' must be a mapping")
        else:
            for list_field in ("packs", "mcp_servers"):
                val = tools.get(list_field)
                if val is not None:
                    if not isinstance(val, list) or not all(
                        isinstance(item, str) and bool(item.strip()) for item in val
                    ):
                        result.add(
                            "BOT005",
                            f"'tools.{list_field}' must be a list of non-empty strings",
                        )

            modules = tools.get("modules", [])
            if not isinstance(modules, list) or not all(isinstance(m, str) for m in modules):
                result.add("BOT005", "'tools.modules' must be a list of relative file paths")
            else:
                for mod_rel in modules:
                    target, err = _resolve_within(bot_dir, mod_rel)
                    if target is None:
                        result.add("BOT005", f"tools.modules entry {mod_rel!r}: {err}")
                        continue
                    if not target.is_file():
                        result.add("BOT005", f"tools.modules file missing: {mod_rel}")
                        continue
                    if target.suffix != ".py":
                        result.add("BOT005", f"tools.modules entry must be .py: {mod_rel}")
                        continue
                    try:
                        src = target.read_text(encoding="utf-8")
                        ast.parse(src, filename=str(target))
                    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
                        result.add(
                            "BOT005",
                            f"tools.modules {mod_rel} failed static parse: {exc}",
                        )

    actions = manifest.get("actions")
    if actions is None:
        return
    if not isinstance(actions, list):
        result.add("BOT005", "'actions' must be a list of action mappings")
        return

    for idx, action in enumerate(actions):
        if not isinstance(action, dict):
            result.add("BOT005", f"actions[{idx}] must be a mapping")
            continue
        name = str(action.get("name") or "").strip()
        spec_rel = str(action.get("openapi_spec") or "").strip()
        auth_type = str(action.get("auth_type") or "none").strip()
        if not name:
            result.add("BOT005", f"actions[{idx}] missing non-empty 'name'")
        if auth_type not in ("none", "bearer", "api_key"):
            result.add(
                "BOT005",
                f"actions[{idx}] invalid auth_type {auth_type!r} "
                "(expected none|bearer|api_key)",
            )
        if not spec_rel:
            result.add("BOT005", f"actions[{idx}] missing 'openapi_spec'")
            continue
        spec_path, err = _resolve_within(bot_dir, spec_rel)
        if spec_path is None:
            result.add("BOT005", f"actions[{idx}] openapi_spec {spec_rel!r}: {err}")
            continue
        if not spec_path.is_file():
            result.add("BOT005", f"actions[{idx}] openapi_spec missing: {spec_rel}")
            continue
        try:
            text = spec_path.read_text(encoding="utf-8")
            if spec_path.suffix.lower() == ".json":
                spec_data = json.loads(text)
            else:
                spec_data = yaml.safe_load(text)
        except Exception as exc:
            result.add("BOT005", f"actions[{idx}] failed to parse {spec_rel}: {exc}")
            continue

        if not isinstance(spec_data, dict):
            result.add("BOT005", f"actions[{idx}] OpenAPI spec {spec_rel} must be a mapping")
            continue
        openapi_ver = str(spec_data.get("openapi") or "").strip()
        if not openapi_ver.startswith("3."):
            result.add(
                "BOT005",
                f"actions[{idx}] OpenAPI spec {spec_rel} must declare openapi 3.x "
                f"(got {openapi_ver!r})",
            )
        paths = spec_data.get("paths")
        if not isinstance(paths, dict) or not paths:
            result.add(
                "BOT005",
                f"actions[{idx}] OpenAPI spec {spec_rel} must define non-empty 'paths'",
            )

        urls_to_check: list[str] = []
        base_url = str(action.get("base_url") or "").strip()
        if base_url:
            urls_to_check.append(base_url)

        def _collect_servers(srv_list: Any) -> None:
            if not isinstance(srv_list, list):
                return
            for srv in srv_list:
                if isinstance(srv, dict):
                    u = str(srv.get("url") or "").strip()
                    if u and u not in urls_to_check:
                        urls_to_check.append(u)

        _collect_servers(spec_data.get("servers"))
        if isinstance(paths, dict):
            for path_item in paths.values():
                if not isinstance(path_item, dict):
                    continue
                _collect_servers(path_item.get("servers"))
                for method_name in ("get", "post", "put", "patch", "delete", "options", "head", "trace"):
                    op = path_item.get(method_name)
                    if isinstance(op, dict):
                        _collect_servers(op.get("servers"))

        for url in urls_to_check:
            ok_url, reason = validate_action_url(
                url,
                allow_local_urls=allow_local_action_urls,
                skip_dns=False,
                fail_on_dns_error=False,
            )
            if not ok_url:
                result.add(
                    "BOT005",
                    f"actions[{idx}] blocked unsafe URL {url!r}: {reason}",
                )


def _validate_knowledge_and_symlinks(
    bot_dir: Path, manifest: dict[str, Any], result: BotValidationResult
) -> None:
    # Symlinks anywhere inside bot_dir are rejected (BOT006).
    for root, dirs, files in os.walk(bot_dir):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS)
        for item in dirs + files:
            p = Path(root) / item
            if p.is_symlink():
                rel = p.relative_to(bot_dir).as_posix()
                result.add("BOT006", f"symlink not allowed in bot directory: {rel}")

    knowledge = manifest.get("knowledge")
    if knowledge is None:
        return
    if not isinstance(knowledge, dict):
        result.add("BOT006", "'knowledge' must be a mapping")
        return

    chunk_size = knowledge.get("chunk_size", 800)
    top_k = knowledge.get("top_k", 5)
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or not (64 <= chunk_size <= 16000):
        result.add("BOT006", "'knowledge.chunk_size' must be an int in [64, 16000]")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not (1 <= top_k <= 50):
        result.add("BOT006", "'knowledge.top_k' must be an int in [1, 50]")

    paths = knowledge.get("paths", [])
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        result.add("BOT006", "'knowledge.paths' must be a list of relative paths")
        return
    for raw_rel in paths:
        target, err = _resolve_within(bot_dir, raw_rel)
        if target is None:
            result.add("BOT006", f"knowledge.paths entry {raw_rel!r}: {err}")
            continue
        if not target.exists():
            result.add("BOT006", f"knowledge path does not exist: {raw_rel}")


def _scan_secrets(bot_dir: Path, result: BotValidationResult) -> None:
    env_file = bot_dir / ".env"
    if env_file.exists() or env_file.is_symlink():
        result.add("BOT007", "'.env' must not exist inside a shareable bot directory")

    for rel in iter_bot_files(bot_dir):
        full = bot_dir / rel
        if full.is_symlink():
            continue
        try:
            text = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pat in ALL_SECRET_RES:
                if pat.search(line):
                    # Report path and line ONLY — never echo the matched secret.
                    result.add(
                        "BOT007",
                        f"potential secret detected at {rel.as_posix()}:{lineno}",
                    )
                    break


def _validate_generation_and_safety(
    manifest: dict[str, Any], result: BotValidationResult
) -> None:
    gen = manifest.get("generation_config")
    if gen is not None:
        if not isinstance(gen, dict):
            result.add("BOT008", "'generation_config' must be a mapping")
        else:
            temp = gen.get("temperature")
            if temp is not None:
                if isinstance(temp, bool) or not isinstance(temp, (int, float)) or not (0.0 <= float(temp) <= 2.0):
                    result.add("BOT008", "'generation_config.temperature' must be in [0, 2]")
            top_p = gen.get("top_p")
            if top_p is not None:
                if isinstance(top_p, bool) or not isinstance(top_p, (int, float)) or not (0.0 < float(top_p) <= 1.0):
                    result.add("BOT008", "'generation_config.top_p' must be in (0, 1]")
            for key in ("top_k", "max_output_tokens", "max_tokens"):
                val = gen.get(key)
                if val is not None:
                    if isinstance(val, bool) or not isinstance(val, int) or val < 1:
                        result.add("BOT008", f"'generation_config.{key}' must be a positive integer")

    safety = manifest.get("safety_settings")
    if safety is not None:
        if not isinstance(safety, list):
            result.add("BOT008", "'safety_settings' must be a list of mappings")
        else:
            for idx, item in enumerate(safety):
                if not isinstance(item, dict):
                    result.add("BOT008", f"safety_settings[{idx}] must be a mapping")
                    continue
                cat = item.get("category")
                thr = item.get("threshold")
                if not isinstance(cat, str) or not cat.strip():
                    result.add("BOT008", f"safety_settings[{idx}].category must be a non-empty string")
                if not isinstance(thr, str) or not thr.strip():
                    result.add("BOT008", f"safety_settings[{idx}].threshold must be a non-empty string")


def validate_bot(
    bot_dir: str | Path, *, allow_local_action_urls: bool = False
) -> BotValidationResult:
    """Run static ``BOT001``–``BOT008`` checks on *bot_dir* without executing any code."""
    root = Path(bot_dir)
    result = BotValidationResult(bot_dir=root)
    if not root.is_dir():
        result.judged = False
        result.add("BOT000", f"bot directory does not exist: {root}")
        return result

    manifest_path = root / MANIFEST_FILENAME
    if not manifest_path.is_file():
        result.add("BOT001", f"missing {MANIFEST_FILENAME}")
        _scan_secrets(root, result)
        return result

    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        result.add("BOT001", f"failed to parse {MANIFEST_FILENAME}: {exc}")
        _scan_secrets(root, result)
        return result

    if not isinstance(raw, dict):
        result.add("BOT001", f"{MANIFEST_FILENAME} must contain a YAML mapping")
        _scan_secrets(root, result)
        return result

    result.manifest = raw

    # BOT002: id & version
    bot_id = raw.get("id")
    if not isinstance(bot_id, str) or not ID_RE.match(bot_id.strip()):
        result.add("BOT002", f"'id' must match {ID_RE.pattern}")
    version = raw.get("version", "0.1.0")
    if not isinstance(version, str) or not SEMVER_RE.match(version.strip()):
        result.add("BOT002", f"'version' must be semver (got {version!r})")

    # BOT003: style, backend, target_surface, shared_ro_mmap
    style = raw.get("style")
    if not isinstance(style, str) or style.strip() not in BOT_STYLES:
        result.add("BOT003", f"'style' must be one of {BOT_STYLES} (got {style!r})")
    backend = raw.get("backend", "auto")
    if not isinstance(backend, str) or backend.strip() not in BOT_BACKENDS:
        result.add("BOT003", f"'backend' must be one of {BOT_BACKENDS} (got {backend!r})")
    target_surface = raw.get("target_surface")
    if target_surface is not None:
        if not isinstance(target_surface, str) or target_surface.strip() not in BOT_TARGET_SURFACES:
            result.add(
                "BOT003",
                f"'target_surface' must be one of {BOT_TARGET_SURFACES} (got {target_surface!r})",
            )
    shared_ro_mmap = raw.get("shared_ro_mmap")
    if shared_ro_mmap is not None and not isinstance(shared_ro_mmap, bool):
        result.add("BOT003", "'shared_ro_mmap' must be a boolean")

    # BOT004: name, instructions, model, conversation_starters, model_sha256
    for req_field in ("name", "instructions", "model"):
        val = raw.get(req_field)
        if not isinstance(val, str) or not val.strip():
            result.add("BOT004", f"'{req_field}' must be a non-empty string")
        elif "\x00" in val:
            result.add("BOT004", f"'{req_field}' must not contain null bytes")
    model_val = raw.get("model")
    if isinstance(model_val, str) and model_val.strip().startswith("-"):
        result.add("BOT004", "'model' must not start with '-'")
    model_sha256 = raw.get("model_sha256")
    if model_sha256 is not None:
        if not isinstance(model_sha256, str) or not SHA256_RE.match(model_sha256.strip()):
            result.add(
                "BOT004",
                "'model_sha256' must be a 64-character hex SHA-256 digest",
            )
    starters = raw.get("conversation_starters")
    if starters is not None:
        if not isinstance(starters, list) or not all(isinstance(s, str) for s in starters):
            result.add("BOT004", "'conversation_starters' must be a list of strings")

    # BOT005: tools.modules & actions[].openapi_spec
    _validate_tools_and_actions(
        root, raw, result, allow_local_action_urls=allow_local_action_urls
    )

    # BOT006: knowledge.paths & symlinks
    _validate_knowledge_and_symlinks(root, raw, result)

    # BOT007: secret scan across bot directory
    _scan_secrets(root, result)

    # BOT008: generation_config bounds & safety_settings
    _validate_generation_and_safety(raw, result)

    return result
