# `minimal_epr_fast.py` Explained

This README explains the main script, `minimal_epr_fast.py`: what each function
does, why the code is structured this way, and why the experiment is executed as
three separate processes.

## Purpose

`minimal_epr_fast.py` implements a minimal three-node EPR swap timing experiment:

- one `repeater` process
- one client process for Alice: `client --client-id 1`
- one client process for Bob: `client --client-id 2`

The repeater owns two simulated memories and sends one fixed-size binary message
per count to both clients. Each client receives its message, compares the local
receive timestamp with the repeater timestamp, stores the delay, and reports
latency/Werner statistics.

The script is optimized for low jitter rather than generic application design.
That is why it uses persistent sockets, fixed binary structs, reused buffers,
preallocated arrays, optional CPU pinning, optional real-time scheduling, and GC
disable/restore around the hot loops.

## Why It Runs As Three Processes

The experiment models three network nodes:

1. `repeater`: central node with A-Repeater and B-Repeater memories.
2. `client --client-id 1`: Alice side.
3. `client --client-id 2`: Bob side.

Running them as separate processes exposes real OS scheduling, socket wakeups,
TCP buffering, clock offset, and machine state. Those effects are part of what is
being measured. Running everything inside one Python process would hide most of
that behavior.

## Basic Execution

Terminal 1, repeater:

```bash
sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py repeater --werner-ar 1 --werner-br 1 --quiet
```

Terminal 2, client 1:

```bash
sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py client --repeater-port 7401 --client-id 1 --quiet --cpu 5 --plot
```

Terminal 3, client 2:

```bash
sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py client --repeater-port 7402 --client-id 2 --quiet --cpu 7 --plot
```

Execution order matters:

1. Start the repeater first.
2. Start both clients.
3. If `--count` is overridden, use the same value in all three processes.

The repeater waits until both client sockets are connected before it starts the
measured count loop.

## Message Format

The main data message is packed with:

```python
MSG_FORMAT = "!QIBd"
```

Fields:

- `Q`: `ts_emit_ns`, unsigned 64-bit repeater wall-clock timestamp.
- `I`: `peer_id`, unsigned 32-bit id of the other client.
- `B`: `bits`, unsigned 8-bit value holding the two correction bits as 0..3.
- `d`: `w_swap`, 64-bit floating-point Werner value after swap.

The `!` prefix uses network byte order and a fixed binary layout. This avoids
text parsing, variable-length reads, and extra allocations inside the measured
path.

## Timing Model

The script uses two clocks:

- `time.time_ns()`: wall-clock time. This is used for repeater/client timestamp
  comparison because cross-machine timestamps need a shared clock domain.
- `time.monotonic_ns()`: local monotonic time. This is used for diagnostics such
  as `sendall()` duration, receive blocking time, and loop gaps.

Client delay is computed as:

```text
delay_ns = client_receive_time_ns + clock_offset_ns - repeater_ts_emit_ns
```

By default:

```text
clock_offset_ns = 0
```

So the script preserves raw signed delay. If clocks are offset, delay may be
negative, and that is intentionally kept in the CSV/output.

For min/max and percentile delay statistics, the script uses distance from zero:

```text
abs(delay_ns)
```

This makes:

- `min`: the sample closest to zero
- `max`: the sample farthest from zero

regardless of whether the signed value is positive or negative.

## Clock Correction

Automatic clock correction is disabled by default.

This is intentional. On SSH tunnels, remote VMs, or asymmetric paths, a PTP-style
estimate can be biased. The safest default is to preserve the raw signed
measurement and only correct the clock when explicitly requested.

When `--clock-sync` is enabled, it must be enabled on the repeater and on both
clients. The repeater acts as the PTP master clock and the client acts as the
slave. The exchange is performed before the measured loop:

1. repeater sends `Sync` at master time `t1`
2. client receives `Sync` at slave time `t2`
3. client sends `Delay_Req` at slave time `t3`
4. repeater receives `Delay_Req` at master time `t4`

The PTP equations are:

```text
slave_minus_master = ((t2 - t1) - (t4 - t3)) / 2
master_minus_slave = ((t4 - t3) - (t2 - t1)) / 2
mean_path_delay = ((t2 - t1) + (t4 - t3)) / 2
```

The script stores `clock_offset_ns = master_minus_slave`, because the client
needs to convert its local receive timestamp into the repeater/master clock
domain:

```text
corrected_client_time = client_time + clock_offset_ns
```

The repeater echoes the Sync and Delay_Req timestamps in the Delay_Resp, so the
client can validate that the response belongs to the exchange being measured.
The client averages `master_minus_slave` across all `--clock-sync-samples`; it
does not select the lowest-delay sample. The reported `clock_sync_path_delay_ns`
is the average PTP mean path delay across those samples. This smooths sample
noise, but it cannot fully remove asymmetric-path error.

`--center-delay` is different. It does not synchronize clocks. It subtracts the
run median from the reported statistics:

```text
delay_centered_ns = delay_ns - median(delay_ns)
```

This is useful when the absolute cross-clock offset is not the focus and the goal
is to inspect jitter around the run baseline.

## Constants

### `MSG_FORMAT` / `MSG_SIZE`

Define the binary message format and fixed message size for the main data path.
The fixed size lets the client read exactly one full message per count.

### `CLOCK_SYNC_REQUEST_FORMAT` / `CLOCK_SYNC_RESPONSE_FORMAT`

Define the small request/response structs used only for `--clock-sync`. They run
before the measured loop and are separate from the main data messages.

## Function Reference

### `parse_args()`

Builds the command-line interface.

It creates two subcommands:

- `repeater`
- `client`

It defines all runtime parameters: ports, count, CPU pinning, real-time priority,
socket buffers, diagnostics, plotting, clock sync, manual clock offset, and
median-centering.

Why it is designed this way:

- One file can run both roles.
- The role is selected explicitly.
- Defaults keep the common run short to type while preserving control for remote
  and diagnostic runs.

### `enable_low_latency_socket(sock, sock_buf=0, busy_poll_us=0)`

Applies low-latency socket options when the OS supports them:

- `TCP_NODELAY`: disables Nagle's algorithm so tiny messages are not delayed for
  batching.
- `TCP_QUICKACK`: asks Linux to ACK quickly when available.
- `SO_SNDBUF` / `SO_RCVBUF`: sets send/receive buffer sizes.
- `SO_BUSY_POLL`: enables short kernel busy-polling if supported.

Unsupported options are ignored so the script can still run across different
Linux/VM configurations.

### `apply_cpu_rt(cpu=None, rt_priority=None)`

Optionally pins the current process to one CPU and optionally sets `SCHED_FIFO`
real-time scheduling.

Why:

- CPU pinning reduces cross-core migration.
- Real-time scheduling can reduce wakeup latency.
- These operations usually require `sudo`.

### `recv_exact_into(sock, buf)`

Reads exactly `len(buf)` bytes from a TCP socket into an existing buffer.

Why it is needed:

- TCP is a byte stream, not a message protocol.
- One `recv()` is not guaranteed to return a full message.
- Reusing a `bytearray` avoids allocating a new bytes object per count.

### `recv_exact(sock, size)`

Allocates a temporary buffer, reads exactly `size` bytes using
`recv_exact_into()`, and returns immutable `bytes`.

It is used for the clock-sync handshake. The hot data loop uses
`recv_exact_into()` directly to avoid allocation.

### `serve_clock_sync(sock, samples)`

Repeater-side helper for `--clock-sync`.

For each sample:

1. captures `t1`
2. sends a PTP-like `Sync` message to the client
3. receives the client's `Delay_Req` containing `t1`, `t2`, and `t3`
4. captures `t4`
5. replies with `t1`, `t2`, `t3`, and `t4`

It runs before the measured data loop, so it does not contaminate the count
measurements.

### `estimate_clock_offset(sock, samples)`

Client-side helper for `--clock-sync`.

For each sample:

1. receives the repeater `Sync` timestamp `t1`
2. captures local receive timestamp `t2`
3. captures local `Delay_Req` transmit timestamp `t3`
4. sends `t1`, `t2`, and `t3` to the repeater
5. receives `t4` in the repeater `Delay_Resp`
6. computes offset and mean path delay
7. adds both values to running totals
8. returns the average offset and average mean path delay

It returns:

```text
(clock_offset_ns, clock_sync_path_delay_ns)
```

The offset represents `repeater_clock - client_clock`, also called
`master_minus_slave` in this document. The path delay is:

```text
mean_path_delay = ((t2 - t1) + (t4 - t3)) / 2
```

### `connect_repeater_until_ready(...)`

Repeatedly attempts to connect a client to the repeater until `detect_timeout`
expires.

Why:

- Manual three-terminal startup is not perfectly synchronized.
- The client can be started while the repeater is still becoming ready.

### `percentile(sorted_vals, p)`

Returns a simple percentile from an already-sorted list.

It does not interpolate; it selects an actual sample value. That is useful for
diagnostics because reported values really occurred in the run.

### `percentile_inverse(sorted_vals, p)`

Returns the inverse percentile.

This is used for Werner because high delay is bad, but low Werner is bad. The
inverse percentile lets labels like `p95` represent "bad-case" values for both
metrics.

### `stddev(vals, mean_value)`

Computes population standard deviation for a list of values.

It is used for absolute delay statistics and Werner statistics.

### `decay_werner(base, age_ns, t1_ns)`

Applies exponential Werner decay:

```text
w(t) = base * exp(-age_ns / t1_ns)
```

If `age_ns` is negative, it is clamped to zero for Werner only.

Why:

- Negative one-way delay means clock offset, not negative physical time.
- The signed negative delay is still preserved in CSV/output.
- Werner should not increase because of a negative age.

### `set_thread_affinity(cpu)`

Pins an individual Python thread to a CPU using `pthread_setaffinity_np` through
`ctypes`.

This matters only in `--parallel` mode, where two sender threads can be pinned
with `--cpu-a` and `--cpu-b`.

### `fmt_ns(v)`

Formats nanoseconds as both raw ns and seconds:

```text
12345 (0.000012345 s)
```

### `fmt_ts_emit(ts_ns)`

Formats the repeater wall-clock timestamp as `MM:SS.NNNNNNNNN` for readable
console output.

The raw `ts_emit_ns` is still printed.

### `fmt_state(state)`

Formats a state tuple:

```text
(local_id,werner,peer_id)
```

Example:

```text
(1,0.929276,2)
```

### `print_client_group(label, delta_ns, werner, delay_label=...)`

Prints one client summary block, such as:

- `client_p50`
- `client_p90`
- `client_p95`
- `client_p99`
- `client_mean`
- `client_std`

It prints delay and Werner. The delay label changes when `--center-delay` is
enabled.

### `print_client_message_state(label, delta_ns, msg, state_out, ...)`

Prints detailed information for one selected sample:

- `min`
- `max`
- `last`

It includes signed delay, count index, raw message fields, and output state after
Werner decay.

## `run_repeater(args)`

Main repeater implementation.

High-level flow:

1. Apply CPU/real-time settings.
2. Read `werner_ar` and `werner_br`, or prompt for them.
3. Initialize A-Repeater and B-Repeater states.
4. Allocate reusable output buffers.
5. Allocate diagnostic arrays when `--diag` is enabled.
6. Precompute correction bits.
7. Open one listening socket for client A and one for client B.
8. Accept both clients.
9. Optionally perform clock-sync handshakes.
10. Run the measured send loop.
11. Optionally write repeater CSV.
12. Print final repeater state/message summary.

### Nested `accept_one(host, port)`

Creates one TCP server socket, applies low-latency options, accepts one
connection, closes the listening socket, and returns the accepted connection.

There is exactly one accepted client per port because the experiment has exactly
two clients.

### Nested `update_round_state(now_ns, correction_bits)`

Builds the per-count logical state:

- A-Repeater state
- B-Repeater state
- common swap timestamp
- correction bits
- `w_swap`

The timestamp is common to both clients because it represents the same logical
swap event. Per-socket send timing is measured separately with `--diag`.

### Sequential Send Path

Default mode:

1. pack message for A into `outbuf_a`
2. send to client A
3. pack message for B into `outbuf_b`
4. send to client B

Why this is the default:

- simpler control flow
- no thread/barrier overhead
- `send_gap_ab_ns` can be measured clearly in `--diag`

### Parallel Send Path (`--parallel`)

Creates two sender threads once before the count loop.

Per count:

1. main thread prepares both buffers
2. a barrier releases both sender threads
3. each sender thread calls `sendall()`
4. another barrier waits for both sends to finish

Why it is optional:

- It can reduce A/B send skew.
- It adds scheduler, thread, and barrier overhead.
- It should be measured separately from the sequential baseline.

### GC Handling In The Repeater

GC is disabled only around the measured send loop and restored afterward.

Why:

- Python GC can introduce unpredictable pauses.
- The hot path is written to avoid unnecessary allocations.
- Restoring GC afterward keeps the rest of the program normal.

## `run_client(args)`

Main client implementation.

High-level flow:

1. Apply CPU/real-time settings.
2. Compute `count`, `warmup`, and sample count.
3. Preallocate delay, Werner, count-index, and diagnostic arrays.
4. Connect to the repeater.
5. Apply manual clock offset or estimate one with `--clock-sync`.
6. Disable GC.
7. Receive exactly one fixed-size message per count.
8. Store raw signed delay and raw `w_swap`.
9. Restore GC.
10. Trim arrays to the actual sample count.
11. Optionally compute median centering.
12. Compute Werner decay after the hot loop.
13. Compute percentiles, mean, std, min, max, and last.
14. Optionally write client CSV.
15. Print summary.

### Why `warmup` Exists

The first counts can include startup effects:

- cold caches
- first process wakeups
- initial TCP buffer behavior
- connection settling

`--warmup` discards those samples from statistics and CSV.

### Why Werner Is Computed After Receiving

Inside the receive loop, the script avoids expensive work:

- no `math.exp()`
- no sorting
- no CSV writes
- no formatted printing

The loop only receives, unpacks, timestamps, computes signed delay, and stores
raw values. This keeps Python-side jitter low.

### Nested `pick_by_delta(samples, want_max=False)`

Selects either the closest-to-zero or farthest-from-zero sample using
`abs(delay)`.

This is important because cross-clock delay can be positive or negative.

## `main()`

Entry point:

1. calls `parse_args()`
2. dispatches to `run_repeater(args)` or `run_client(args)`
3. returns the selected role's exit code

The final block:

```python
if __name__ == "__main__":
    raise SystemExit(main())
```

makes the script behave like a normal command-line program.

## Client CSV

Normal columns:

- `count_idx`: count number after warmup.
- `delay_ns`: signed raw/corrected delay.
- `delay_center_ns`: median used for centering, or 0.
- `delay_centered_ns`: `delay_ns - delay_center_ns`.
- `clock_offset_ns`: applied clock offset, or 0.
- `clock_sync_path_delay_ns`: average PTP mean path delay across the clock-sync
  samples, or 0.

With `--diag`, these are added:

- `loop_gap_ns`: monotonic time between client loop iterations.
- `recv_block_ns`: time spent blocked inside `recv_exact_into()`.

## Repeater CSV

Without `--diag`, it only stores `count_idx`.

With `--diag`, it stores:

- `send_a_block_ns`: time spent inside `sendall()` to client A.
- `send_b_block_ns`: time spent inside `sendall()` to client B.
- `send_gap_ab_ns`: sequential-mode gap between send A start and send B start.

## How To Interpret Runs

Raw mode:

```bash
python3 minimal_epr_fast.py client --plot
```

Use it to preserve the signed cross-clock measurement.

Centered mode:

```bash
python3 minimal_epr_fast.py client --plot --center-delay
```

Use it when the absolute clock offset is not the focus and you want jitter around
the run median.

Clock-sync mode:

```bash
python3 minimal_epr_fast.py repeater --clock-sync
python3 minimal_epr_fast.py client --clock-sync
python3 minimal_epr_fast.py client --clock-sync
```

Use it when you want the script to estimate repeater/client clock offset before
the run. It must be enabled on the repeater and both clients.

Diagnostic mode:

```bash
python3 minimal_epr_fast.py repeater --plot --diag
python3 minimal_epr_fast.py client --plot --diag
```

Use it to explain spikes:

- high `recv_block_ns`: the client waited in receive
- high `loop_gap_ns`: client scheduling/wakeup delay
- high `send_a_block_ns` or `send_b_block_ns`: repeater blocked while sending
- high `send_gap_ab_ns`: separation between sequential A and B sends

## Summary

The script separates two things:

1. Logical swap model: common repeater timestamp, peer id, correction bits, and
   Werner value.
2. Real system behavior: process scheduling, TCP delivery, socket blocking,
   clock offset, and machine state.

The hot path is intentionally small. Everything not required for the measured
send/receive loop is moved before or after it: argument parsing, allocation,
random bits, clock-sync, Werner decay, sorting, percentiles, CSV writing, and
printing.
