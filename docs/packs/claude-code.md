# Claude Code Studio

`claude-code` · version `3.8.48` · 5.0 KB

**[Download claude-code-3.8.48.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.48/claude-code-3.8.48.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.48/claude-code-3.8.48.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.48/claude-code-3.8.48.tar.gz
tar xzf claude-code-3.8.48.tar.gz
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

sha256 `90f6fd53a4652288d16eb032ba3e915e467464fc8d76afa13b7b8ab8e762a5f7`  
Built from `v3.8.48` (adk 3.8.48). [All packs](../packs.md)
