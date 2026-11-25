# Claude Code Studio

`claude-code` · version `4.0.0` · 5.0 KB

**[Download claude-code-4.0.0.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v4.0.0/claude-code-4.0.0.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v4.0.0/claude-code-4.0.0.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v4.0.0/claude-code-4.0.0.tar.gz
tar xzf claude-code-4.0.0.tar.gz
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

sha256 `fbef72cb9d47e80d36eb1256f5087dc776d8a7d5124d5d656da629ce43635302`  
Built from `v4.0.0` (adk 4.0.0). [All packs](../packs.md)
