# Iris Visual Artisan

`iris` · version `4.0.0` · 8.2 KB

**[Download iris-4.0.0.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v4.0.0/iris-4.0.0.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v4.0.0/iris-4.0.0.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v4.0.0/iris-4.0.0.tar.gz
tar xzf iris-4.0.0.tar.gz
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

sha256 `8c4d16a2036003d96599f714c96da792a43fcce66aa75fe8d9658efbe9fac8f1`  
Built from `v4.0.0` (adk 4.0.0). [All packs](../packs.md)
