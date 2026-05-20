Fast 3-process swap (minimal_epr_fast.py)

Overview

This script implements a minimal, fast EPR swap with 3 processes:

- repeater: central node with two memories (A and B)
- client: end node (Alice or Bob)

The repeater accepts two TCP connections, maintains two Werner memories, and sends a
single swap message to each client per round. Each client measures one-way delay
using the repeater timestamp and reports latency percentiles plus Werner percentiles
(inverted so higher Werner maps to lower percentiles).

The repeater timestamp is intentionally common to both clients because it represents
the logical swap event. It is not a per-socket transmit timestamp. Transport timing
is measured separately with the repeater/client diagnostic CSV columns described
below.

Run (current baseline)

        sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python minimal_epr_fast.py repeater \
        --count 2000 \
        --quiet \
        --cpu 3 \
        --sock-buf 65536

        sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python minimal_epr_fast.py client --repeater-port 7401 --client-id 1 --repeater-id 0 --count 2000 --quiet --cpu 5 --sock-buf 65536 --plot

        sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python minimal_epr_fast.py client --repeater-port 7402 --client-id 2 --repeater-id 0 --count 2000 --quiet --cpu 7 --sock-buf 65536 --plot



Protocol

Message format is packed with struct "!QIBd":

- ts_emit_ns: uint64 (wall clock ns)
- peer_id: uint32 (other client id)
- bits: uint8 (two correction bits)
- w_swap: float64 (Werner after swap)

Werner model:

- w(t) = w0 * exp(-age / t1_ns)
- w_swap = w_ar * w_br

Quick start (localhost)

Repeater:

    python minimal_epr_fast.py repeater --count 2000 --sock-buf 65536

Client A (Alice):

    python minimal_epr_fast.py client --repeater-port 7401 --client-id 1 --count 2000 --quiet --sock-buf 65536

Client B (Bob):

    python minimal_epr_fast.py client --repeater-port 7402 --client-id 2 --count 2000 --quiet --sock-buf 65536

Note: use sudo if you enable --rt-priority or need privileged socket options.

Remote run: repeater + one client on one node, second client on another machine

Use this layout when only one remote machine is available:

- local node: repeater + client 1
- remote Ubuntu node: client 2
- SSH reverse tunnel: remote 127.0.0.1:7402 forwards to local 127.0.0.1:7402

Copy the same script version to the remote machine before running mixed-node
tests:

    scp -J pasarela@kr.ls.fi.upm.es /home/giicc/NETQ/AESO/minimal_epr_fast.py ubuntu22@192.168.0.223:/home/ubuntu22/AESO/minimal_epr_fast.py

Open the SSH tunnel from the local machine:

    ssh -J pasarela@kr.ls.fi.upm.es -o Compression=no -R 7402:127.0.0.1:7402 ubuntu22@192.168.0.223

Terminal 1, local node, repeater:

    cd /home/giicc/NETQ/AESO
    sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py repeater --werner-ar 1 --werner-br 1 --quiet

Terminal 2, local node, client 1:

    cd /home/giicc/NETQ/AESO
    sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py client --repeater-port 7401 --client-id 1 --quiet --cpu 5 --plot

Terminal 3, remote Ubuntu node, client 2:

    cd /home/ubuntu22/AESO
    sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py client --repeater-port 7402 --client-id 2 --quiet --cpu 1 --plot

All three processes must use the same --count if you override the default.
On the remote Ubuntu VM used in these tests, only CPUs 0 and 1 were available, so
client 2 used --cpu 1.

Clock synchronization outside Python

The remote Ubuntu clock was synchronized with chrony:

    sudo apt install -y chrony
    sudo chronyc -a 'burst 4/4'
    sudo chronyc makestep
    chronyc tracking
    chronyc sources -v

The target state is a small System time/Last offset in chronyc tracking and a
selected source marked with * in chronyc sources. Windows/WSL time can be harder
to force because W32Time may be driven by VMICTimeProvider or Local CMOS Clock.
For WSL runs, keep the host clock stable and use the script-level correction
below when comparing timestamps across machines.

Clock correction inside Python

By default, no Python clock correction is applied. The client records:

    delay_ns = client_time_ns - repeater_ts_emit_ns

This preserves raw signed delays, including negative values if the clocks are
offset. To apply an NTP-like pre-run correction, enable --clock-sync on the
repeater and on both clients:

    sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py repeater --werner-ar 1 --werner-br 1 --quiet --clock-sync --clock-sync-samples 64
    sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py client --repeater-port 7401 --client-id 1 --quiet --cpu 5 --plot --clock-sync --clock-sync-samples 64
    sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py client --repeater-port 7402 --client-id 2 --quiet --cpu 1 --plot --clock-sync --clock-sync-samples 64

The correction estimates repeater_clock - client_clock before the hot receive
loop. The client output and CSV include clock_offset_ns and clock_sync_rtt_ns so
bad calibrations can be detected. If the SSH path is asymmetric, the absolute
one-way value can still be biased.

For comparison plots and stats around the run baseline, keep the raw signed delay
but center the reported statistics around the median:

    sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py client --repeater-port 7402 --client-id 2 --quiet --cpu 1 --plot --center-delay

--center-delay writes both delay_ns and delay_centered_ns to the CSV. It is useful
for jitter comparisons, while delay_ns remains the raw signed cross-clock value.

Output

- Repeater prints initial Werner states and the last message/state for A and B.
- Clients print p50/p90/p95/p99 for delay and inverse Werner percentiles, plus
  mean/std and min/max/last message snapshots with count index.
- States are printed as (local_id, werner, peer_id). For example, client 1
  sharing with client 2 prints state_out=(1,w,2). Repeater empty output memories
  are printed as (repeater_id,0.000000,None).
- Correction bits are generated as integer values 0..3 and printed with two
  bits: 00, 01, 10, or 11.

Plot data (CSV)

Use --plot on clients to write per-sample delay data to csv/.

- Output path: csv/delay_hist_client_<client_id>[_N].csv
- Default columns:
  count_idx,delay_ns,delay_center_ns,delay_centered_ns,clock_offset_ns,clock_sync_rtt_ns
- delay_ns is client wall-clock arrival plus any configured clock offset, minus
  the common repeater swap timestamp.
- delay_centered_ns is delay_ns - delay_center_ns. delay_center_ns is 0 unless
  --center-delay is enabled.

Use --diag together with --plot on clients to write extra receive diagnostics.

- Diagnostic columns add loop_gap_ns and recv_block_ns.
- loop_gap_ns is the monotonic time between client loop iterations.
- recv_block_ns is the time spent inside recv_exact_into().

Use --plot on the repeater to write a lightweight count CSV. Use --diag together
with --plot to write per-count send diagnostic data to csv/.

- Output path: csv/repeater_send_hist[_N].csv
- Default columns: count_idx
- Diagnostic columns: count_idx,send_a_block_ns,send_b_block_ns,send_gap_ab_ns
- send_a_block_ns/send_b_block_ns measure time spent inside each sendall().
- send_gap_ab_ns measures the gap from the start of send A to the start of send B
  in sequential mode. In --parallel mode it is written as 0.

Diagnostic run example:

    sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python minimal_epr_fast.py repeater --count 2000 --quiet --cpu 3 --sock-buf 65536 --plot --diag
    sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python minimal_epr_fast.py client --repeater-port 7401 --client-id 1 --repeater-id 0 --count 2000 --quiet --cpu 5 --sock-buf 65536 --plot --diag
    sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python minimal_epr_fast.py client --repeater-port 7402 --client-id 2 --repeater-id 0 --count 2000 --quiet --cpu 7 --sock-buf 65536 --plot --diag

Recent diagnostic interpretation:

- With --sock-buf 4096, repeater send/gap p99 could rise into the ~150-250 us
  range, and those events strongly correlated with client delay spikes.
- With --sock-buf 65536, repeater send/gap spikes dropped sharply in recent runs.
  This should be treated as the current baseline.
- Remaining large client spikes can still come from client wakeup/scheduling or
  receive backlog, visible in loop_gap_ns and recv_block_ns.

Plotter (plot_delay_hist.py)

This is a standalone plotting helper so the main script does not depend on matplotlib.
It discovers CSV files under csv/ by default and produces PNGs under plots/:

- plots/sec/ : histogram of delays
- plots/counter/ : delay vs count index

Examples:

    python plot_delay_hist.py
    python plot_delay_hist.py --last
    python plot_delay_hist.py csv/delay_hist_client_1.csv

Filtered/outlier plots:

The normal plots keep every sample. To make the spike windows easier to inspect,
the plotter can also write extra per-count PNGs:

- plots/counter_filtered/ : delay vs count with slow samples removed from the view
- plots/counter_outliers/ : delay vs count with all samples kept and slow samples highlighted

Create only the extra filtered/outlier PNGs, leaving the existing normal plots untouched:

    python plot_delay_hist.py --filtered-only --last

With --filtered-only --last, a CSV is skipped when both extra PNGs already exist:

    plots/counter_filtered/<csv_name>_seq_filtered.png
    plots/counter_outliers/<csv_name>_seq_outliers.png

Create normal plots plus the extra filtered/outlier PNGs:

    python plot_delay_hist.py --filtered --last

Create filtered/outlier PNGs for one CSV:

    python plot_delay_hist.py --filtered-only csv/delay_hist_client_1_14.csv

Use a manual delay threshold in microseconds:

    python plot_delay_hist.py --filtered-only --filter-threshold-us 500

Use a stricter or looser automatic MAD threshold:

    python plot_delay_hist.py --filtered-only --mad-scale 12
    python plot_delay_hist.py --filtered-only --mad-scale 6

Run count variations:

    sudo python minimal_epr_fast.py repeater --count 1000 --quiet --sock-buf 65536 --plot
    sudo python minimal_epr_fast.py client --repeater-port 7401 --client-id 1 --repeater-id 0 --count 1000 --quiet --cpu 5 --sock-buf 65536 --plot
    sudo python minimal_epr_fast.py client --repeater-port 7402 --client-id 2 --repeater-id 0 --count 1000 --quiet --cpu 7 --sock-buf 65536 --plot

    sudo python minimal_epr_fast.py repeater --count 500 --quiet --sock-buf 65536 --plot
    sudo python minimal_epr_fast.py client --repeater-port 7401 --client-id 1 --repeater-id 0 --count 500 --quiet --cpu 5 --sock-buf 65536 --plot
    sudo python minimal_epr_fast.py client --repeater-port 7402 --client-id 2 --repeater-id 0 --count 500 --quiet --cpu 7 --sock-buf 65536 --plot

    sudo python minimal_epr_fast.py repeater --count 400 --quiet --sock-buf 65536 --plot
    sudo python minimal_epr_fast.py client --repeater-port 7401 --client-id 1 --repeater-id 0 --count 400 --quiet --cpu 5 --sock-buf 65536 --plot
    sudo python minimal_epr_fast.py client --repeater-port 7402 --client-id 2 --repeater-id 0 --count 400 --quiet --cpu 7 --sock-buf 65536 --plot

Notes

- The repeater sends: timestamp, peer id, 2 correction bits, and w_swap.
- The timestamp is common to both clients and represents the simulated swap event.
- Clients only measure the one-way delta (arrival - common repeater timestamp).
- Werner percentiles are inverted so higher w maps to lower percentiles.
- If --werner-ar/--werner-br are omitted, the repeater asks for them interactively.
- Use --sock-buf 0 to disable socket buffer tuning.
- The current baseline uses --sock-buf 65536 because it reduced repeater sendall()
  blocking in recent measurements. 4096 is still accepted but is more prone to
  backpressure spikes.
- The repeater uses separate A/B message buffers even in sequential mode.
- Correction bits are precomputed before the hot loop to avoid random-number
  generation in the measured send path.
- Clients store raw delay and raw w_swap in the receive loop. Werner decay for
  percentiles/states is computed after the receive loop, so math.exp() is not in
  the hot receive path.
- --diag enables extra monotonic timing inside the hot send/receive loops. Use it
  for diagnosis, but leave it off for the cleanest performance baseline.
- --clock-sync is disabled by default. Enable it on the repeater and both clients
  only when you want the pre-run clock offset calibration handshake.
- --parallel creates sender threads once before the count loop. It does not create
  threads per count, but it does add barrier/scheduler overhead. It keeps the
  common swap timestamp while attempting A/B sends in parallel.

Help (-h)

Top-level:

    usage: minimal_epr_fast.py [-h] {repeater,client} ...

    Fast3 unified: repeater/client with persistent sockets and low-jitter options.

    positional arguments:
      {repeater,client}
        repeater         Run in repeater mode.
        client           Run in client mode.

    options:
      -h, --help         show this help message and exit

Repeater:

    usage: minimal_epr_fast.py repeater [-h] [--listen-host-a LISTEN_HOST_A]
                                        [--listen-port-a LISTEN_PORT_A]
                                        [--listen-host-b LISTEN_HOST_B]
                                        [--listen-port-b LISTEN_PORT_B]
                                        [--count COUNT]
                                        [--accept-timeout ACCEPT_TIMEOUT]
                                        [--cpu CPU] [--rt-priority RT_PRIORITY]
                                        [--sock-buf SOCK_BUF]
                                        [--busy-poll-us BUSY_POLL_US]
                                        [--repeater-id REPEATER_ID]
                                        [--client-a-id CLIENT_A_ID]
                                        [--client-b-id CLIENT_B_ID]
                                        [--werner-ar WERNER_AR]
                                        [--werner-br WERNER_BR] [--parallel]
                                        [--cpu-a CPU_A] [--cpu-b CPU_B]
                                        [--count-interval COUNT_INTERVAL]
                                        [--quiet] [--plot]
                                        [--plot-prefix PLOT_PREFIX] [--diag]
                                        [--clock-sync]
                                        [--clock-sync-samples CLOCK_SYNC_SAMPLES]

    options:
      -h, --help            show this help message and exit
      --listen-host-a LISTEN_HOST_A
      --listen-port-a LISTEN_PORT_A
      --listen-host-b LISTEN_HOST_B
      --listen-port-b LISTEN_PORT_B
      --count COUNT
      --accept-timeout ACCEPT_TIMEOUT
      --cpu CPU             Pin this process to one CPU core.
      --rt-priority RT_PRIORITY
                            Set SCHED_FIFO priority (1-99), usually needs sudo.
      --sock-buf SOCK_BUF   Set both SO_SNDBUF/SO_RCVBUF if > 0.
      --busy-poll-us BUSY_POLL_US
                            Set SO_BUSY_POLL in microseconds if supported.
      --repeater-id REPEATER_ID
      --client-a-id CLIENT_A_ID
      --client-b-id CLIENT_B_ID
      --werner-ar WERNER_AR
      --werner-br WERNER_BR
      --parallel            Send to A/B in parallel threads.
      --cpu-a CPU_A         Pin sender thread A to this CPU core.
      --cpu-b CPU_B         Pin sender thread B to this CPU core.
      --count-interval COUNT_INTERVAL
                            Sleep seconds between counts.
      --quiet
      --plot                Write repeater send timing CSV data.
      --plot-prefix PLOT_PREFIX
                            Prefix for repeater send timing CSV outputs.
      --diag                Measure extra repeater send timing diagnostics.
      --clock-sync          Enable pre-run clock offset calibration handshake.
                            Enable it on both clients too.
      --clock-sync-samples CLOCK_SYNC_SAMPLES
                            Calibration exchanges used only with --clock-sync.

Client:

    usage: minimal_epr_fast.py client [-h] [--repeater-host REPEATER_HOST]
                                      [--repeater-port REPEATER_PORT]
                                      [--count COUNT] [--warmup WARMUP]
                                      [--connect-timeout CONNECT_TIMEOUT]
                                      [--detect-timeout DETECT_TIMEOUT]
                                      [--detect-interval DETECT_INTERVAL]
                                      [--cpu CPU] [--rt-priority RT_PRIORITY]
                                      [--sock-buf SOCK_BUF]
                                      [--busy-poll-us BUSY_POLL_US]
                                      [--client-id CLIENT_ID]
                                      [--repeater-id REPEATER_ID]
                                      [--werner-in WERNER_IN] [--plot]
                                      [--plot-prefix PLOT_PREFIX]
                                      [--count-interval COUNT_INTERVAL] [--quiet]
                                      [--t1-ns T1_NS] [--diag] [--clock-sync]
                                      [--clock-sync-samples CLOCK_SYNC_SAMPLES]
                                      [--clock-offset-ns CLOCK_OFFSET_NS]
                                      [--center-delay]

    options:
      -h, --help            show this help message and exit
      --repeater-host REPEATER_HOST
      --repeater-port REPEATER_PORT
      --count COUNT
      --warmup WARMUP
      --connect-timeout CONNECT_TIMEOUT
      --detect-timeout DETECT_TIMEOUT
      --detect-interval DETECT_INTERVAL
      --cpu CPU             Pin this process to one CPU core.
      --rt-priority RT_PRIORITY
                            Set SCHED_FIFO priority (1-99), usually needs sudo.
      --sock-buf SOCK_BUF   Set both SO_SNDBUF/SO_RCVBUF if > 0.
      --busy-poll-us BUSY_POLL_US
                            Set SO_BUSY_POLL in microseconds if supported.
      --client-id CLIENT_ID
      --repeater-id REPEATER_ID
      --werner-in WERNER_IN
      --plot                Write delay histogram data and plot if matplotlib is
                            available.
      --plot-prefix PLOT_PREFIX
                            Prefix for plot outputs.
      --count-interval COUNT_INTERVAL
                            Sleep seconds between counts.
      --quiet
      --t1-ns T1_NS
      --diag                Measure extra client loop/recv timing diagnostics.
      --clock-sync          Estimate repeater-clock minus client-clock offset
                            before the data loop. Repeater and both clients must
                            enable it.
      --clock-sync-samples CLOCK_SYNC_SAMPLES
                            Calibration exchanges used only with --clock-sync.
      --clock-offset-ns CLOCK_OFFSET_NS
                            Manual repeater-clock minus client-clock offset. Skips
                            auto calibration and does not require --clock-sync.
      --center-delay        Center delay stats around the run median; raw signed
                            delay stays in CSV.
