# Claude Code Studio

`claude-code` · version `4.0.1` · 5.0 KB

**[Download claude-code-4.0.1.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v4.0.1/claude-code-4.0.1.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v4.0.1/claude-code-4.0.1.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v4.0.1/claude-code-4.0.1.tar.gz
tar xzf claude-code-4.0.1.tar.gz
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

sha256 `eb24e7ea5d92867ad75d2e827a074ef6b82bb89c608b23b1bdad1d65aa87cfde`  
Built from `v4.0.1` (adk 4.0.1). [All packs](../packs.md)
