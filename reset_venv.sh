#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python3.10 >/dev/null 2>&1; then
  echo "python3.10 not found in PATH" >&2
  exit 1
fi

rm -rf .venv
python3.10 -m venv .venv

# shellcheck disable=SC1091
source .venv/bin/activate
python -V

echo "Virtualenv recreated and activated."
