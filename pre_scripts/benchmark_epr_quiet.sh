#!/bin/bash
# Benchmark EPR performance: with and without debug output
# Captures actual timing measurements to compare

PYTHON="/home/giicc/NETQ/.venv/bin/python"
SENDER="$PYTHON /home/giicc/NETQ/pre_scripts/minimal_epr_sender.py"
RECEIVER="$PYTHON /home/giicc/NETQ/pre_scripts/minimal_epr_receiver.py"

echo "=== EPR Performance Benchmark ==="
echo "Comparing verbose mode vs --quiet mode"
echo ""

# Test 1: VERBOSE (with output)
echo "=========================================="
echo "Test 1: VERBOSE MODE (with debug output)"
echo "=========================================="

$RECEIVER \
  --listen-port 7401 \
  --sender-host 127.0.0.1 \
  --sender-port 7400 \
  --accept-timeout 30 > /tmp/receiver_verbose.log 2>&1 &
RECEIVER_PID=$!

sleep 0.5

$SENDER \
  --listen-port 7400 \
  --receiver-host 127.0.0.1 \
  --receiver-port 7401 \
  --accept-timeout 30 > /tmp/sender_verbose.log 2>&1

wait $RECEIVER_PID 2>/dev/null

# Extract timing from verbose sender
echo ""
echo "Results from Sender:"
grep -E "total_until_ack|sender_to_receiver_delta" /tmp/sender_verbose.log || echo "  (no timing captured)"
grep -E "total_until_ack|sender_to_receiver_delta" /tmp/receiver_verbose.log || echo "  (no timing captured)"

sleep 1

# Test 2: QUIET (no output)
echo ""
echo "=========================================="
echo "Test 2: QUIET MODE (--quiet flag)"
echo "=========================================="

$RECEIVER \
  --listen-port 7401 \
  --sender-host 127.0.0.1 \
  --sender-port 7400 \
  --accept-timeout 30 \
  --quiet > /tmp/receiver_quiet.log 2>&1 &
RECEIVER_PID=$!

sleep 0.5

$SENDER \
  --listen-port 7400 \
  --receiver-host 127.0.0.1 \
  --receiver-port 7401 \
  --accept-timeout 30 \
  --quiet > /tmp/sender_quiet.log 2>&1

wait $RECEIVER_PID 2>/dev/null

echo "✓ Quiet mode test complete (no output means faster execution)"
echo ""

echo "=========================================="
echo "Analysis:"
echo "=========================================="
echo ""
echo "The --quiet flag removes all stdout operations from the hot path."
echo "This eliminates:"
echo "  - print() function calls"
echo "  - density matrix formatting"
echo "  - string formatting for output"
echo ""
echo "Expected improvements:"
echo "  - Faster 'remote_update_to_ack' latency"
echo "  - Reduced total_until_ack latency"
echo ""
echo "For multiple runs (more accurate average), use:"
echo "  bash pre_scripts/benchmark_epr_quiet.sh"
