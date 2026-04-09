#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
TARGET_DIR="${HERMES_HOME}/plugins/napcat_qq_bridge"
TMP_DIR="${TARGET_DIR}.tmp.$$"

mkdir -p "${HERMES_HOME}/plugins"
rm -rf "${TMP_DIR}"
cp -r "${REPO_DIR}/napcat_qq_bridge" "${TMP_DIR}"
rm -rf "${TARGET_DIR}"
mv "${TMP_DIR}" "${TARGET_DIR}"

echo "Installed plugin to ${TARGET_DIR}"
echo "Verify with: hermes napcat-qq-bridge --help"

