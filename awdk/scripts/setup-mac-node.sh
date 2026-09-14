#!/usr/bin/env bash
# ============================================================================
# AitherADK Grid -- Mac Reasoning Node Setup
# ============================================================================
# Sets up a Mac (Apple Silicon or Intel) as a reasoning node using llama.cpp.
# Uses Metal GPU acceleration on Apple Silicon for fast inference.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Aitherium/awdk/main/scripts/setup-mac-node.sh | bash
#   # or
#   bash scripts/setup-mac-node.sh
#   bash scripts/setup-mac-node.sh https://custom-url/model.gguf
#
# Environment:
#   LLAMACPP_PORT  Port for llama.cpp server (default: 8121)
#   MODEL_DIR      Model storage directory (default: ~/aither/models)
# ============================================================================
set -euo pipefail

MODEL_URL="${1:-https://huggingface.co/bartowski/DeepSeek-R1-0528-Qwen3-8B-GGUF/resolve/main/DeepSeek-R1-0528-Qwen3-8B-Q4_K_M.gguf}"
MODEL_DIR="${MODEL_DIR:-$HOME/aither/models}"
LLAMACPP_PORT="${LLAMACPP_PORT:-8121}"

echo ""
echo "  AitherADK Grid -- Mac Reasoning Node"
echo "  ========================================"
echo ""

# Step 1: Install llama.cpp via Homebrew
if ! command -v llama-server &>/dev/null; then
  echo "  [1/4] Installing llama.cpp..."
  if command -v brew &>/dev/null; then
    brew install llama.cpp
  else
    echo "  [!] Homebrew not found. Installing Homebrew first..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    brew install llama.cpp
  fi
else
  echo "  [1/4] llama.cpp already installed"
fi

# Step 2: Download model
mkdir -p "$MODEL_DIR"
if [ ! -f "$MODEL_DIR/model.gguf" ]; then
  echo "  [2/4] Downloading DeepSeek-R1 8B Q4_K_M (~5GB)..."
  curl -L --progress-bar -o "$MODEL_DIR/model.gguf" "$MODEL_URL"
else
  echo "  [2/4] Model already downloaded at $MODEL_DIR/model.gguf"
fi

# Step 3: Create launchd service (macOS equivalent of systemd)
echo "  [3/4] Creating launchd service..."
PLIST_PATH="$HOME/Library/LaunchAgents/com.aitherium.llamacpp.plist"

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST_PATH" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.aitherium.llamacpp</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(command -v llama-server)</string>
        <string>--model</string>
        <string>${MODEL_DIR}/model.gguf</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>${LLAMACPP_PORT}</string>
        <string>--ctx-size</string>
        <string>4096</string>
        <string>--threads</string>
        <string>$(sysctl -n hw.ncpu 2>/dev/null || echo 4)</string>
        <string>--parallel</string>
        <string>2</string>
        <string>--gpu-layers</string>
        <string>99</string>
        <string>--api-oai</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${HOME}/aither/logs/llamacpp.log</string>
    <key>StandardErrorPath</key>
    <string>${HOME}/aither/logs/llamacpp.err</string>
</dict>
</plist>
PLISTEOF

mkdir -p "$HOME/aither/logs"

# Unload old version if present, then load
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"

# Step 4: Verify
echo "  [4/4] Verifying..."
sleep 3
if curl -sf "http://localhost:$LLAMACPP_PORT/health" > /dev/null 2>&1; then
  echo "  [+] llama.cpp server is healthy on port $LLAMACPP_PORT"
else
  echo "  [!] Health check failed. Check: tail -f ~/aither/logs/llamacpp.err"
fi

LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "<your-mac-ip>")

echo ""
echo "  ========================================"
echo "  Mac reasoning node ready!"
echo "  ========================================"
echo ""
echo "  llama.cpp server: http://${LOCAL_IP}:${LLAMACPP_PORT}"
echo "  Model: DeepSeek-R1 8B Q4_K_M (Metal accelerated)"
echo "  API:   OpenAI-compatible (/v1/chat/completions)"
echo ""
echo "  On your main PC, run:"
echo "    adk deploy grid --mac-host ${LOCAL_IP}"
echo ""
echo "  Or manually set:"
echo "    export AITHER_GRID_MAC_HOST=${LOCAL_IP}"
echo ""
echo "  To update: re-run this script (overwrites the launchd service)."
echo "  Logs: tail -f ~/aither/logs/llamacpp.log"
echo ""
