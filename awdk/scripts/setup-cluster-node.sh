#!/usr/bin/env bash
# ============================================================================
# AitherADK Grid -- Cluster Node Setup (CPU inference)
# ============================================================================
# Sets up a mini PC as a CPU inference node using llama.cpp server.
# Run this on each mini PC in your cluster.
#
# Usage:
#   bash scripts/setup-cluster-node.sh
#   bash scripts/setup-cluster-node.sh https://custom-url/model.gguf
#
# Environment:
#   LLAMACPP_PORT  Port for llama.cpp server (default: 8121)
#   MODEL_DIR      Model storage directory (default: /opt/aither/models)
# ============================================================================
set -euo pipefail

MODEL_URL="${1:-https://huggingface.co/Qwen/Qwen2.5-32B-Instruct-GGUF/resolve/main/qwen2.5-32b-instruct-q4_k_m.gguf}"
MODEL_DIR="${MODEL_DIR:-/opt/aither/models}"
LLAMACPP_PORT="${LLAMACPP_PORT:-8121}"

echo ""
echo "  AitherADK Grid -- Cluster Node Setup"
echo "  ========================================"
echo ""

# Step 1: Install llama.cpp
if ! command -v llama-server &>/dev/null; then
  echo "  [1/4] Installing llama.cpp..."
  if [[ "$(uname)" == "Linux" ]]; then
    BUILD_DIR=$(mktemp -d)
    git clone --depth 1 https://github.com/ggerganov/llama.cpp "$BUILD_DIR/llama.cpp"
    cd "$BUILD_DIR/llama.cpp"
    make -j"$(nproc)" llama-server
    sudo cp llama-server /usr/local/bin/
    cd /
    rm -rf "$BUILD_DIR"
  else
    echo "  [!] Non-Linux detected. Install llama.cpp manually:"
    echo "      https://github.com/ggerganov/llama.cpp#build"
    exit 1
  fi
else
  echo "  [1/4] llama.cpp already installed"
fi

# Step 2: Download model
sudo mkdir -p "$MODEL_DIR"
if [ ! -f "$MODEL_DIR/model.gguf" ]; then
  echo "  [2/4] Downloading Qwen2.5-32B Q4_K_M (~20GB)..."
  echo "        This will take a while depending on your connection."
  sudo curl -L --progress-bar -o "$MODEL_DIR/model.gguf" "$MODEL_URL"
else
  echo "  [2/4] Model already downloaded at $MODEL_DIR/model.gguf"
fi

# Step 3: Create systemd service (re-running updates the unit file)
echo "  [3/4] Creating systemd service..."
sudo tee /etc/systemd/system/aither-llamacpp.service > /dev/null <<SERVICEEOF
[Unit]
Description=AitherADK llama.cpp Inference Server
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/llama-server \\
  --model $MODEL_DIR/model.gguf \\
  --host 0.0.0.0 \\
  --port $LLAMACPP_PORT \\
  --ctx-size 4096 \\
  --threads $(nproc) \\
  --parallel 2 \\
  --mlock \\
  --api-oai
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICEEOF

sudo systemctl daemon-reload
sudo systemctl enable --now aither-llamacpp

# Step 4: Verify
echo "  [4/4] Verifying..."
sleep 3
if curl -sf "http://localhost:$LLAMACPP_PORT/health" > /dev/null 2>&1; then
  echo "  [+] llama.cpp server is healthy on port $LLAMACPP_PORT"
else
  echo "  [!] Health check failed. Check: journalctl -u aither-llamacpp -f"
fi

LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "<this-node-ip>")

echo ""
echo "  ========================================"
echo "  Cluster node ready!"
echo "  ========================================"
echo ""
echo "  llama.cpp server: http://${LOCAL_IP}:${LLAMACPP_PORT}"
echo ""
echo "  Add this node to your main PC config:"
echo "    export AITHER_GRID_CLUSTER_NODES='[\"${LOCAL_IP}\"]'"
echo ""
echo "  Or if you have multiple nodes:"
echo "    export AITHER_GRID_CLUSTER_NODES='[\"192.168.1.10\",\"192.168.1.11\",\"192.168.1.12\"]'"
echo ""
echo "  To update an existing installation (e.g. add --api-oai flag):"
echo "    Re-run this script — it overwrites the systemd unit and restarts the service."
echo ""
