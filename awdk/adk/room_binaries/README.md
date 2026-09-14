# AitherRoom Bundled Binaries

This directory contains bundled AitherRoom binaries for offline deployments.

The binaries are organized by platform:
- `win-x64/` — Windows x86_64 (binary: `aither-room-win64.exe`)
- `linux-x64/` — Linux x86_64 (binary: `aither-room-linux-x64`)
- `mac-arm64/` — macOS ARM64 (binary: `aither-room-macos-arm64`)
- `mac-x64/` — macOS x86_64 (binary: `aither-room-macos-x64`)

## CI Population

These binaries are populated by the CI workflow at release time. The room worktree
builds the AitherRoom service and publishes it as a GitHub Release with:
- Binary assets (e.g., `aither-room-win64.exe`, `aither-room-linux-x64`)
- SHA256 checksum file (`checksums.sha256`)

The CI then downloads and places these binaries into their platform directories.

## Consumer Launcher Behavior

`room_launcher.py:get_room_binary()` follows this resolution order:
1. Cached binary in `~/.aither/bin/`
2. Download from GitHub Releases (tag: `room-cli-v*`) with checksum verification
3. Bundled offline binary in this directory
4. Fail gracefully (None)

For offline environments, ensure binaries are pre-populated in the appropriate
platform subdirectories.
