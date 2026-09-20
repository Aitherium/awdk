# Iris Visual Artisan

`iris` · version `3.8.22` · 8.2 KB

**[Download iris-3.8.22.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/iris-3.8.22.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/iris-3.8.22.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/iris-3.8.22.tar.gz
tar xzf iris-3.8.22.tar.gz
python iris/install.py
```

Installs to `~/.aither/packs/iris/`, which adk discovers with no
configuration. The installer verifies the pack is discoverable rather than
assuming it. adk itself:

```bash
pip install aither-adk
```

## About

The image-generation counterpart to the aither orchestrator pack. Iris stands
up her OWN image-gen backend (via the imagegen_* bootstrap toolpack), then
generates through it — ComfyUI for control, SANA for speed. She knows the one
law that governs character work: txt2img + IPAdapter CANNOT pin a character.

## Skills

- `character-consistency`
- `deploy-image-gen`
- `image-generation`

## Contents

```
brain_pack.yaml
skills/character-consistency.md
skills/deploy-image-gen.md
skills/image-generation.md
```

---

sha256 `4e9c74a548b9f2e27388ab9df3a91edcb69cc61cfced4f79186c6350f736e2e2`  
Built from `v3.8.22` (adk 3.8.22). [All packs](../packs.md)
