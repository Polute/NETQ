# AESO Fast Classical Notification Experiment

This repository contains a minimal three-process experiment for measuring the
classical communication delay after an entanglement-swapping event in a repeater
node.

The current baseline is:

- data messages over UDP;
- PTP-like clock synchronization over UDP;
- Linux kernel receive timestamps for UDP clock-sync packets;
- Linux kernel receive timestamps for UDP data packets at the clients;
- one shared swap timestamp for both clients;
- paced sending with a spin wait;
- `--sock-buf 65536` and `--busy-poll-us 25`.

The Python and C implementations are intentionally kept close to each other so
that Python runtime effects can be compared against a lower-overhead C version.

## Topology

The usual three-node deployment is:

| Role | Node id | Site | Optical link from repeater |
| --- | --- | --- | --- |
| Repeater | `226` | UPM Rectorado | central node |
| Client 1 | `223` | UPM CCS/CEDINT | `23.3 km`, `6.6 dB` |
| Client 2 | `227` | UPM Teleco/ETSIT | `1.9 km`, `2.3 dB` |

The repeater listens on:

- port `7401` for client 1;
- port `7402` for client 2.

## What the program does

The repeater represents the central swapping node. For every count, it emits one
small message to each client containing:

- the swap event timestamp in nanoseconds;
- the peer id;
- two correction bits;
- the current Werner parameter associated with the swap.

By default, the repeater uses the same timestamp for both A/B data messages. This
is intentional: it represents the same logical swapping event being announced to
both end nodes. PTP clock synchronization is not shared; each client performs its
own synchronization exchange with the repeater.

Each client receives the message, timestamps the reception, applies the estimated
clock offset, and stores the one-way delay:

```text
delay_ns = corrected_client_receive_time_ns - repeater_swap_timestamp_ns
```

The client also computes Werner decay after the receive loop. The hot receive
path stores raw samples first, then statistics are computed afterwards.

## Kernel timestamps

For UDP runs, the program can ask Linux for kernel receive timestamps with
`SO_TIMESTAMPNS`.

In Python, the socket is configured with `SO_TIMESTAMPNS`, then packets are read
with `recvmsg`/`recvmsg_into`. The kernel timestamp is extracted from the
ancillary control message (`SCM_TIMESTAMPNS`). In C, the same idea is used with
`recvmsg`.

This removes most user-space wakeup delay from the receive timestamp. It is still
not hardware timestamping: the timestamp is taken in the kernel, not at the NIC
PHY/MAC. Therefore it is better than `time.time_ns()` after `recv()`, but it is
not yet the relativistic/hardware lower bound.

For clock synchronization over UDP:

- `t1` is the repeater Sync send timestamp;
- `t2` is the client Sync receive timestamp, preferably from the kernel;
- `t3` is the client Delay_Req send timestamp;
- `t4` is the repeater Delay_Req receive timestamp, preferably from the kernel.

The client estimates repeater-clock minus client-clock offset and applies it to
all data receive timestamps. The current recommended estimator is
`best-path-median`, after discarding the first 5% of clock-sync samples.

## Recommended Python run: three nodes

Run the repeater first, then run both clients. The two clients are differentiated by port:
- **Client A** connects to port `7401` with `--client-id 1`
- **Client B** connects to port `7402` with `--client-id 2`

### Local example (all on localhost)

```bash
# Terminal 1: Repeater
sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py repeater \
  --listen-host-a 0.0.0.0 \
  --listen-port-a 7401 \
  --listen-host-b 0.0.0.0 \
  --listen-port-b 7402 \
  --werner-ar 1 \
  --werner-br 1 \
  --pswap 1.0 \
  --count 2000 \
  --quiet \
  --data-protocol udp \
  --plot \
  --plot-dir csv_local \
  --accept-timeout 120 \
  --cpu 3 \
  --sock-buf 65536 \
  --busy-poll-us 50 \
  --count-interval 0.00005 \
  --pace-mode spin

# Terminal 2: Client A
sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py client \
  --repeater-host 127.0.0.1 \
  --repeater-port 7401 \
  --client-id 1 \
  --count 2000 \
  --warmup 50 \
  --quiet \
  --data-protocol udp \
  --plot \
  --plot-dir csv_local \
  --connect-timeout 10 \
  --detect-timeout 120 \
  --detect-interval 0.02 \
  --cpu 5 \
  --sock-buf 65536 \
  --busy-poll-us 50

# Terminal 3: Client B
sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py client \
  --repeater-host 127.0.0.1 \
  --repeater-port 7402 \
  --client-id 2 \
  --count 2000 \
  --warmup 50 \
  --quiet \
  --data-protocol udp \
  --plot \
  --plot-dir csv_local \
  --connect-timeout 10 \
  --detect-timeout 120 \
  --detect-interval 0.02 \
  --cpu 7 \
  --sock-buf 65536 \
  --busy-poll-us 50
```

### External example (separate machines)

```bash
# Machine 1 (Repeater at 192.168.1.100): Repeater
sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py repeater \
  --listen-host-a 0.0.0.0 \
  --listen-port-a 7401 \
  --listen-host-b 0.0.0.0 \
  --listen-port-b 7402 \
  --werner-ar 1 \
  --werner-br 1 \
  --pswap 0.8 \
  --count 2000 \
  --quiet \
  --data-protocol udp \
  --clock-sync udp \
  --clock-sync-samples 264 \
  --clock-sync-kernel-timestamp \
  --plot \
  --plot-dir csv_external \
  --accept-timeout 120 \
  --cpu 1 \
  --sock-buf 65536 \
  --busy-poll-us 50 \
  --count-interval 0.00005 \
  --pace-mode spin

# Machine 2 (Client A at 192.168.1.101): Client A
sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py client \
  --repeater-host 192.168.1.100 \
  --repeater-port 7401 \
  --client-id 1 \
  --count 2000 \
  --warmup 50 \
  --quiet \
  --data-protocol udp \
  --clock-sync udp \
  --clock-sync-samples 264 \
  --clock-sync-kernel-timestamp \
  --kernel-timestamp \
  --plot \
  --plot-dir csv_external \
  --connect-timeout 10 \
  --detect-timeout 120 \
  --detect-interval 0.02 \
  --cpu 1 \
  --sock-buf 65536 \
  --busy-poll-us 50

# Machine 3 (Client B at 192.168.1.102): Client B
sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py client \
  --repeater-host 192.168.1.100 \
  --repeater-port 7402 \
  --client-id 2 \
  --count 2000 \
  --warmup 50 \
  --quiet \
  --data-protocol udp \
  --clock-sync udp \
  --clock-sync-samples 264 \
  --clock-sync-kernel-timestamp \
  --kernel-timestamp \
  --plot \
  --plot-dir csv_external \
  --connect-timeout 10 \
  --detect-timeout 120 \
  --detect-interval 0.02 \
  --cpu 1 \
  --sock-buf 65536 \
  --busy-poll-us 50
```

### Repeater, Rectorado, node `226`

```bash
cd ~/AESO_opt

sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py repeater \
  --listen-host-a LISTEN_HOST \
  --listen-port-a 7401 \
  --listen-host-b LISTEN_HOST \
  --listen-port-b 7402 \
  --werner-ar 1 \
  --werner-br 1 \
  --quiet \
  --data-protocol udp \
  --clock-sync udp \
  --clock-sync-samples 264 \
  --clock-sync-kernel-timestamp \
  --plot \
  --plot-dir csv_3nodes_py_udp_kernel_spin \
  --accept-timeout 120 \
  --cpu 1 \
  --sock-buf 65536 \
  --busy-poll-us 25 \
  --count-interval 0.00005 \
  --pace-mode spin
```

### Client 1, CCS/CEDINT, node `223`

```bash
cd ~/AESO_opt

sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py client \
  --repeater-host REPEATER_HOST \
  --repeater-port 7401 \
  --client-id 1 \
  --quiet \
  --data-protocol udp \
  --clock-sync udp \
  --clock-sync-samples 264 \
  --clock-sync-kernel-timestamp \
  --kernel-timestamp \
  --plot \
  --plot-dir csv_3nodes_py_udp_kernel_spin \
  --connect-timeout 120 \
  --detect-timeout 120 \
  --cpu 1 \
  --sock-buf 65536 \
  --busy-poll-us 25
```

### Client 2, Teleco/ETSIT, node `227`

```bash
cd ~/AESO_opt

sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py client \
  --repeater-host REPEATER_HOST \
  --repeater-port 7402 \
  --client-id 2 \
  --quiet \
  --data-protocol udp \
  --clock-sync udp \
  --clock-sync-samples 264 \
  --clock-sync-kernel-timestamp \
  --kernel-timestamp \
  --plot \
  --plot-dir csv_3nodes_py_udp_kernel_spin \
  --connect-timeout 120 \
  --detect-timeout 120 \
  --cpu 1 \
  --sock-buf 65536 \
  --busy-poll-us 25
```

## Recommended C run: three nodes

The C binary should be compiled on a compatible Ubuntu 22.04 system or inside the
Ubuntu 22.04 Docker container. A binary built on a newer glibc may fail on the
remote machines with a `GLIBC_2.38 not found` error.

Build from the repository root:

```bash
cd ~/AESO_opt

gcc -O3 -Wall -Wextra -pthread \
  -o c_code/minimal_epr_fast_c \
  c_code/minimal_epr_fast.c \
  -lm
```

Run the C repeater first on node `226`, then run both C clients.

### C repeater, Rectorado, node `226`

```bash
cd ~/AESO_opt

sudo ./c_code/minimal_epr_fast_c repeater \
  --listen-host-a LISTEN_HOST \
  --listen-port-a 7401 \
  --listen-host-b LISTEN_HOST \
  --listen-port-b 7402 \
  --werner-ar 1 \
  --werner-br 1 \
  --quiet \
  --data-protocol udp \
  --clock-sync udp \
  --clock-sync-samples 264 \
  --clock-sync-kernel-timestamp \
  --plot \
  --plot-dir csv_3nodes_c_udp_kernel_spin \
  --accept-timeout 120 \
  --cpu 1 \
  --sock-buf 65536 \
  --busy-poll-us 25 \
  --shared-send-timestamp \
  --send-mode paced \
  --count-interval 0.00005 \
  --pace-mode spin
```

### C client 1, CCS/CEDINT, node `223`

```bash
cd ~/AESO_opt

sudo ./c_code/minimal_epr_fast_c client \
  --repeater-host REPEATER_HOST \
  --repeater-port 7401 \
  --client-id 1 \
  --quiet \
  --data-protocol udp \
  --clock-sync udp \
  --clock-sync-samples 264 \
  --clock-sync-kernel-timestamp \
  --kernel-timestamp \
  --plot \
  --plot-dir csv_3nodes_c_udp_kernel_spin \
  --connect-timeout 120 \
  --detect-timeout 120 \
  --cpu 1 \
  --sock-buf 65536 \
  --busy-poll-us 25
```

### C client 2, Teleco/ETSIT, node `227`

```bash
cd ~/AESO_opt

sudo ./c_code/minimal_epr_fast_c client \
  --repeater-host REPEATER_HOST \
  --repeater-port 7402 \
  --client-id 2 \
  --quiet \
  --data-protocol udp \
  --clock-sync udp \
  --clock-sync-samples 264 \
  --clock-sync-kernel-timestamp \
  --kernel-timestamp \
  --plot \
  --plot-dir csv_3nodes_c_udp_kernel_spin \
  --connect-timeout 120 \
  --detect-timeout 120 \
  --cpu 1 \
  --sock-buf 65536 \
  --busy-poll-us 25
```

## Local Docker baseline

Use Docker when local WSL results are noisy. Start one container per terminal and
pin each container to one CPU:

```bash
cd /home/giicc/NETQ

docker run --rm -it \
  --network host \
  --cpuset-cpus="3" \
  --cap-add SYS_NICE \
  --cap-add NET_ADMIN \
  --ulimit rtprio=99 \
  --ulimit memlock=-1 \
  -v "$PWD":/work \
  -w /work/AESO \
  netq-ubuntu2204-py310
```

Open two more terminals with `--cpuset-cpus="5"` and `--cpuset-cpus="7"`.
Inside the containers, use the local repeater host and the same UDP/UDP kernel
timestamp options shown above.

## Output layout

With `--plot`, the programs write CSV files:

```text
csv_*/delay_hist_client_<id>[_N].csv
csv_*/clock_sync_client_<id>[_N].csv
csv_*/repeater_send_hist[_N].csv
```

With JSON enabled, which is the default when `--plot` is used, matching metadata
is written under:

```text
json_*/delay_hist_client_<id>[_N].json
json_*/repeater_send_hist[_N].json
```

The client delay CSV includes:

- `count_idx`;
- `delay_ns`, signed diagnostic delay;
- `delay_physical_ns`, clamped to zero for physical/Werner interpretation;
- `clock_offset_ns`;
- `clock_sync_path_delay_ns`.

The clock-sync CSV includes per-sample PTP values and summary fields such as:

- `clock_offset_final_ns`;
- `clock_offset_mean_ns`;
- `clock_offset_median_ns`;
- `clock_offset_best_path_median_ns`;
- `clock_offset_std_ns`;
- `clock_offset_mad_ns`;
- `clock_sync_path_delay_median_ns`;
- `clock_sync_path_delay_p95_ns`;
- `sync_quality`.

## Plotting

### Delay histogram plotter

Use the delay histogram plotter from the repository root:

```bash
MPLCONFIGDIR=/tmp/matplotlib \
/home/giicc/NETQ/.venv/bin/python AESO/plot_delay_hist.py \
  AESO/pulled_all_csv_json/223/csv_3nodes_py_udp_kernel_spin \
  AESO/pulled_all_csv_json/227/csv_3nodes_py_udp_kernel_spin \
  --filtered \
  --force
```

### Success/failure analysis plotter

Use the success/failure analysis plotter for pswap statistics. It accepts a directory and analyzes all CSV files:

```bash
# Analyze all CSVs in directory (repeater + both clients)
python AESO/plot_success_analysis.py csv_local_pswap --plot
```

The plotter writes output to the matching `plots_*` folder. It adds a small
metadata box to each figure with:

- node/site;
- optical distance and dB loss;
- link name;
- experiment folder;
- client id;
- data/sync protocol;
- kernel timestamp status;
- pacing/buffer/CPU information when available from JSON;
- delay mean, p50, p95, std;
- UDP receive/loss and sync quality when available.

When `--filtered` is used, the plotter also writes:

- `sec_filtered/`: delay histogram after removing high-delay bias/outliers;
- `counter_filtered/`: per-count delay after keeping only filtered samples;
- `counter_outliers/`: full per-count delay with removed samples highlighted.

In filtered plots, the metadata box reports the filtered delay statistics. The
`filter:` line shows the threshold, kept samples, and removed samples.

Known site metadata used in the plotter:

```text
223 -> CCS/CEDINT, Rectorado -> CCS/CEDINT, 23.3 km, 6.6 dB
226 -> Rectorado, repeater
227 -> Teleco/ETSIT, Rectorado -> Teleco/ETSIT, 1.9 km, 2.3 dB
```

## Notes and interpretation

- UDP must use direct client/repeater connectivity. TCP-only forwarding paths do
  not carry the UDP data channel.
- The current best configuration uses pacing because burst mode can measure
  local queue buildup instead of network latency.
- Kernel timestamps reduce receive-side user-space delay, but they are still not
  NIC hardware timestamps.
- Python and C results being similar is a sign that the remaining delay/jitter is
  mostly kernel, scheduler, network path, buffering, or infrastructure behavior.
- The repeater's shared data timestamp represents one logical swap event. Do not
  use shared timestamps for PTP clock sync; the code keeps PTP timestamps
  per-client/per-sample.
- `--diag` adds extra clock calls inside hot loops. Use it for diagnosis, not for
  the cleanest performance run.
- `--pswap` (probability of swap success) controls the repeater's swap success rate:
  - `--pswap 1.0`: All swaps succeed (default)
  - `--pswap 0.8`: 80% of swaps succeed, 20% fail
  - Failed swaps set `w_swap = 0.0` in the message sent to clients
  - The repeater tracks success/failure statistics in JSON output
  - Use `plot_success_analysis.py` to analyze pswap statistics
