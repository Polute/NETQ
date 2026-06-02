# Minimal EPR Fast in C

This directory contains the C port of `../minimal_epr_fast.py`.
It is meant to run the same local experiments without depending on the Python runtime.

## Layout

```text
AESO/c_code/
  minimal_epr_fast.c      C source
  minimal_epr_fast_c      compiled binary
  README_C.md             this file
  csv_*                   C experiment CSV folders
  plots_*                 C plot folders, if generated
```

## Build

From this directory:

```bash
cd /home/giicc/NETQ/AESO/c_code
gcc -O3 -Wall -Wextra -pthread -o minimal_epr_fast_c minimal_epr_fast.c -lm
```

The binary is:

```bash
/home/giicc/NETQ/AESO/c_code/minimal_epr_fast_c
```

## What This Compares

The C binary keeps the same core structure as the Python script:

- one `repeater` and two `client` processes;
- TCP control channel;
- data over `--data-protocol udp` or `--data-protocol tcp`;
- send mode with `--send-mode burst|paced|ack`;
- CSV output with `--plot --plot-dir ...`;
- CPU pinning with `--cpu`;
- real-time scheduling by default with `--rt-priority 50`, matching Python.

If C is much faster than Python, the bottleneck is probably Python/runtime scheduling.
If C behaves similarly, the issue is more likely the PC, VM, kernel, buffers, scheduler, or the test structure.

## Send Modes

`--send-mode burst` is the default. The repeater sends all messages as fast as possible.
Use it to stress queues, buffers, and scheduling.

`--send-mode paced` uses `--count-interval` to space rounds:

```bash
--send-mode paced --count-interval 0.0001
```

`--send-mode ack` waits for an ACK from both clients before moving to the next `count`.
Use it to measure without inter-round queue buildup. It lowers throughput, but gives a cleaner latency measurement.

## UDP Local Burst

Terminal 1, repeater:

```bash
cd /home/giicc/NETQ/AESO/c_code

sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc ./minimal_epr_fast_c repeater \
  --listen-host-a 127.0.0.1 \
  --listen-port-a 7401 \
  --listen-host-b 127.0.0.1 \
  --listen-port-b 7402 \
  --werner-ar 1 \
  --werner-br 1 \
  --quiet \
  --data-protocol udp \
  --send-mode burst \
  --plot \
  --plot-dir csv_local_udp_c \
  --accept-timeout 120 \
  --cpu 3
```

Terminal 2, client 1:

```bash
cd /home/giicc/NETQ/AESO/c_code

sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc ./minimal_epr_fast_c client \
  --repeater-host 127.0.0.1 \
  --repeater-port 7401 \
  --client-id 1 \
  --quiet \
  --cpu 5 \
  --plot \
  --plot-dir csv_local_udp_c \
  --data-protocol udp \
  --send-mode burst
```

Terminal 3, client 2:

```bash
cd /home/giicc/NETQ/AESO/c_code

sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc ./minimal_epr_fast_c client \
  --repeater-host 127.0.0.1 \
  --repeater-port 7402 \
  --client-id 2 \
  --quiet \
  --cpu 7 \
  --plot \
  --plot-dir csv_local_udp_c \
  --data-protocol udp \
  --send-mode burst
```

## UDP Local ACK

Use the same three UDP commands, changing:

```bash
--send-mode burst
```

to:

```bash
--send-mode ack
```

and use a separate output folder, for example:

```bash
--plot-dir csv_local_udp_c_ack
```

## TCP Local Burst

Same setup, changing only protocol and output folder.

Terminal 1, repeater:

```bash
cd /home/giicc/NETQ/AESO/c_code

sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc ./minimal_epr_fast_c repeater \
  --listen-host-a 127.0.0.1 \
  --listen-port-a 7401 \
  --listen-host-b 127.0.0.1 \
  --listen-port-b 7402 \
  --werner-ar 1 \
  --werner-br 1 \
  --quiet \
  --data-protocol tcp \
  --send-mode burst \
  --plot \
  --plot-dir csv_local_tcp_c \
  --accept-timeout 120 \
  --cpu 3
```

Terminal 2, client 1:

```bash
cd /home/giicc/NETQ/AESO/c_code

sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc ./minimal_epr_fast_c client \
  --repeater-host 127.0.0.1 \
  --repeater-port 7401 \
  --client-id 1 \
  --quiet \
  --cpu 5 \
  --plot \
  --plot-dir csv_local_tcp_c \
  --data-protocol tcp \
  --send-mode burst
```

Terminal 3, client 2:

```bash
cd /home/giicc/NETQ/AESO/c_code

sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc ./minimal_epr_fast_c client \
  --repeater-host 127.0.0.1 \
  --repeater-port 7402 \
  --client-id 2 \
  --quiet \
  --cpu 7 \
  --plot \
  --plot-dir csv_local_tcp_c \
  --data-protocol tcp \
  --send-mode burst
```

## TCP Local ACK

Use the same three TCP commands, changing:

```bash
--send-mode burst
```

to:

```bash
--send-mode ack
```

and use a separate output folder, for example:

```bash
--plot-dir csv_local_tcp_c_ack
```

## Running Without Sudo

The C binary accepts:

```bash
--rt-priority -1
```

This disables `SCHED_FIFO`. The commands above do not use it because they reproduce the Python default, where `--rt-priority` is `50`.

## CSV Permissions

When the binary is run with `sudo`, CSV files and the final `--plot-dir` directory are changed back to the original user detected through `SUDO_UID` and `SUDO_GID`.
That makes the generated files removable without `sudo`.
