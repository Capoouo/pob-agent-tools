#!/usr/bin/env bash
# Install or upgrade pob-agent-tools (`pob`) on macOS or Linux.
#
# Examples:
#   curl -fsSL https://raw.githubusercontent.com/exilemoulei/pob-agent-tools/feat/macos-support/install.sh | bash
#   POB_BRANCH=feat/macos-support ./install.sh
#   POB_PROXY=http://127.0.0.1:7890 ./install.sh

set -euo pipefail

REPO="${POB_REPO:-exilemoulei/pob-agent-tools}"
BRANCH="${POB_BRANCH:-feat/macos-support}"
INSTALL_URL="git+https://github.com/${REPO}.git@${BRANCH}#subdirectory=pob_cli"

echo "[pob-install] Target: ${REPO}@${BRANCH}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[pob-install] ERROR: python3 (>=3.11) is required. On macOS run: brew install python@3.12" >&2
  exit 1
fi

PY_OK="$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)')"
if [ "$PY_OK" != "1" ]; then
  echo "[pob-install] ERROR: Python 3.11+ is required (found $(python3 --version))." >&2
  exit 1
fi

if command -v uv >/dev/null 2>&1; then
  echo "[pob-install] Installing pob-cli via uv tool..."
  uv tool install --force "$INSTALL_URL"
elif command -v pipx >/dev/null 2>&1; then
  echo "[pob-install] Installing pob-cli via pipx..."
  pipx install --force "$INSTALL_URL"
else
  echo "[pob-install] Installing pob-cli into user site-packages via python3 -m pip..."
  python3 -m pip install --user --upgrade "$INSTALL_URL"
fi

echo "[pob-install] Installed: $(pob --version 2>/dev/null || echo 'run `hash -r` or add ~/.local/bin to PATH')"
echo ""
echo "Next step: initialize the native headless PoB engine:"
echo "  pob bootstrap"
echo "  # If behind a proxy:"
echo "  pob bootstrap --proxy http://127.0.0.1:7890"
