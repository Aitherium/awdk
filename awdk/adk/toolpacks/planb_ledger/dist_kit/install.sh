#!/usr/bin/env bash
# Plan B Ledger — self-contained setup wizard (macOS / Linux)
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

echo ""
echo "  ============================================"
echo "   PLAN B LEDGER  -  one ledger, two faces"
echo "   local-first - no cloud - yours"
echo "  ============================================"
echo ""

# 1. Python
PY=""
for c in python3 python; do
  if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "  [X] Python 3.10+ not found. Install it (https://python.org) and re-run."
  exit 1
fi
echo "  [OK] Python found ($($PY --version))"

# 2. venv + deps
if [ ! -d "$HERE/.venv" ]; then
  echo "  [..] Creating virtual environment..."
  "$PY" -m venv "$HERE/.venv"
fi
VPY="$HERE/.venv/bin/python"
echo "  [..] Installing dependencies (discord.py, httpx)..."
"$VPY" -m pip install --quiet --upgrade pip
"$VPY" -m pip install --quiet -r "$HERE/requirements.txt"
echo "  [OK] Dependencies installed"

# 3. Discord token
echo ""
echo "  -- Discord bot setup --------------------------------------"
echo "  1. https://discord.com/developers/applications -> New Application"
echo "  2. Bot -> Reset Token -> copy;  enable MESSAGE CONTENT INTENT"
echo "  3. OAuth2 URL Generator: scope 'bot' + Send Messages/Attach Files ->"
echo "     open the URL, add the bot to your server"
echo ""
printf "  Paste your Discord bot token (Enter to skip -> CLI-only): "
read -r TOKEN

# 4. Local brain
ENDPOINT="http://127.0.0.1:8090"
if curl -s -m 3 "$ENDPOINT/v1/models" >/dev/null 2>&1; then
  echo "  [OK] Local brain live: bonsai (llama.cpp) at $ENDPOINT"
else
  echo "  [i] No local model at $ENDPOINT yet."
  echo "      I can download the bonsai brain now (236MB-3.6GB by your RAM,"
  echo "      fully offline after that, runs on CPU) - or skip: the built-in"
  echo "      parser works without it."
  printf "  Download + start the local brain now? [y/N]: "
  read -r DL
  if [ "$DL" = "y" ]; then
    "$VPY" -m planb.bootstrap auto || \
      echo "  [i] Brain bootstrap incomplete - built-in parser will be used."
  else
    printf "  Different llama.cpp endpoint? (Enter to keep): "
    read -r CUSTOM
    [ -n "$CUSTOM" ] && ENDPOINT="$CUSTOM"
  fi
fi

# 5. Config
DATA="$HOME/.aither/planb"
mkdir -p "$DATA"
if [ -n "$TOKEN" ]; then
  printf '{"llm_endpoint": "%s", "discord_token": "%s"}\n' "$ENDPOINT" "$TOKEN" > "$DATA/config.json"
else
  printf '{"llm_endpoint": "%s"}\n' "$ENDPOINT" > "$DATA/config.json"
fi
chmod 600 "$DATA/config.json"
echo "  [OK] Config written to $DATA/config.json"

# 6. Seed + launchers
printf "  Load demo data? [Y/n]: "
read -r SEED
[ "$SEED" != "n" ] && "$VPY" -m planb.cli seed

cat > "$HERE/run-bot.sh" <<EOF
#!/usr/bin/env bash
cd "$HERE" && exec .venv/bin/python -m planb.bot
EOF
cat > "$HERE/planb" <<EOF
#!/usr/bin/env bash
cd "$HERE" && exec .venv/bin/python -m planb.cli "\$@"
EOF
chmod +x "$HERE/run-bot.sh" "$HERE/planb"

echo ""
echo "  ============================================"
echo "   DONE."
[ -n "$TOKEN" ] && echo "   Start the bot:   ./run-bot.sh   (then !help in Discord)"
echo "   CLI demo:        ./planb demo"
echo "   Print a sheet:   ./planb sheet"
echo "   Your data:       $DATA  (plain JSON - it's YOURS)"
echo "  ============================================"
if [ -n "$TOKEN" ]; then
  printf "  Start the Discord bot now? [Y/n]: "
  read -r GO
  [ "$GO" != "n" ] && exec "$VPY" -m planb.bot
fi
