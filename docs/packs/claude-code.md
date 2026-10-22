# Claude Code Studio

`claude-code` · version `3.8.88` · 5.0 KB

**[Download claude-code-3.8.88.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.88/claude-code-3.8.88.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.88/claude-code-3.8.88.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.88/claude-code-3.8.88.tar.gz
tar xzf claude-code-3.8.88.tar.gz
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

sha256 `73a475d2f19fd0a266f08ce10b6e3f20276032053f0de32a6a7591bc62ba5b13`  
Built from `v3.8.88` (adk 3.8.88). [All packs](../packs.md)
