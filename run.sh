#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
PYTHON="${VENV_DIR}/bin/python"

if [[ ! -x "${PYTHON}" ]]; then
  python3 -m venv "${VENV_DIR}"
fi

"${PYTHON}" -m pip install --disable-pip-version-check -r "${SCRIPT_DIR}/requirements.txt"

# Subcommands:
#   ./run.sh sensors [...]   live humidity/temperature display
#   ./run.sh control [...]   humidity-driven AC automation (dry >65%, off <=54%)
#   ./run.sh [...]           Gree AC control (default; backward compatible)
case "${1:-}" in
  sensors)
    shift
    exec "${PYTHON}" "${SCRIPT_DIR}/humidity_sensors.py" "$@"
    ;;
  control)
    shift
    exec "${PYTHON}" "${SCRIPT_DIR}/humidity_control.py" "$@"
    ;;
  web)
    shift
    exec "${PYTHON}" "${SCRIPT_DIR}/webapp.py" "$@"
    ;;
  *)
    exec "${PYTHON}" "${SCRIPT_DIR}/test.py" "$@"
    ;;
esac
