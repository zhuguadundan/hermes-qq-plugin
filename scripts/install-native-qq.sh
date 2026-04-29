#!/usr/bin/env bash
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_ROOT="${1:-${HERMES_ROOT:-$HOME/.hermes/hermes-agent}}"
PYTHON_BIN="${HERMES_ROOT}/venv/bin/python"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Hermes venv python not found: ${PYTHON_BIN}" >&2
  echo "Usage: scripts/install-native-qq.sh /path/to/hermes-agent" >&2
  exit 1
fi
if [[ ! -f "${HERMES_ROOT}/gateway/run.py" ]]; then
  echo "Not a Hermes checkout: ${HERMES_ROOT}" >&2
  exit 1
fi

"${PYTHON_BIN}" -m pip install -e "${PLUGIN_DIR}"
mkdir -p "${HERMES_ROOT}/gateway/platforms"
cp "${PLUGIN_DIR}/gateway_platform_shim/qq.py" "${HERMES_ROOT}/gateway/platforms/qq.py"
"${PYTHON_BIN}" "${PLUGIN_DIR}/scripts/patch_hermes_core.py" "${HERMES_ROOT}"
"${PYTHON_BIN}" -m py_compile "${HERMES_ROOT}/gateway/platforms/qq.py"

echo "Installed Hermes native QQ adapter into ${HERMES_ROOT}."
echo "Restart Hermes gateway after updating config.yaml: systemctl --user restart hermes-gateway.service"
