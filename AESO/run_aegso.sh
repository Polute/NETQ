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

PY="${PY:-$SCRIPT_DIR/aegso_3.py}"
PYTHON="${PYTHON:-$(command -v python3 || echo /usr/bin/python3)}"

if [[ ! -f "$PY" ]]; then
  echo "aegso.py not found: $PY" >&2
  exit 1
fi
if [[ "$PY" != /* ]]; then
  PY="$(pwd)/$PY"
fi

# ── Execution Configuration ──
COUNT="${COUNT:-2000}"
ATTEMPT_TIMEOUT="${ATTEMPT_TIMEOUT:-0.5}"
SWAP_WAIT_TIMEOUT="${SWAP_WAIT_TIMEOUT:-3.0}"
REGISTER_TIMEOUT="${REGISTER_TIMEOUT:-60.0}"
SOCK_BUF="${SOCK_BUF:-65536}"
BUSY_POLL_US="${BUSY_POLL_US:-50}"
RT_PRIORITY="${RT_PRIORITY:-50}"
COHERENCE_NS="${COHERENCE_NS:-1000000}"
WERNER_A0="${WERNER_A0:-1.0}"
WERNER_B0="${WERNER_B0:-1.0}"
WERNER_THRESHOLD="${WERNER_THRESHOLD:-0.3333333333333333}"
PORT_A="${PORT_A:-7401}"
PORT_B="${PORT_B:-7402}"
CPU_REP="${CPU_REP:-1}"
CPU_A="${CPU_A:-1}"
CPU_B="${CPU_B:-1}"

# ── Cycle Wait Configuration ──
# Full cycle duration: 600 seconds per combo
CYCLE_WAIT=600
EXEC_TIMEOUT="600s"

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

read -r -a PGENS  <<< "${PGENS:-1.0 0.9 0.8 0.7 0.6 0.5 0.4 0.3 0.2 0.1}"
read -r -a PSWAPS <<< "${PSWAPS:-1.0 0.9 0.8 0.7 0.6 0.5 0.4 0.3 0.2 0.1}"

if (( ${#PGENS[@]} == 0 || ${#PSWAPS[@]} == 0 )); then
  echo "PGENS and PSWAPS must contain at least one value" >&2
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

# Execute Python script with a safety timeout of 600s
run_python() {
  echo "[run_aegso] CMD: timeout --signal=SIGINT $EXEC_TIMEOUT $*"
  timeout --signal=SIGINT "$EXEC_TIMEOUT" "$PYTHON" "$PY" "$@" || true
}

total=$(( ${#PGENS[@]} * ${#PSWAPS[@]} ))
combo_idx=0
is_first_run=true

echo "[run_aegso] role=$ROLE node=${NODE_ID:--} cycle_wait=${CYCLE_WAIT}s initial_delay=${START_DELAY}s combos=$total"

# Loop over all pgen and pswap combinations
for pgen in "${PGENS[@]}"; do
  for pswap in "${PSWAPS[@]}"; do
    combo_idx=$((combo_idx + 1))

    # Apply initial start delay only before the very first execution
    if [ "$is_first_run" = true ]; then
      is_first_run=false
      if (( START_DELAY > 0 )); then
        echo "[run_aegso] Applying initial start delay of ${START_DELAY}s for $ROLE ${NODE_ID:--}..."
        sleep "$START_DELAY"
      fi
    fi

    echo "============================================================"
    echo "[run_aegso] ($combo_idx/$total) pgen=$pgen pswap=$pswap -> EXECUTING"
    echo "============================================================"

    # Run client or repeater based on role
    if [[ "$ROLE" == "client" ]]; then
      run_python client \
        --node-id "$NODE_ID" \
        --repeater-host "$DEST_IP" \
        --repeater-port "$PORT" \
        --count "$COUNT" \
        --pgen "$pgen" \
        --pswap "$pswap" \
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
        --pgen "$pgen" \
        --pswap "$pswap" \
        --werner-a0 "$WERNER_A0" \
        --werner-b0 "$WERNER_B0" \
        --werner-threshold "$WERNER_THRESHOLD" \
        --coherence-ns "$COHERENCE_NS" \
        --register-timeout "$REGISTER_TIMEOUT" \
        --cpu "$CPU_REP" \
        --rt-priority "$RT_PRIORITY" \
        --sock-buf "$SOCK_BUF" \
        --busy-poll-us "$BUSY_POLL_US"
    fi

    # Wait for 600 seconds before running the next combination
    echo "[run_aegso] Waiting ${CYCLE_WAIT}s to complete the cycle..."
    sleep "$CYCLE_WAIT"

  done
done

echo "[run_aegso] Done. role=$ROLE node=${NODE_ID:--}"