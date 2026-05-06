#!/bin/bash
# Benchmark EPR sender/receiver with and without quiet mode

PYTHON="/home/giicc/NETQ/.venv/bin/python"
SENDER="$PYTHON /home/giicc/NETQ/pre_scripts/minimal_epr_sender.py"
RECEIVER="$PYTHON /home/giicc/NETQ/pre_scripts/minimal_epr_receiver.py"

echo "=== EPR Performance Benchmark ==="
echo ""

# Test 1: WITH debug output
echo "Test 1: WITH debug output (verbose mode)"
echo "---"
$RECEIVER \
  --listen-port 7401 \
  --sender-host 127.0.0.1 \
  --sender-port 7400 \
  --accept-timeout 30 &
RECEIVER_PID=$!

sleep 0.5

$SENDER \
  --listen-port 7400 \
  --receiver-host 127.0.0.1 \
  --receiver-port 7401 \
  --accept-timeout 30

wait $RECEIVER_PID
echo ""
echo ""

# Test 2: WITHOUT debug output (quiet mode)
echo "Test 2: WITHOUT debug output (--quiet mode) - FAST"
echo "---"
$RECEIVER \
  --listen-port 7401 \
  --sender-host 127.0.0.1 \
  --sender-port 7400 \
  --accept-timeout 30 \
  --quiet &
RECEIVER_PID=$!

sleep 0.5

$SENDER \
  --listen-port 7400 \
  --receiver-host 127.0.0.1 \
  --receiver-port 7401 \
  --accept-timeout 30 \
  --quiet

wait $RECEIVER_PID
echo ""
echo "Benchmark complete. Compare the timing outputs above."
echo ""
echo "Key observations:"
echo "- sender_to_receiver_delta: network latency (receiver getting sender's msg)"
echo "- remote_update_to_ack: time for receiver to process and send ack back"
echo "- total_until_ack: total round-trip time"
echo ""
echo "The --quiet mode eliminates print/format overhead from the hot path."
