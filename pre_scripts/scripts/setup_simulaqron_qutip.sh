#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
NETWORK_SOURCE="${NETWORK_SOURCE:-${ROOT_DIR}/config/madrid_nodes_clean.json}"
NETWORK_CONFIG="${NETWORK_CONFIG:-${ROOT_DIR}/config/madrid_3nodes.json}"
BACKEND="${BACKEND:-qutip}"
NOISY_QUBITS="${NOISY_QUBITS:-off}"
T1_VALUE="${T1_VALUE:-1.0}"

cd "${ROOT_DIR}"

python3 -m venv "${VENV_DIR}"
source "${VENV_DIR}/bin/activate"

python -m pip install --upgrade pip
pip install -r requirements.txt

python "${ROOT_DIR}/scripts/build_simulaqron_network.py" \
  --input "${NETWORK_SOURCE}" \
  --output "${NETWORK_CONFIG}"

NETWORK_NAME="$(
python - "${NETWORK_SOURCE}" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    print(json.load(handle)["network_name"])
PY
)"

simulaqron set backend "${BACKEND}"
simulaqron set network-config-file "${NETWORK_CONFIG}"
simulaqron set noisy-qubits "${NOISY_QUBITS}"
simulaqron set t1 "${T1_VALUE}"

python - <<'PY'
from importlib import metadata

packages = ["simulaqron", "qutip", "scipy", "numpy"]
for package in packages:
    print(f"{package}=={metadata.version(package)}")
PY

echo "Configured network name: ${NETWORK_NAME}"
echo "Clean network source: ${NETWORK_SOURCE}"
echo "Configured network file: ${NETWORK_CONFIG}"
echo "Configured backend: ${BACKEND}"
echo "Configured noisy qubits: ${NOISY_QUBITS}"
echo "Configured T1: ${T1_VALUE}"
