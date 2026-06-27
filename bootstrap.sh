#!/usr/bin/env bash
# TerraPilot one-time bootstrap.
#   ./bootstrap.sh
# Creates an isolated environment, installs TerraPilot, writes a starter
# config, and prints the snippet to register the MCP server with Claude.
set -euo pipefail

cd "$(dirname "$0")"
GREEN='\033[0;32m'; BOLD='\033[1m'; NC='\033[0m'
say() { echo -e "${GREEN}▸${NC} $*"; }

# 1. Resolve a Python >= 3.10 -------------------------------------------------
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  fi
done
[ -n "$PY" ] || { echo "ERROR: need Python >= 3.10 (found none). Install via 'brew install python@3.13'."; exit 1; }
say "Using $($PY --version)"

# 2. Install (prefer uv, else venv + pip) ------------------------------------
if command -v uv >/dev/null 2>&1; then
  say "Installing with uv"
  uv venv --python "$PY" .venv
  uv pip install --python .venv/bin/python -e ".[dev]"
else
  say "uv not found; using venv + pip"
  "$PY" -m venv .venv
  ./.venv/bin/python -m pip install --quiet --upgrade pip
  ./.venv/bin/python -m pip install --quiet -e ".[dev]"
fi

# 3. Starter config -----------------------------------------------------------
if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  say "Wrote config.yaml (edit repo_path / aws_profile as needed)"
else
  say "config.yaml already exists — left untouched"
fi

VENV_PY="$(pwd)/.venv/bin/python"

# 4. Self-test ----------------------------------------------------------------
say "Running self-test (mock mode)…"
"$VENV_PY" -m terrapilot.cli stacks --limit 1 >/dev/null && say "Engine OK"

# 5. Register with Claude -----------------------------------------------------
echo ""
echo -e "${BOLD}TerraPilot installed.${NC} Register it as an MCP server:"
echo ""
echo -e "  ${BOLD}Claude Code (CLI):${NC}"
echo "    claude mcp add terrapilot -- $VENV_PY -m terrapilot.server"
echo ""
echo -e "  ${BOLD}Claude Desktop${NC} (claude_desktop_config.json):"
cat <<JSON
    {
      "mcpServers": {
        "terrapilot": {
          "command": "$VENV_PY",
          "args": ["-m", "terrapilot.server"],
          "env": { "TERRAPILOT_CONFIG": "$(pwd)/config.yaml" }
        }
      }
    }
JSON
echo ""
echo -e "Try the showcase:  ${BOLD}make demo${NC}   |   run tests:  ${BOLD}make test${NC}"
