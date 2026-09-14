# Claude Code Studio

`claude-code` · version `3.8.18` · 5.0 KB

**[Download claude-code-3.8.18.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/claude-code-3.8.18.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/claude-code-3.8.18.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/claude-code-3.8.18.tar.gz
tar xzf claude-code-3.8.18.tar.gz
python claude-code/install.py
```

Installs to `~/.aither/packs/claude-code/`, which adk discovers with no
configuration. The installer verifies the pack is discoverable rather than
assuming it. adk itself:

```bash
pip install aither-adk
```

## About

A coding-focused agent for feature development, debugging, testing,
refactoring, and code review. Works with any programming language and
integrates with version control.

## Skills

- `debugging`
- `feature-development`

## Contents

```
agent.yaml
brain_pack.yaml
skills/debugging.md
skills/feature-development.md
```

---

sha256 `40709d6bd9f13b33115f44ac35f3095ad7c1d70ddac64027d56dd3e86a09536f`  
Built from `v3.8.18` (adk 3.8.18). [All packs](../packs.md)
