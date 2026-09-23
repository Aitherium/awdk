# Claude Code Studio

`claude-code` · version `3.8.24` · 5.0 KB

**[Download claude-code-3.8.24.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.24/claude-code-3.8.24.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.24/claude-code-3.8.24.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.24/claude-code-3.8.24.tar.gz
tar xzf claude-code-3.8.24.tar.gz
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

sha256 `faf5a8bbbc5c3c7704f6bd7ed658b2196a7abc0a8a2b97c3a76a612d10825e70`  
Built from `v3.8.24` (adk 3.8.24). [All packs](../packs.md)
