# Claude Code Studio

`claude-code` · version `3.8.73` · 5.0 KB

**[Download claude-code-3.8.73.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.73/claude-code-3.8.73.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.73/claude-code-3.8.73.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.73/claude-code-3.8.73.tar.gz
tar xzf claude-code-3.8.73.tar.gz
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

sha256 `85315b66d97fec82161d178116edf7477b17d1a0f1ea1f1f45e31b5e65eca389`  
Built from `v3.8.73` (adk 3.8.73). [All packs](../packs.md)
