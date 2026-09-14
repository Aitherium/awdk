#!/usr/bin/env sh
# AitherADK installer -- curl -fsSL https://aitherium.com/install.sh | sh
set -e

echo "AitherADK Installer"
echo "==================="
echo

# Check Python
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" >/dev/null 2>&1; then
        ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
        major=$("$cmd" -c "import sys; print(sys.version_info.major)" 2>/dev/null)
        minor=$("$cmd" -c "import sys; print(sys.version_info.minor)" 2>/dev/null)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON="$cmd"
            echo "[OK] Python $ver"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "[!!] Python >= 3.10 required"
    echo
    # Try uv
    if command -v uv >/dev/null 2>&1; then
        echo "Installing Python via uv..."
        uv python install 3.12
        PYTHON="uv run python"
    elif command -v curl >/dev/null 2>&1; then
        echo "Installing uv first..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
        uv python install 3.12
        PYTHON="uv run python"
    else
        echo "Please install Python 3.10+ from https://python.org"
        exit 1
    fi
fi

# Install awdk
echo
echo "Installing awdk..."
$PYTHON -m pip install --upgrade awdk 2>/dev/null || \
    $PYTHON -m pip install --user --upgrade awdk

echo
echo "[OK] awdk installed"
echo

# Run doctor
if command -v adk >/dev/null 2>&1; then
    adk doctor
else
    $PYTHON -m adk.cli doctor 2>/dev/null || true
fi

echo
echo "Next steps:"
echo "  adk init my-agent    # Create an agent"
echo "  adk setup            # Set up GPU inference"
echo "  adk doctor           # Check system health"
echo
