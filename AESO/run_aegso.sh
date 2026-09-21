#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage:
  ./run_aegso.sh <role> <node-id> <dest-ip>

Timing (all nodes share PTP clock -> same schedule):
  SLOT          seconds per combo slot   (default 10)
  REP_OFFSET    repeater start offset in slot (default 0)
  CLIENT_OFFSET clients start offset in slot  (default 1)

Launch all three within one SLOT window -> they lockstep.
Repeater starts CLIENT_OFFSET seconds before the clients, every combo.

Examples:
  SLOT=10 bash run_aegso.sh repeater - 192.168.0.226
  SLOT=10 bash run_aegso.sh client A  192.168.0.226
  SLOT=10 bash run_aegso.sh client B  192.168.0.226
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

# ── Run configuration ──
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

# ── Lockstep timing (shared via PTP wall clock) ──
SLOT="${SLOT:-10}"
REP_OFFSET="${REP_OFFSET:-0}"
CLIENT_OFFSET="${CLIENT_OFFSET:-1}"

if [[ "$ROLE" == "client" ]]; then
  OFFSET="$CLIENT_OFFSET"
else
  OFFSET="$REP_OFFSET"
fi

# First slot boundary: next multiple of SLOT on the shared clock.
# All nodes launched within one SLOT window compute the same BASE.
NOW0="$(date +%s)"
BASE=$(( (NOW0 / SLOT + 1) * SLOT ))

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
    *)   echo "For client, node-id must be A or B" >&2; exit 1 ;;
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

run_python() {
  echo "[run_aegso] CMD: $*"
  "$PYTHON" "$PY" "$@"
}

total=$(( ${#PGENS[@]} * ${#PSWAPS[@]} ))
combo_idx=0

echo "[run_aegso] role=$ROLE node=${NODE_ID:--} base=$BASE slot=$SLOT offset=$OFFSET combos=$total"

for pgen in "${PGENS[@]}"; do
  for pswap in "${PSWAPS[@]}"; do
    combo_idx=$((combo_idx + 1))

    # Wall-clock anchor: this combo starts at BASE + (idx-1)*SLOT + OFFSET.
    slot_start=$(( BASE + (combo_idx - 1) * SLOT + OFFSET ))
    now="$(date +%s)"
    delta=$(( slot_start - now ))

    echo "============================================================"
    if (( delta > 0 )); then
      echo "[run_aegso] ($combo_idx/$total) pgen=$pgen pswap=$pswap -> slot $slot_start (wait ${delta}s)"
      sleep "$delta"
    else
      echo "[run_aegso] ($combo_idx/$total) pgen=$pgen pswap=$pswap -> slot $slot_start (overrun, now)"
    fi
    echo "============================================================"

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
  done
done

echo "[run_aegso] Done. role=$ROLE node=${NODE_ID:--}"