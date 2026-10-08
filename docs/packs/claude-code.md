# Claude Code Studio

`claude-code` · version `3.8.63` · 5.0 KB

**[Download claude-code-3.8.63.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.63/claude-code-3.8.63.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.63/claude-code-3.8.63.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.63/claude-code-3.8.63.tar.gz
tar xzf claude-code-3.8.63.tar.gz
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

sha256 `e506f174d2b2f2b73a1f3d15852bb79b0163505ecc3c874781c3eb54888308ec`  
Built from `v3.8.63` (adk 3.8.63). [All packs](../packs.md)
