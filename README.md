Fast unified (minimal_epr_fast.py)

This repo concentrates the final fast EPR-over-TCP version focused on latency and consistent measurement. Earlier test scripts live in pre_scripts/ with their own README files for historical reference and comparisons.

Arguments (summary of -h)

Sender mode:

- --receiver-host: Receiver host/IP to connect to.
- --receiver-port: Receiver TCP port.
- --count: Number of exchanges to run.
- --warmup: Initial exchanges to ignore in stats.
- --connect-timeout: Timeout per connect attempt (seconds).
- --detect-timeout: Total time to keep probing for receiver (seconds).
- --detect-interval: Delay between probe attempts (seconds).
- --cpu: Pin this process to a CPU core.
- --rt-priority: Set SCHED_FIFO priority (1-99). Needs sudo.
- --sock-buf: Set SO_SNDBUF/SO_RCVBUF if > 0.
- --busy-poll-us: Set SO_BUSY_POLL in microseconds if supported.
- --show-arrows: Print timing arrows tables.
- --werner-min: Minimum Werner floor.
- --t1-ns: Werner decay timescale in ns.
- --quiet: Reduce output to summary tables.

Receiver mode:

- --listen-host: Bind address for the receiver.
- --listen-port: Bind port for the receiver.
- --count: Number of exchanges to run.
- --warmup: Initial exchanges to ignore in stats.
- --accept-timeout: Time to wait for sender to connect (seconds).
- --cpu: Pin this process to a CPU core.
- --rt-priority: Set SCHED_FIFO priority (1-99). Needs sudo.
- --sock-buf: Set SO_SNDBUF/SO_RCVBUF if > 0.
- --busy-poll-us: Set SO_BUSY_POLL in microseconds if supported.
- --werner-min: Minimum Werner floor.
- --t1-ns: Werner decay timescale in ns.
- --show-arrows: Print receiver timing table.
- --quiet: Reduce output to summary tables.

Receiver (with sudo for RT):

    sudo python minimal_epr_fast.py receiver \
      --listen-host 0.0.0.0 \
      --listen-port 7401 \
      --count 1000 \
      --warmup 50 \
      --accept-timeout 30.0 \
      --cpu 3 \
      --rt-priority 50 \
      --sock-buf 0 \
      --busy-poll-us 25 \
      --werner-min 0.2 \
      --t1-ns 1000000.0 \
      --quiet

Sender (with sudo for RT):

    sudo python minimal_epr_fast.py sender \
      --receiver-host 127.0.0.1 \
      --receiver-port 7401 \
      --count 1000 \
      --warmup 50 \
      --connect-timeout 10.0 \
      --detect-timeout 30.0 \
      --detect-interval 0.05 \
      --cpu 2 \
      --rt-priority 50 \
      --sock-buf 0 \
      --busy-poll-us 25 \
      --show-arrows \
      --werner-min 0.2 \
      --t1-ns 1000000.0 \
      --quiet

Best sweep results (mean p50 RTT from run_fast_sweep.sh):

Defaults in minimal_epr_fast.py were chosen from the sweep results produced by run_fast_sweep.sh.

- Best overall: count=1000, detect_interval=0.05, busy_poll=25, rt_priority=50, sock_buf=4096
- Best by count:
  - count=1000: detect_interval=0.05, busy_poll=25, rt_priority=50, sock_buf=4096
  - count=3000: detect_interval=0.05, busy_poll=0, rt_priority=50, sock_buf=0
- Best by detect_interval:
  - 0.01: detect_interval=0.01, busy_poll=25, rt_priority=50, sock_buf=0
  - 0.05: detect_interval=0.05, busy_poll=25, rt_priority=50, sock_buf=4096
  - 0.1: detect_interval=0.1, busy_poll=0, rt_priority=50, sock_buf=4096
- Best by busy_poll:
  - 0: detect_interval=0.05, busy_poll=0, rt_priority=50, sock_buf=0
  - 25: detect_interval=0.05, busy_poll=25, rt_priority=50, sock_buf=4096
  - 50: detect_interval=0.05, busy_poll=50, rt_priority=50, sock_buf=0
- Best by rt_priority:
  - 50: detect_interval=0.05, busy_poll=25, rt_priority=50, sock_buf=4096
- Best by sock_buf:
  - 0: detect_interval=0.05, busy_poll=25, rt_priority=50, sock_buf=0
  - 4096: detect_interval=0.05, busy_poll=25, rt_priority=50, sock_buf=4096
  - 65536: detect_interval=0.05, busy_poll=25, rt_priority=50, sock_buf=65536

Note

- If you do not have RT permissions, remove --rt-priority or run without sudo.


Timing arrows mapping (fast sender output):

```
  Sender                             Receiver
  |                                   |
  | ts_emit_ns (unix)                 |
  |-----------------┐                 |
  |                 └---------------->|  sender_to_receiver (ts_recv_ns (receiver time))
  |                                   |  (local state update)
  |                      ts_recv_ns   |
  |                 ┌-----------------|  receiver_to_ack_send
  |<----------------┘                 |  
  |<----- total_round_trip_perf ----->|

  
  Sender                             Receiver
  |                                   |
  | ts_emit_ns (unix)                 |
  |-----------------┐                 |
  |                 └---------------->|  sender_to_receiver (ts_recv_ns (receiver time))
  |                                   |  (local state update)
  |                      ts_recv_ns   |
  |                 ┌-----------------|  receiver_to_ack_send
  |<-------- total_receiver_view --------->|
```

Remote Proxmox VM through SSH jump host

The examples below keep the ports visible and redact usernames, hosts, and IP
addresses.

Receiver on PC, sender on VM:

First, open the reverse SSH tunnel from the PC:

```bash
ssh -J <jump-user>@<jump-host> -o Compression=no -R 7401:127.0.0.1:7401 <vm-user>@<vm-private-ip>
```

On the PC, run the receiver:

```bash
sudo python minimal_epr_fast.py receiver
```

On the VM, run the sender:

```bash
sudo python3 /home/<vm-user>/minimal_epr_fast.py sender --receiver-host 127.0.0.1 --receiver-port 7401
```

Sender on PC, receiver on VM:

First, open the local SSH tunnel from the PC:

```bash
ssh -J <jump-user>@<jump-host> -L 7402:127.0.0.1:7402 <vm-user>@<vm-private-ip>
```

On the PC, run the sender:

```bash
sudo python3 minimal_epr_fast.py sender --receiver-host 127.0.0.1 --receiver-port 7402
```

On the VM, run the receiver:

```bash
sudo python3 minimal_epr_fast.py receiver --listen-host 127.0.0.1 --listen-port 7402
```
