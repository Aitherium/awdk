# Claude Code Studio

`claude-code` · version `3.8.40` · 5.0 KB

**[Download claude-code-3.8.40.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.40/claude-code-3.8.40.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.40/claude-code-3.8.40.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.40/claude-code-3.8.40.tar.gz
tar xzf claude-code-3.8.40.tar.gz
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

sha256 `cf47506d98f9d2ef99cd62265b357ebefb75a5a5b294cd0e95d5123a6ed140f6`  
Built from `v3.8.40` (adk 3.8.40). [All packs](../packs.md)
