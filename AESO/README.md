Fast 3-process swap (minimal_epr_fast.py)

Overview

This script implements a minimal, fast EPR swap with 3 processes:

- repeater: central node with two memories (A and B)
- client: end node (Alice or Bob)

The repeater accepts two TCP connections, maintains two Werner memories, and sends a
single swap message to each client per round. Each client measures one-way delay
using the repeater timestamp and reports latency percentiles plus Werner percentiles
(inverted so higher Werner maps to lower percentiles).

Run (current baseline)

        sudo python minimal_epr_fast.py repeater \
            --listen-host-a 0.0.0.0 --listen-port-a 7401 \
            --listen-host-b 0.0.0.0 --listen-port-b 7402 \
            --quiet

        sudo python minimal_epr_fast.py client --repeater-port 7401 --client-id 1 --repeater-id 0 --quiet --cpu 5 --plot

        sudo python minimal_epr_fast.py client --repeater-port 7402 --client-id 2 --repeater-id 0 --quiet --cpu 7 --plot



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

    python minimal_epr_fast.py repeater

Client A (Alice):

    python minimal_epr_fast.py client --repeater-port 7401 --client-id 1 --quiet

Client B (Bob):

    python minimal_epr_fast.py client --repeater-port 7402 --client-id 2 --quiet

Note: use sudo if you enable --rt-priority or need privileged socket options.

Output

- Repeater prints initial Werner states and the last message/state for A and B.
- Clients print p50/p90/p95/p99 for delay and inverse Werner percentiles, plus
  min/max/last message snapshots with count index.

Plot data (CSV)

Use --plot on clients to write per-sample delay data to csv/.

- Output path: csv/delay_hist_client_<client_id>[_N].csv
- Columns: count_idx,delay_ns

Plotter (plot_delay_hist.py)

This is a standalone plotting helper so the main script does not depend on matplotlib.
It discovers CSV files under csv/ by default and produces PNGs under plots/:

- plots/sec/ : histogram of delays
- plots/counter/ : delay vs count index

Examples:

    python plot_delay_hist.py
    python plot_delay_hist.py --last
    python plot_delay_hist.py csv/delay_hist_client_1.csv

Notes

- The repeater sends: timestamp, peer id, 2 correction bits, and w_swap.
- Clients only measure the one-way delta (arrival - repeater timestamp).
- Werner percentiles are inverted so higher w maps to lower percentiles.
- If --werner-ar/--werner-br are omitted, the repeater asks for them interactively.
- Use --sock-buf 0 to disable socket buffer tuning.

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
                                        [--werner-br WERNER_BR] [--t1-ns T1_NS]
                                        [--parallel] [--cpu-a CPU_A]
                                        [--cpu-b CPU_B]
                                        [--count-interval COUNT_INTERVAL]
                                        [--quiet]

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
