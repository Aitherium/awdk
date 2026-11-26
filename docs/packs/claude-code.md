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

sha256 `de76f722187b1046acaacabc249766e7caacb3f36d048646e56a9731bca097ec`  
Built from `v4.0.1` (adk 4.0.1). [All packs](../packs.md)
