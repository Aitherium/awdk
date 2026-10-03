# Claude Code Studio

`claude-code` · version `3.8.50` · 5.0 KB

**[Download claude-code-3.8.50.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.50/claude-code-3.8.50.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.50/claude-code-3.8.50.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.50/claude-code-3.8.50.tar.gz
tar xzf claude-code-3.8.50.tar.gz
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

sha256 `4c97fc30b69b1f5f4a176eb841b6e6e3cbf5baad8ce86f1056b2089308f2a360`  
Built from `v3.8.50` (adk 3.8.50). [All packs](../packs.md)
