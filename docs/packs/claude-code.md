# Claude Code Studio

`claude-code` · version `3.8.59` · 5.0 KB

**[Download claude-code-3.8.59.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.59/claude-code-3.8.59.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.59/claude-code-3.8.59.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.59/claude-code-3.8.59.tar.gz
tar xzf claude-code-3.8.59.tar.gz
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

sha256 `0affb2bfc3e27d7deb3168b9a5e9a18e1b8fc1f0a157708c731b3d1bef6232ae`  
Built from `v3.8.59` (adk 3.8.59). [All packs](../packs.md)
