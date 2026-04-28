#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
TARGET_DIR="${HERMES_HOME}/plugins/napcat_qq_bridge"
RUNTIME_DIR="${HERMES_HOME}/napcat_qq_bridge"
TMP_DIR="${TARGET_DIR}.tmp.$$"

mkdir -p "${HERMES_HOME}/plugins"
mkdir -p "${RUNTIME_DIR}"
rm -rf "${TMP_DIR}"
cp -r "${REPO_DIR}/napcat_qq_bridge" "${TMP_DIR}"
rm -rf "${TARGET_DIR}"
mv "${TMP_DIR}" "${TARGET_DIR}"

if [ ! -f "${RUNTIME_DIR}/config.json" ]; then
  sed "s#/home/YOUR_USER#${HOME}#g" "${TARGET_DIR}/config.example.json" > "${RUNTIME_DIR}/config.json"
  SEEDED_CONFIG=1
else
  SEEDED_CONFIG=0
fi

echo "Installed plugin to ${TARGET_DIR}"
echo "Runtime directory: ${RUNTIME_DIR}"
if [ "${SEEDED_CONFIG}" = "1" ]; then
  echo "Seeded config: ${RUNTIME_DIR}/config.json"
else
  echo "Kept existing config: ${RUNTIME_DIR}/config.json"
fi
echo ""
echo "Next steps:"
echo "  1. Edit ${RUNTIME_DIR}/config.json"
echo "  2. Verify command registration: hermes napcat-qq-bridge --help"
echo "  3. Start the bridge: hermes napcat-qq-bridge run"
echo "  4. Optional systemd template: examples/systemd/hermes-napcat-qq-bridge.service"
echo "  5. Health check after startup: curl http://127.0.0.1:8096/healthz"
