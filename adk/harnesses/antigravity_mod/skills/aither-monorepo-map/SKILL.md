---
name: aither-monorepo-map
description: >-
  Navigate the AitherOS-Fresh monorepo before building or modifying features. Use when
  starting work in /var/mnt/c/AitherOS-Fresh, looking for where a native app, service,
  brick, checker, or release lane lives, or orienting on prior architecture built across
  the codebase.
---

# AitherOS Monorepo Map & Exploration Ladder

Use this ladder **before** designing or creating any new app, UI surface, script, or release step.

## 1. Exploration Order (Cheap & Authoritative First)
1. **Repo ground truth (read once per session when touching `/var/mnt/c/AitherOS-Fresh`):**
   - `/var/mnt/c/AitherOS-Fresh/AGENTS.md` — standing rules, fleet/deploy reality, tool order, merge guard.
   - `/var/mnt/c/AitherOS-Fresh/.claude/rules/` — domain rules: the aw family, code search, dispatch, dev flow, quality gate and deploy reality.
   - `/var/mnt/c/AitherOS-Fresh/.claude/reference/gate-index.md` — every checker & CI gate.
2. **Prior decisions & crystallized briefs:**
   - Call `aither-local` → `recall(query=...)` before re-investigating history.
   - Call `awkno -k <verb|topic>` for existing `aw*` bricks and laws.
   - Call `aither-local` → `research(question=..., paths=[...])` with **absolute paths** (`/var/mnt/c/AitherOS-Fresh/...`) for multi-file codebase surveys instead of burning frontier turns on manual reads.
3. **Scoped native-surface check (`rg` / `find`):**
   - Check whether a real shipped surface already owns the user's request before proposing a web/CLI workaround.

## 2. Where Shipped Surfaces Live
Paths starting `awdk/` are from the repo root. Rows that name the deployment tree are inside it. Every other path is inside the repo's `AitherOS` subtree.

| Surface | Path | Notes |
| --- | --- | --- |
| Native Android phone app | `awdk/android/aither/` | Full AitherOS shell + `AssistActivity`, `SettingsActivity`, `DeskLink`, `MeshActivity`, `Updater.java` |
| Native Wear OS watch app | `awdk/android/aither-wear/` | Standalone watch app (`WearActivity`, `WearApi`, `WearVoice`, `WearTile`, `WearUpdater`) |
| Wear OS watch face | `awdk/android/aither-watchface/` | Watch Face Format v2 (resources only, no dex) |
| Web OS / Portal (Veil) | `apps/AitherVeil/` | Living OS desktop/phone web UI, blog, `/app` download manifest (`public/aither-android.json`) |
| Core Services | `services/` & `apps/` | Genesis, Security Core / Identity, MicroScheduler, Comet, Pulse, Flux |
| `aw*` Bricks & Packages | `packages/aw*/` & `awdk/` | `awpool`, `awgit`, `awrise`, `awstorage`, `awvoice`, `adk`, etc. |
| Bare-metal / `awnix` OS | the monorepo deployment tree, `standalone/bootc/` | `desktop-hypr/`, `awnix-models.yaml`, `awnix-setup`, `awnix-voice-remote` |
| Compose & Runtime | the monorepo deployment tree, `compose/docker-compose.aitheros.yml` | Canonical fleet compose (root file is an include stub) |
| Official Dev / Release Lanes | `dev/tools/` | `release_aither_android.py`, `sign_aither_apk.py`, `aither_secret.py`, `check_android_components.py`, `run_gates.py` |
| Android & Platform Tests | `dev/tests/` | `test_android_*.py`, `test_release_aither_android.py` |

## 3. Golden Rule
If a shipped surface or a dev-tools lane already exists, **extend it and ship through it** — never build a parallel throwaway app or bypass the release lane with raw `gh release create`.
