# Claude Code Studio

`claude-code` · version `3.8.43` · 5.0 KB

**[Download claude-code-3.8.43.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.43/claude-code-3.8.43.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.43/claude-code-3.8.43.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.43/claude-code-3.8.43.tar.gz
tar xzf claude-code-3.8.43.tar.gz
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

sha256 `8a5a90fb24ad66298d3a94b84f9233f36c3fdcba2d526b05f96d1b17d13c2b11`  
Built from `v3.8.43` (adk 3.8.43). [All packs](../packs.md)
