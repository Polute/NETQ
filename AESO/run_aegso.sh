#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage:
  ./run_aegso.sh <role> <node-id> <dest-ip>

Examples:
  bash run_aegso.sh repeater - 192.168.0.226
  bash run_aegso.sh client A  192.168.0.226
  bash run_aegso.sh client B  192.168.0.226
EOF
  exit 1
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
fi

if [[ $# -lt 3 ]]; then
  usage
fi

ROLE="$1"
NODE_ID="$2"
DEST_IP="$3"

case "$ROLE" in
  client|c|C)    ROLE="client" ;;
  repeater|r|R)  ROLE="repeater" ;;
  *)
    echo "Unknown role: $ROLE" >&2
    usage
    ;;
esac

PY="${PY:-$SCRIPT_DIR/aegso.py}"
PYTHON="${PYTHON:-$(command -v python3 || echo /usr/bin/python3)}"

if [[ ! -f "$PY" ]]; then
  echo "aegso.py not found: $PY" >&2
  exit 1
fi
if [[ "$PY" != /* ]]; then
  PY="$(pwd)/$PY"
fi

# ── Execution Configuration ──
COUNT="${COUNT:-1000000000}"
ATTEMPT_TIMEOUT="${ATTEMPT_TIMEOUT:-0.5}"
SWAP_WAIT_TIMEOUT="${SWAP_WAIT_TIMEOUT:-3}"
REGISTER_TIMEOUT="${REGISTER_TIMEOUT:-60.0}"
SOCK_BUF="${SOCK_BUF:-65536}"
BUSY_POLL_US="${BUSY_POLL_US:-50}"
RT_PRIORITY="${RT_PRIORITY:-50}"
WERNER_A0="${WERNER_A0:-1.0}"
WERNER_B0="${WERNER_B0:-1.0}"
WERNER_THRESHOLD="${WERNER_THRESHOLD:-0.3333333333333333}"
PORT_A="${PORT_A:-7401}"
PORT_B="${PORT_B:-7402}"
CPU_REP="${CPU_REP:-1}"
CPU_A="${CPU_A:-1}"
CPU_B="${CPU_B:-1}"

EXEC_DURATION="${EXEC_DURATION:-60}"
EXEC_TIMEOUT="${EXEC_DURATION}s"
COMBO_WAIT="${COMBO_WAIT:-40}"

# Define initial startup delays depending on node role
if [[ "$ROLE" == "repeater" ]]; then
  START_DELAY=0
elif [[ "$ROLE" == "client" ]]; then
  case "$NODE_ID" in
    A|a) START_DELAY=5 ;;
    B|b) START_DELAY=10 ;;
    *)   echo "For client, node-id must be A or B" >&2; exit 1 ;;
  esac
fi

# Full pgen x pswap x Tcoh sweep. The deterministic seed keeps every node in the
# same randomized order; completed combinations are recorded per role/node.
read -r -a PGENS  <<< "${PGENS:-1.0 0.9 0.8 0.7 0.6 0.5 0.4 0.3 0.2 0.1}"
read -r -a PSWAPS <<< "${PSWAPS:-1.0 0.9 0.8 0.7 0.6 0.5 0.4 0.3 0.2 0.1}"
read -r -a TCOHS <<< "${TCOHS:-inf 100000000 5000000 1000000 500000 400000 250000 200000 150000 100000}"
SHUFFLE_SEED="${SHUFFLE_SEED:-20261126}"

if (( ${#PGENS[@]} == 0 || ${#PSWAPS[@]} == 0 || ${#TCOHS[@]} == 0 )); then
  echo "PGENS, PSWAPS, and TCOHS must contain at least one value" >&2
  exit 1
fi

if [[ "$ROLE" == "client" ]]; then
  case "$NODE_ID" in
    A|a) NODE_ID="A"; CPU="$CPU_A"; PORT="$PORT_A" ;;
    B|b) NODE_ID="B"; CPU="$CPU_B"; PORT="$PORT_B" ;;
  esac
  if [[ "$DEST_IP" == "-" || -z "$DEST_IP" ]]; then
    echo "For client, dest-ip must be the repeater IP" >&2
    exit 1
  fi
else
  LISTEN_HOST="${DEST_IP:-0.0.0.0}"
  if [[ "$LISTEN_HOST" == "-" ]]; then
    LISTEN_HOST="0.0.0.0"
  fi
fi

cd "$SCRIPT_DIR"

# Execute each combination for a fixed duration, then wait before advancing.
run_python() {
  echo "[run_aegso] CMD: timeout --signal=SIGINT --kill-after=5s $EXEC_TIMEOUT $PYTHON $PY $*"
  if timeout --signal=SIGINT --kill-after=5s "$EXEC_TIMEOUT" "$PYTHON" "$PY" "$@"; then
    status=0
  else
    status=$?
  fi
  case "$status" in
    0)
      return 0
      ;;
    124|130|137|143)
      echo "[run_aegso] Execution limit reached; continuing with the next combination."
      return 0
      ;;
    2)
      return 2
      ;;
    *)
      echo "[run_aegso] Execution failed with status $status." >&2
      return "$status"
      ;;
  esac
}

PLAN_ID="$("$PYTHON" -c '
import hashlib
import json
import sys

plan = sys.argv[1:]
print(hashlib.sha256(json.dumps(plan, separators=(",", ":")).encode()).hexdigest()[:12])
' "$SHUFFLE_SEED" "${PGENS[*]}" "${PSWAPS[*]}" "${TCOHS[*]}")"
RUN_CSV="${RUN_CSV:-$SCRIPT_DIR/run_aegso_${ROLE}_${NODE_ID}_seed${SHUFFLE_SEED}_${PLAN_ID}.csv}"
if [[ "$RUN_CSV" != /* ]]; then
  RUN_CSV="$SCRIPT_DIR/$RUN_CSV"
fi
mkdir -p "$(dirname "$RUN_CSV")"

COMBO_LIST="$("$PYTHON" -c '
import itertools
import random
import sys

seed = int(sys.argv[1])
pgens, pswaps, tcohs = (arg.split() for arg in sys.argv[2:5])
combinations = list(itertools.product(pgens, pswaps, tcohs))
random.Random(seed).shuffle(combinations)
for combination in combinations:
    print("\t".join(combination))
' "$SHUFFLE_SEED" "${PGENS[*]}" "${PSWAPS[*]}" "${TCOHS[*]}")"

mapfile -t COMBINATIONS <<< "$COMBO_LIST"
total=${#COMBINATIONS[@]}

"$PYTHON" - "$RUN_CSV" "${COMBINATIONS[@]}" <<'PY'
import csv
import os
import sys
import tempfile

path = sys.argv[1]
expected = [tuple(line.split("\t")) for line in sys.argv[2:]]
fields = [
    "iteration", "pgen", "pswap", "tcoh_ns", "status", "attempts",
    "started_at", "completed_at",
]

if os.path.exists(path) and os.path.getsize(path) > 0:
    with open(path, newline="", encoding="utf-8") as csv_file:
        reader = csv.DictReader(csv_file)
        if reader.fieldnames != fields:
            raise SystemExit(f"Unexpected CSV format in {path}")
        rows = list(reader)
    actual = [(row["pgen"], row["pswap"], row["tcoh_ns"]) for row in rows]
    if actual != expected or len(rows) != len(expected):
        raise SystemExit(f"CSV plan does not match the current parameter grid: {path}")
    if any(row["iteration"] != str(index) for index, row in enumerate(rows, start=1)):
        raise SystemExit(f"CSV iteration numbers are out of order in {path}")
    if any(row["status"] not in {"pending", "in_progress", "failed", "completed"} for row in rows):
        raise SystemExit(f"Invalid iteration status in {path}")
else:
    rows = [
        {
            "iteration": str(index),
            "pgen": pgen,
            "pswap": pswap,
            "tcoh_ns": tcoh,
            "status": "pending",
            "attempts": "0",
            "started_at": "",
            "completed_at": "",
        }
        for index, (pgen, pswap, tcoh) in enumerate(expected, start=1)
    ]
    with tempfile.NamedTemporaryFile(
        "w", newline="", encoding="utf-8", dir=os.path.dirname(path), delete=False
    ) as csv_file:
        temporary_path = csv_file.name
        writer = csv.DictWriter(csv_file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary_path, path)
PY

get_iteration_status() {
  "$PYTHON" -c '
import csv
import sys

path, iteration = sys.argv[1], int(sys.argv[2])
with open(path, newline="", encoding="utf-8") as csv_file:
    rows = csv.DictReader(csv_file)
    for row in rows:
        if int(row["iteration"]) == iteration:
            print(row["status"])
            break
    else:
        raise SystemExit(f"Iteration {iteration} is missing from {path}")
' "$RUN_CSV" "$1"
}

update_iteration_status() {
  "$PYTHON" - "$RUN_CSV" "$1" "$2" "$(date '+%Y-%m-%dT%H:%M:%S%z')" <<'PY'
import csv
import os
import sys
import tempfile

path, iteration, status, timestamp = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
with open(path, newline="", encoding="utf-8") as csv_file:
    reader = csv.DictReader(csv_file)
    fields = reader.fieldnames
    rows = list(reader)

row = rows[iteration - 1]
if int(row["iteration"]) != iteration:
    raise SystemExit(f"Iteration {iteration} is out of position in {path}")
row["status"] = status
if status == "in_progress":
    row["attempts"] = str(int(row["attempts"]) + 1)
    row["started_at"] = timestamp
    row["completed_at"] = ""
elif status == "completed":
    row["completed_at"] = timestamp

with tempfile.NamedTemporaryFile(
    "w", newline="", encoding="utf-8", dir=os.path.dirname(path), delete=False
) as csv_file:
    temporary_path = csv_file.name
    writer = csv.DictWriter(csv_file, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
os.replace(temporary_path, path)
PY
}

combo_idx=0
is_first_run=true

echo "[run_aegso] role=$ROLE node=${NODE_ID:--} exec_timeout=${EXEC_TIMEOUT} combo_wait=${COMBO_WAIT}s initial_delay=${START_DELAY}s combos=$total seed=$SHUFFLE_SEED csv=$RUN_CSV"

for combination in "${COMBINATIONS[@]}"; do
  IFS=$'\t' read -r pgen pswap tcoh <<< "$combination"
  combo_idx=$((combo_idx + 1))

  iteration_status="$(get_iteration_status "$combo_idx")"
  if [[ "$iteration_status" == "completed" ]]; then
    echo "[run_aegso] ($combo_idx/$total) pgen=$pgen pswap=$pswap tcoh=$tcoh -> ALREADY COMPLETED; skipping"
    continue
  fi

  # Apply initial start delay before the first combination that actually runs.
  if [ "$is_first_run" = true ]; then
    is_first_run=false
    if (( START_DELAY > 0 )); then
      echo "[run_aegso] Applying initial start delay of ${START_DELAY}s for $ROLE ${NODE_ID:--}..."
      sleep "$START_DELAY"
    fi
  fi

  echo "============================================================"
  echo "[run_aegso] ($combo_idx/$total) pgen=$pgen pswap=$pswap tcoh=$tcoh -> EXECUTING"
  echo "============================================================"

  run_current_combo() {
    if [[ "$ROLE" == "client" ]]; then
      run_python client \
        --node-id "$NODE_ID" \
        --repeater-host "$DEST_IP" \
        --repeater-port "$PORT" \
        --count "$COUNT" \
        --duration-seconds "$EXEC_DURATION" \
        --pgen "$pgen" \
        --pswap "$pswap" \
        --coherence-ns "$tcoh" \
        --attempt-timeout "$ATTEMPT_TIMEOUT" \
        --swap-wait-timeout "$SWAP_WAIT_TIMEOUT" \
        --cpu "$CPU" \
        --rt-priority "$RT_PRIORITY" \
        --sock-buf "$SOCK_BUF" \
        --busy-poll-us "$BUSY_POLL_US"
    else
      run_python repeater \
        --listen-host-a "$LISTEN_HOST" \
        --listen-port-a "$PORT_A" \
        --listen-host-b "$LISTEN_HOST" \
        --listen-port-b "$PORT_B" \
        --count "$COUNT" \
        --duration-seconds "$EXEC_DURATION" \
        --pgen "$pgen" \
        --pswap "$pswap" \
        --werner-a0 "$WERNER_A0" \
        --werner-b0 "$WERNER_B0" \
        --werner-threshold "$WERNER_THRESHOLD" \
        --coherence-ns "$tcoh" \
        --register-timeout "$REGISTER_TIMEOUT" \
        --cpu "$CPU_REP" \
        --rt-priority "$RT_PRIORITY" \
        --sock-buf "$SOCK_BUF" \
        --busy-poll-us "$BUSY_POLL_US"
    fi
  }

  while true; do
    update_iteration_status "$combo_idx" in_progress
    if run_current_combo; then
      status=0
    else
      status=$?
    fi
    if [[ "$status" -eq 2 ]]; then
      echo "[run_aegso] No READY for pgen=$pgen pswap=$pswap tcoh=$tcoh; retrying same combination in 2s."
      sleep 2
      continue
    fi
    if [[ "$status" -ne 0 ]]; then
      update_iteration_status "$combo_idx" failed
      exit "$status"
    fi
    break
  done

  update_iteration_status "$combo_idx" completed
  echo "[run_aegso] Iteration $combo_idx completed in $RUN_CSV"

  echo "[run_aegso] Waiting ${COMBO_WAIT}s before the next combination..."
  sleep "$COMBO_WAIT"
done

echo "[run_aegso] Done. role=$ROLE node=${NODE_ID:--}"
