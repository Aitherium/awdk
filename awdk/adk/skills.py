"""Skill extraction, storage, and retrieval for ADK agents."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("adk.skills")


@dataclass
class Skill:
    """A learned, reusable agent skill."""

    name: str
    description: str
    steps: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    success_count: int = 0
    last_used: str | None = None  # ISO-8601
    tags: list[str] = field(default_factory=list)
    version: int = 1

    def touch(self) -> None:
        """Record a successful use of this skill."""
        self.success_count += 1
        self.last_used = datetime.now(timezone.utc).isoformat()


def _slugify(name: str) -> str:
    """Convert a skill name to a filesystem-safe slug.

    Args:
        name: Human-readable skill name.

    Returns:
        Lowercase slug with hyphens replacing non-alphanumeric runs.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "unnamed-skill"


class SkillStore:
    """Persistent skill storage backed by individual JSON files.

    Skills are stored in ``~/.aither/skills/`` (or a custom directory) as
    ``<slugified-name>.json``.
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        self._dir = (data_dir or Path.home() / ".aither") / "skills"
        self._dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def save(self, skill: Skill) -> Path:
        """Persist a skill to disk.

        If a skill with the same slug already exists the file is overwritten
        and the version is bumped.

        Args:
            skill: The skill to save.

        Returns:
            Path to the written JSON file.
        """
        path = self._path_for(skill.name)
        existing = self._load_path(path)
        if existing is not None:
            skill.version = existing.version + 1
            skill.success_count = max(skill.success_count, existing.success_count)
        try:
            path.write_text(json.dumps(asdict(skill), indent=2), encoding="utf-8")
            logger.info("Saved skill %r (v%d) -> %s", skill.name, skill.version, path)
        except OSError as exc:
            logger.error("Failed to save skill %r: %s", skill.name, exc)
            raise
        return path

    def load(self, name: str) -> Skill | None:
        """Load a skill by name.

        Args:
            name: Human-readable skill name (will be slugified).

        Returns:
            The skill, or ``None`` if not found.
        """
        return self._load_path(self._path_for(name))

    def delete(self, name: str) -> bool:
        """Delete a skill by name.

        Args:
            name: Human-readable skill name.

        Returns:
            ``True`` if a file was removed, ``False`` if it didn't exist.
        """
        path = self._path_for(name)
        if path.exists():
            path.unlink()
            logger.info("Deleted skill %r", name)
            return True
        return False

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def list_all(self) -> list[Skill]:
        """Return every stored skill, sorted by success_count descending.

        Returns:
            List of all skills.
        """
        skills: list[Skill] = []
        for path in sorted(self._dir.glob("*.json")):
            skill = self._load_path(path)
            if skill is not None:
                skills.append(skill)
        skills.sort(key=lambda s: s.success_count, reverse=True)
        return skills

    def search(self, query: str, k: int = 5) -> list[Skill]:
        """Search skills by keyword matching against name, description, steps, and tags.

        Each skill is scored by the number of query tokens that appear
        (case-insensitive) in its combined text.  Only skills with at least
        one hit are returned, ordered by descending score.

        Args:
            query: Free-text search query.
            k: Maximum number of results.

        Returns:
            Up to *k* matching skills.
        """
        tokens = [t.lower() for t in re.split(r"\s+", query.strip()) if t]
        if not tokens:
            return []

        scored: list[tuple[int, Skill]] = []
        for skill in self.list_all():
            blob = " ".join([
                skill.name,
                skill.description,
                " ".join(skill.steps),
                " ".join(skill.tags),
                " ".join(skill.tools_used),
            ]).lower()
            score = sum(1 for t in tokens if t in blob)
            if score > 0:
                scored.append((score, skill))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [s for _, s in scored[:k]]

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_agentskills(self) -> dict[str, Any]:
        """Export all skills in agentskills.io interchange format.

        Returns:
            Dict with version, agent identifier, and skill list.
        """
        skills = self.list_all()
        return {
            "version": "1.0",
            "agent": "adk",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "skills": [asdict(s) for s in skills],
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _path_for(self, name: str) -> Path:
        return self._dir / f"{_slugify(name)}.json"

    def _load_path(self, path: Path) -> Skill | None:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Skill(**{
                k: v for k, v in data.items()
                if k in Skill.__dataclass_fields__
            })
        except (json.JSONDecodeError, TypeError, OSError) as exc:
            logger.warning("Corrupt skill file %s: %s", path, exc)
            return None


class SkillExtractor:
    """Extract reusable skills from completed agent sessions.

    Analyzes a conversation (list of message dicts) and an optional list of
    tool names to determine whether the session performed a learnable,
    multi-step task.  Simple Q&A or sessions with fewer than 2 tool calls
    are ignored.
    """

    # Minimum tool calls to consider a session skill-worthy.
    MIN_TOOL_CALLS = 2
    # Minimum assistant messages that look like steps.
    MIN_STEP_MESSAGES = 1

    def extract_from_session(
        self,
        messages: list[dict[str, Any]],
        tools_called: list[str] | None = None,
    ) -> Skill | None:
        """Extract a skill from a completed session.

        Args:
            messages: Conversation messages, each with at least ``role`` and
                ``content`` keys.  Tool-call messages may also carry a
                ``tool_calls`` list (OpenAI-style) or a ``name`` field.
            tools_called: Optional explicit list of tool names used.  If
                ``None``, tools are inferred from the messages.

        Returns:
            A :class:`Skill` instance, or ``None`` if the session does not
            contain a meaningful learnable skill.
        """
        if not messages:
            return None

        task = self._extract_task(messages)
        tools = self._collect_tools(messages, tools_called)
        steps = self._extract_steps(messages)

        # Gate: need enough substance to be a real skill.
        if len(tools) < self.MIN_TOOL_CALLS and len(steps) < self.MIN_STEP_MESSAGES:
            return None

        name = self._derive_name(task)
        description = task or "Skill extracted from agent session"
        tags = self._derive_tags(tools, steps)

        return Skill(
            name=name,
            description=description,
            steps=steps,
            tools_used=sorted(set(tools)),
            success_count=1,
            last_used=datetime.now(timezone.utc).isoformat(),
            tags=tags,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_task(self, messages: list[dict[str, Any]]) -> str:
        """Find the first substantive user message as the task description."""
        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = (msg.get("content") or "").strip()
            if len(content) > 10:
                # Truncate very long prompts to keep the description concise.
                return content[:300].rstrip()
        return ""

    def _extract_steps(self, messages: list[dict[str, Any]]) -> list[str]:
        """Collect assistant messages that describe actions taken.

        Heuristic: assistant messages longer than 20 chars that are not
        just pleasantries are treated as step descriptions.  Each is
        truncated to a single line for brevity.
        """
        steps: list[str] = []
        trivial = {"hello", "hi", "sure", "ok", "yes", "no", "thanks"}
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            content = (msg.get("content") or "").strip()
            if not content or len(content) < 20:
                continue
            first_line = content.split("\n", 1)[0].strip()
            if first_line.lower().rstrip(".!") in trivial:
                continue
            steps.append(first_line[:200])
        return steps

    def _collect_tools(
        self,
        messages: list[dict[str, Any]],
        explicit: list[str] | None,
    ) -> list[str]:
        """Build a deduplicated tool list from messages and/or explicit list."""
        tools: list[str] = list(explicit) if explicit else []

        for msg in messages:
            # OpenAI-style tool_calls list.
            for tc in msg.get("tool_calls") or []:
                fn = tc if isinstance(tc, str) else (tc.get("function", {}).get("name") or "")
                if fn:
                    tools.append(fn)

            # Role=tool messages carry a "name" field.
            if msg.get("role") == "tool" and msg.get("name"):
                tools.append(msg["name"])

            # Anthropic-style content blocks.
            if isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = block.get("name")
                        if name:
                            tools.append(name)

        return tools

    def _derive_name(self, task: str) -> str:
        """Create a short skill name from the task description."""
        if not task:
            return "unnamed-skill"
        # Take first 60 chars, trim to last word boundary.
        short = task[:60]
        if len(task) > 60:
            space = short.rfind(" ")
            if space > 20:
                short = short[:space]
        # Remove trailing punctuation.
        return short.rstrip(".,;:!?").strip()

    def _derive_tags(self, tools: list[str], steps: list[str]) -> list[str]:
        """Generate a small set of tags from tool names and step content."""
        tags: set[str] = set()
        tag_hints = {
            "file": "file-ops",
            "read": "file-ops",
            "write": "file-ops",
            "search": "search",
            "grep": "search",
            "git": "git",
            "commit": "git",
            "test": "testing",
            "pytest": "testing",
            "deploy": "deployment",
            "docker": "deployment",
            "api": "api",
            "http": "api",
            "bash": "shell",
            "terminal": "shell",
        }
        blob = " ".join(tools + steps).lower()
        for keyword, tag in tag_hints.items():
            if keyword in blob:
                tags.add(tag)
        return sorted(tags)
