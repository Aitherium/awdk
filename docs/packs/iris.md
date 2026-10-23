# Iris Visual Artisan

`iris` · version `3.8.89` · 8.2 KB

**[Download iris-3.8.89.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.89/iris-3.8.89.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.89/iris-3.8.89.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.89/iris-3.8.89.tar.gz
tar xzf iris-3.8.89.tar.gz
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

sha256 `0c2aa6e1a634ce8f33c3ba34862337237181be7dc96899f5d9f91602026fb901`  
Built from `v3.8.89` (adk 3.8.89). [All packs](../packs.md)
