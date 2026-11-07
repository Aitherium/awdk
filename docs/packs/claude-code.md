# Claude Code Studio

`claude-code` · version `3.8.98` · 5.0 KB

**[Download claude-code-3.8.98.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.98/claude-code-3.8.98.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.98/claude-code-3.8.98.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.98/claude-code-3.8.98.tar.gz
tar xzf claude-code-3.8.98.tar.gz
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

sha256 `30755b97b69b5d319ae47a32dcdcc1b4e8d5de7e26af4d01518cc08ee00f8578`  
Built from `v3.8.98` (adk 3.8.98). [All packs](../packs.md)
