#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT_DIR}/.venv/bin/activate"

# Keep matplotlib/qutip from trying to write into an unavailable user config directory.
export MPLCONFIGDIR="${ROOT_DIR}/.mplconfig"
mkdir -p "${MPLCONFIGDIR}"

python "${ROOT_DIR}/minimal_qutip_network.py" "$@"
