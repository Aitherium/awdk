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

sha256 `6e7a0257a864a2d70097b5c788a09e5a927fc70213dcc6d0564f0531411eb328`  
Built from `v3.8.73` (adk 3.8.73). [All packs](../packs.md)
