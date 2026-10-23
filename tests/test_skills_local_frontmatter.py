"""adk.harnesses.skills_local surfaces the skill frontmatter a front door acts on.

A skill's ``context: fork`` / ``when_to_use`` / ``argument-hint`` used to be read
by Claude Code only; every other front door (awsh, the desk, ``adk shell``)
inlined a fork-only skill into the conversation without knowing it.
"""

from __future__ import annotations

from pathlib import Path

from adk.harnesses import skills_local


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_fork_context_and_hints_are_surfaced(tmp_path):
    root = tmp_path / ".claude"
    _write(root, "skills/deep-audit/SKILL.md", (
        "---\n"
        "name: deep-audit\n"
        "description: Audit a repo end to end\n"
        "context: fork\n"
        "when_to_use: before a release\n"
        "argument-hint: <repo path>\n"
        "---\n"
        "Audit $ARGUMENTS carefully.\n"
    ))
    _write(root, "commands/quick.md", "---\ndescription: Quick one\n---\nDo it.\n")

    found = skills_local.discover(roots=[root])
    audit = found["deep-audit"].describe()
    assert audit["context"] == "fork"
    assert audit["when_to_use"] == "before a release"
    assert audit["argument_hint"] == "<repo path>"
    quick = found["quick"].describe()
    assert quick["context"] == "inline"  # absent context = inline
    assert quick["when_to_use"] == "" and quick["argument_hint"] == ""


def test_fork_skill_render_tells_the_model_to_delegate(tmp_path):
    root = tmp_path / ".claude"
    _write(root, "skills/deep-audit/SKILL.md",
           "---\ncontext: fork\n---\nAudit $ARGUMENTS carefully.\n")
    _write(root, "skills/plain/SKILL.md", "---\ndescription: x\n---\nPlain body.\n")
    found = skills_local.discover(roots=[root])

    text = skills_local.render(found["deep-audit"], "the repo")
    assert "context: fork" in text and "sub-agent" in text
    assert "Audit the repo carefully." in text
    plain = skills_local.render(found["plain"])
    assert "sub-agent" not in plain
    assert plain == "# Skill: plain\n\nPlain body.\n"  # inline render unchanged


def test_unknown_context_value_is_inline(tmp_path):
    root = tmp_path / ".claude"
    _write(root, "skills/odd/SKILL.md", "---\ncontext: sideways\n---\nBody.\n")
    assert skills_local.discover(roots=[root])["odd"].context == "inline"
    assert "fork" in skills_local.SKILL_CONTEXTS
