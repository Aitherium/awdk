# Claude Code Studio

`claude-code` · version `3.8.22` · 5.0 KB

**[Download claude-code-3.8.22.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/claude-code-3.8.22.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/claude-code-3.8.22.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/claude-code-3.8.22.tar.gz
tar xzf claude-code-3.8.22.tar.gz
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

sha256 `84c38e37f9f34ac2b442eab4adbc2574fe44ebe4e5d722576881921a00e568ea`  
Built from `v3.8.22` (adk 3.8.22). [All packs](../packs.md)
